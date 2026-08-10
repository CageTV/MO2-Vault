"""
Reconstructs an installed mod's exact file layout directly from its source
archive, without ever running MO2's installer UI - the same core idea Wabbajack
uses. We diff the archive's extracted contents against what's actually sitting
in the live mods/<name>/ folder, matching by file content hash (not path, since
FOMOD/BAIN installers can rename or relocate files), producing a small recipe
of archive-path -> output-path pairs. Restoring just replays that recipe:
extract the archive, copy the matched files into place. No installer dialogs,
no MO2 process interaction, no RIC dependency for the install step itself.

Only files that don't hash-match anything in the archive (rare - installer
side-effects, files another mod later overwrote, etc.) can't be reconstructed
this way and fall back to being bundled individually.
"""

import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .util import logger

HASH_CHUNK_SIZE = 1024 * 1024
SUPPORTED_EXTENSIONS = (".zip", ".7z", ".7zip", ".rar")

_SEVEN_ZIP_CANDIDATES = (
    "7z", "7z.exe",
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)
_seven_zip_path: Optional[str] = None
_seven_zip_checked = False


def _find_seven_zip() -> Optional[str]:
    """A real 7-Zip install extracts far more than the pure-Python fallbacks:
    .7z archives using filters py7zr doesn't implement (BCJ2 is common in
    real-world mod archives), and .rar entirely (no pure-Python option at all).
    Cached after the first check since this doesn't change mid-run."""
    global _seven_zip_path, _seven_zip_checked
    if _seven_zip_checked:
        return _seven_zip_path
    _seven_zip_checked = True
    for candidate in _SEVEN_ZIP_CANDIDATES:
        found = shutil.which(candidate)
        if not found and Path(candidate).is_file():
            found = candidate
        if found:
            _seven_zip_path = found
            break
    return _seven_zip_path


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _index_by_hash(root: Path) -> Dict[str, List[Path]]:
    index: Dict[str, List[Path]] = {}
    for file_path in root.rglob("*"):
        if file_path.is_file():
            index.setdefault(_hash_file(file_path), []).append(file_path)
    return index


def extract_archive(archive_path: Path, destination: Path) -> bool:
    """Returns False if extraction failed or the format isn't supported by any
    available method - caller should fall back to bundling instead."""
    destination.mkdir(parents=True, exist_ok=True)

    seven_zip = _find_seven_zip()
    if seven_zip:
        try:
            subprocess.run(
                [seven_zip, "x", "-y", f"-o{destination}", str(archive_path)],
                check=True, capture_output=True, timeout=600,
            )
            return True
        except Exception as e:
            logger.error("7z.exe failed on %s, falling back: %s", archive_path.name, e)
            # fall through to the pure-Python methods below

    suffix = archive_path.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(destination)
            return True
        if suffix in (".7z", ".7zip"):
            import py7zr
            with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                archive.extractall(path=str(destination))
            return True
    except Exception as e:
        logger.error("Failed to extract %s: %s", archive_path, e)
        return False
    return False  # e.g. .rar with no 7-Zip install available


@dataclass
class RecipeEntry:
    output_path: str    # relative to mods/<name>/
    archive_path: str   # relative to the archive root


@dataclass
class RecipeResult:
    supported: bool
    matched: List[RecipeEntry] = field(default_factory=list)
    unresolved_output_paths: List[str] = field(default_factory=list)


def _match_against_index(
    mod_dir: Path, candidate_files, source_index: Dict[str, List[Path]], source_root: Path,
    skip_meta_ini: bool,
) -> RecipeResult:
    matched: List[RecipeEntry] = []
    unresolved: List[str] = []
    for installed_file in candidate_files:
        if not installed_file.is_file():
            continue
        # meta.ini is MO2's own bookkeeping file, never part of the source -
        # we always regenerate it ourselves on restore, so it's neither a
        # recipe entry nor an "unresolved" file worth bundling.
        if skip_meta_ini and installed_file.parent == mod_dir and installed_file.name.lower() == "meta.ini":
            continue
        output_rel = installed_file.relative_to(mod_dir).as_posix()
        candidates = source_index.get(_hash_file(installed_file))
        if not candidates:
            unresolved.append(output_rel)
            continue
        # Prefer a candidate whose filename matches the output filename, in
        # case identical-content files exist at multiple source paths.
        best = min(
            candidates,
            key=lambda c: (c.name != installed_file.name, len(c.relative_to(source_root).as_posix())),
        )
        matched.append(RecipeEntry(
            output_path=output_rel,
            archive_path=best.relative_to(source_root).as_posix(),
        ))
    return RecipeResult(supported=True, matched=matched, unresolved_output_paths=unresolved)


def compute_install_recipe(
    archive_path: Path, mod_dir: Path, restrict_to: Optional[Set[str]] = None
) -> RecipeResult:
    """Diffs archive_path's contents against the live mod_dir to figure out
    which archive file produced each installed file.

    restrict_to, if given, is a set of mod_dir-relative posix paths to
    consider instead of every file under mod_dir - used to check a second
    (third, ...) candidate archive against only what the previous candidate(s)
    left unresolved, when a single tool/mod folder is actually assembled from
    several separate downloads."""
    if not mod_dir.is_dir():
        return RecipeResult(supported=False)

    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_recipe_") as tmp:
        extracted = Path(tmp)
        if not extract_archive(archive_path, extracted):
            return RecipeResult(supported=False)

        archive_index = _index_by_hash(extracted)
        candidate_files = (mod_dir / p for p in restrict_to) if restrict_to is not None else mod_dir.rglob("*")
        return _match_against_index(mod_dir, candidate_files, archive_index, extracted, skip_meta_ini=restrict_to is None)


def compute_folder_recipe(
    source_dir: Path, mod_dir: Path, source_index: Optional[Dict[str, List[Path]]] = None
) -> RecipeResult:
    """Like compute_install_recipe, but the source is a live folder (e.g. the
    real game's Data/ directory) rather than an archive - for mod folders
    whose content is byte-identical to what's already sitting on disk
    elsewhere (e.g. a "Creation Club Files" mod that's just Steam-installed CC
    content copied into MO2's mod system for load-order control), so it can
    be reconstructed by copying instead of bundling raw.

    source_index can be precomputed and passed in when checking several mods
    against the same (potentially huge) source folder, to avoid re-hashing it
    once per mod."""
    if not mod_dir.is_dir() or not source_dir.is_dir():
        return RecipeResult(supported=False)
    if source_index is None:
        source_index = _index_by_hash(source_dir)
    return _match_against_index(mod_dir, mod_dir.rglob("*"), source_index, source_dir, skip_meta_ini=True)


def apply_install_recipe(archive_path: Path, recipe: List[RecipeEntry], target_mod_dir: Path) -> None:
    if not recipe:
        return
    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_apply_") as tmp:
        extracted = Path(tmp)
        if not extract_archive(archive_path, extracted):
            raise RuntimeError(f"Could not extract {archive_path.name} to apply its install recipe.")

        for entry in recipe:
            source = extracted / entry.archive_path
            if not source.is_file():
                raise RuntimeError(
                    f"Recipe expected '{entry.archive_path}' inside {archive_path.name}, but it wasn't there "
                    "(the archive on Nexus may have changed since backup)."
                )
            destination = target_mod_dir / entry.output_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def apply_folder_recipe(source_dir: Path, recipe: List[RecipeEntry], target_mod_dir: Path) -> None:
    """Counterpart to apply_install_recipe for a compute_folder_recipe result -
    copies straight from source_dir, no archive extraction involved."""
    if not recipe:
        return
    for entry in recipe:
        source = source_dir / entry.archive_path
        if not source.is_file():
            raise RuntimeError(
                f"Recipe expected '{entry.archive_path}' under {source_dir}, but it wasn't there "
                "(the target's game files may differ from the source's at backup time)."
            )
        destination = target_mod_dir / entry.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
