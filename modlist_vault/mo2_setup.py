"""
Sets up MO2 itself on the target instance: extracts the official portable
release archive, overlays whatever third-party plugin files aren't part of
that stock release, and rewrites ModOrganizer.ini's paths for the new location.

Requires py7zr (only for this feature - everything else in this project is
stdlib-only). If it's missing, functions here raise a clear error rather than
importing it at module load time, so the rest of the tool still works without it.
"""

import fnmatch
import hashlib
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .util import logger, safe_copy2

# Top-level folders under the instance root that this tool already manages
# elsewhere (mods/downloads/profiles) or that are purely transient/regenerable
# (crash dumps, logs, browser cache, temp) - never treated as "customization".
EXCLUDED_TOP_LEVEL = {"mods", "downloads", "profiles", "overwrite", "crashdumps", "logs", "webcache", "__temp__"}

# Noise that can appear anywhere but is runtime-generated, never a customization.
# .git/.svn/.hg: tools pulled via `git clone` (e.g. zEdit's script modules,
# often set up as submodules) carry full VCS metadata that isn't needed to
# run the tool, bloats the backup, and - worse - git marks files under
# .git/objects read-only on Windows, which breaks a plain copy2 overwrite
# during restore with a permission error.
IGNORE_PATTERNS = ("*/__pycache__/*", "*.pyc", "*/logs/*", "*/.git/*", "*/.git", "*/.svn/*", "*/.hg/*")

# Excluded when building a local Stock Game copy - Creation Club content is
# tracked as its own separate mod instead (see DEFAULT_VANILLA_PATTERNS in
# backup.py), confirmed via a real diff to never overlap with what Creation
# Kit itself adds to the game folder.
DEFAULT_GAME_COPY_EXCLUDE_PREFIXES = ("data/cc",)

# Extensions worth attempting a path rewrite on when overlaying extra files -
# tool settings files (DynDOLOD's .ini, PGPatcher/Synthesis's .json, ...) can
# embed absolute paths back to the source instance/game folder (e.g. an
# "mo2instancedir" or "DataPathOverride" field) that would otherwise silently
# point at the wrong location after a restore. Restricted to known text
# formats rather than every file, so a binary tool .exe/.dll never gets a
# UTF-8 decode attempted against it.
TEXT_CONFIG_EXTENSIONS = {".ini", ".json", ".txt", ".cfg", ".toml", ".yaml", ".yml", ".xml"}

HASH_CHUNK_SIZE = 1024 * 1024


def _require_py7zr():
    try:
        import py7zr
        return py7zr
    except ImportError as e:
        raise RuntimeError(
            "py7zr is required for MO2 setup (extracting the portable .7z). "
            "Install it with: pip install py7zr"
        ) from e


def _is_ignored(relative_path: str) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in IGNORE_PATTERNS)


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _stock_file_hashes(archive_path: Path) -> Dict[str, str]:
    """Extracts the whole stock MO2 release to a temp dir and hashes every
    file, keyed by its path relative to the archive root. Needed (not just a
    path listing) so we can also detect MODIFIED files, e.g. a patched
    ModOrganizer.exe or a replaced splash.png - not just newly added ones."""
    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_stockhash_") as tmp:
        tmp_dir = Path(tmp)
        try:
            extract_mo2_archive(archive_path, tmp_dir)
        except Exception as e:
            logger.error("Failed to extract %s for comparison: %s", archive_path, e)
            return {}
        return {
            f.relative_to(tmp_dir).as_posix(): _hash_file(f)
            for f in tmp_dir.rglob("*") if f.is_file()
        }


_GAME_PATH_RE = re.compile(r"gamePath=@ByteArray\((.*?)\)")


def _detect_game_copy_folder(instance_root: Path) -> Optional[str]:
    """If ModOrganizer.ini's gamePath points inside the instance itself (a
    local copy of the game, as opposed to pointing at the real Steam/GOG
    install), returns that top-level folder name so it's excluded from the
    customization scan - it's the game, not an MO2 customization."""
    ini_path = instance_root / "ModOrganizer.ini"
    if not ini_path.is_file():
        return None
    match = _GAME_PATH_RE.search(ini_path.read_text(encoding="utf-8", errors="ignore"))
    if not match:
        return None
    game_path = Path(match.group(1).replace("\\\\", "\\"))
    try:
        relative = game_path.relative_to(instance_root)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def find_extra_or_modified_files(instance_root: Path, archive_path: Path) -> List[Path]:
    """Every file anywhere under instance_root that's either not part of the
    stock MO2 release at all (added plugins, DLLs, the tools/ folder, a new
    splash.png, ...) or present in both but with different content (a patched
    ModOrganizer.exe, a replaced splash.png, ...). mods/, downloads/,
    profiles/, and transient folders are excluded - those are handled
    elsewhere or aren't meaningful customizations."""
    stock_hashes = _stock_file_hashes(archive_path)
    excluded = set(EXCLUDED_TOP_LEVEL)
    game_copy_folder = _detect_game_copy_folder(instance_root)
    if game_copy_folder:
        excluded.add(game_copy_folder.lower())

    extra_or_modified = []
    for file_path in instance_root.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(instance_root)
        if relative.parts and relative.parts[0].lower() in excluded:
            continue
        relative_posix = relative.as_posix()
        if relative_posix == "ModOrganizer.ini" or _is_ignored(relative_posix):
            continue
        stock_hash = stock_hashes.get(relative_posix)
        if stock_hash is None or stock_hash != _hash_file(file_path):
            extra_or_modified.append(file_path)
    return extra_or_modified


def find_game_copy_extra_files(game_copy_dir: Path, real_game_path: Path) -> List[Path]:
    """Same idea as find_extra_or_modified_files, but for a local "Stock Game"
    copy (gamePath pointed inside the instance rather than at the real Steam/GOG
    install) instead of the MO2 release itself - diffs it against the real game
    install directly. Catches things like a Creation Kit patcher (e.g. CK
    Platform Extended) whose files were dropped straight into the local copy,
    on top of whatever Steam/GOG installed. Files present in real_game_path but
    missing from the local copy are NOT reported - that's an intentional
    exclusion (e.g. Creation Club content moved out into its own mod), not a
    customization to capture."""
    if not game_copy_dir.is_dir() or not real_game_path.is_dir():
        return []
    real_hashes = {
        f.relative_to(real_game_path).as_posix(): _hash_file(f)
        for f in real_game_path.rglob("*") if f.is_file()
    }
    extra_or_modified = []
    for file_path in game_copy_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative_posix = file_path.relative_to(game_copy_dir).as_posix()
        if _is_ignored(relative_posix):
            continue
        real_hash = real_hashes.get(relative_posix)
        if real_hash is None or real_hash != _hash_file(file_path):
            extra_or_modified.append(file_path)
    return extra_or_modified


def create_stock_game_copy(
    real_game_path: Path,
    destination: Path,
    exclude_prefixes: Sequence[str] = DEFAULT_GAME_COPY_EXCLUDE_PREFIXES,
) -> int:
    """Copies real_game_path's files into destination (a local "Stock Game"
    folder inside the MO2 instance), so MO2 and every mod install only ever
    touch that local copy - never the real Steam/GOG install - and so Steam
    updating or verifying the game later doesn't change files out from under
    an in-progress modding setup. Creation Club content is excluded by
    default since it's tracked as its own separate mod instead (see
    DEFAULT_VANILLA_PATTERNS in backup.py) - confirmed via a real diff that
    Creation Kit's own install never touches that content, so the exclusion
    stays correct whether or not Creation Kit is installed."""
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for file_path in real_game_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(real_game_path)
        if any(relative.as_posix().lower().startswith(prefix) for prefix in exclude_prefixes):
            continue
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        safe_copy2(file_path, destination_path)
        copied += 1
    return copied


def extract_mo2_archive(archive_path: Path, destination: Path) -> None:
    py7zr = _require_py7zr()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            archive.extractall(path=str(destination))
    except PermissionError as e:
        raise RuntimeError(
            f"Could not overwrite files in {destination} ({e.filename or e}) - is MO2 (or a helper "
            "process like usvfs_proxy_*.exe) still running against this instance? Close it and try again."
        ) from e
    logger.info("Extracted %s into %s", archive_path, destination)


def _both_slash_forms(path: str) -> List[str]:
    """An instance/game path can appear either as a plain forward-slash string
    or as a backslash-escaped one (Qt's ini writer doubles backslashes) -
    generate both so substitution catches either form."""
    forward = path.replace("\\", "/")
    backslash_escaped = path.replace("/", "\\").replace("\\", "\\\\")
    return [forward, backslash_escaped]


def rewrite_ini_paths(
    ini_text: str,
    old_instance_root: str,
    new_instance_root: str,
    new_game_path: str,
    game_copy_folder_name: Optional[str] = None,
    keep_local_game_copy: bool = False,
) -> str:
    """Redirects an old ModOrganizer.ini to a new instance location.

    This is a scoped literal substring substitution, not a full ini/QSettings
    parse - deliberately so, since most of the file is opaque Qt binary blobs
    (@ByteArray/@Variant widget geometry, colors, pickled objects) that must be
    left byte-for-byte untouched. Every path field we care about (gamePath,
    customExecutables) turned out to be plain readable text, so literal
    substring replacement is both sufficient and much safer than trying to
    reproduce Qt's serialization.

    By default, gamePath (and anything under it, e.g. "...\\Stock
    Game\\Data" in a tool's argument) redirects to new_game_path instead of
    "<new_instance_root>\\Stock Game", since a plain restore points straight
    at your real game install rather than keeping a local copy of it. Pass
    keep_local_game_copy=True (paired with setup_mo2's
    create_local_game_copy) to skip that redirect instead - the generic
    instance-root substitution below then naturally rewrites
    "<old_root>\\Stock Game" to "<new_root>\\Stock Game" on its own, pointing
    at the freshly-created local copy.
    """
    result = ini_text

    if game_copy_folder_name and not keep_local_game_copy:
        for old_form, new_form in zip(
            _both_slash_forms(f"{old_instance_root.rstrip(chr(92)).rstrip('/')}/{game_copy_folder_name}"),
            _both_slash_forms(new_game_path),
        ):
            result = result.replace(old_form, new_form)

    for old_form, new_form in zip(
        _both_slash_forms(old_instance_root),
        _both_slash_forms(new_instance_root),
    ):
        result = result.replace(old_form, new_form)

    return result


@dataclass
class SetupResult:
    extracted: bool
    ini_written: bool
    extra_files_restored: int
    config_paths_rewritten: int
    stock_game_files_restored: int
    stock_game_copy_created: Optional[str]
    tools_reconstructed: List[str]
    tools_failed: List[str]


def setup_mo2(
    archive_path: Optional[Path],
    target_root: Path,
    backup_zip: Path,
    new_game_path: str,
    nexus_api_key: Optional[str] = None,
    game_domain: Optional[str] = None,
    create_local_game_copy: bool = False,
    existing_downloads_dir: Optional[Path] = None,
) -> SetupResult:
    """Extracts the stock MO2 release into target_root, overlays the extra/
    modified files captured in backup_zip (plugins, a custom splash.png, a
    patched exe, ...), reconstructs any tools/<name> entries that matched a
    download source via their recipe, and writes a path-rewritten
    ModOrganizer.ini. Safe to run on an already-partially-populated target
    (e.g. after restore() has already staged mods/downloads/RIC data) -
    the stock archive has no mods/, downloads/, or profiles/ of its own.

    archive_path is optional - if omitted, MO2 itself is redownloaded from
    manifest.mo2_release_url (captured at backup time from MO2's own
    self-updater .meta), so no local copy needs to be pre-staged.

    new_game_path is always the real Steam/GOG install (needed either way -
    as the direct gamePath target, or as the source to copy from). With
    create_local_game_copy=True, a same-named local copy (e.g. "Stock Game")
    is built inside target_root instead, gamePath points there instead of at
    the real install, and the captured stock_game_extra_files (e.g. CK
    Platform Extended) are applied into that local copy rather than onto the
    real install - keeping the real Steam/GOG folder untouched and insulated
    from a later Steam update changing files mid-project.

    existing_downloads_dir, if given, is checked (by exact archive filename)
    before downloading any tool component from Nexus - see restore()'s
    parameter of the same name."""
    from . import install_recipe
    from .backup import EXTRA_FILES_DIRNAME, MANIFEST_FILENAME, MO2_INI_ARCNAME, STOCK_GAME_EXTRA_DIRNAME, BackupManifest
    from .nexus_api import NexusApi, download_file
    from .restore import resolve_source_url

    ini_written = False
    extra_count = 0
    stock_game_count = 0
    stock_game_copy_created: Optional[str] = None
    tools_reconstructed: List[str] = []
    tools_failed: List[str] = []
    nexus_api = NexusApi(nexus_api_key) if nexus_api_key else None

    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_setup_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(backup_zip, "r") as zf:
            zf.extractall(tmp_dir)

        manifest = BackupManifest.from_json((tmp_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))

        if archive_path is None:
            if not manifest.mo2_release_url:
                raise RuntimeError(
                    "No --archive given and this backup has no recorded MO2 release URL "
                    "(only captured if downloads/<mo2-archive>.meta existed at backup time). "
                    "Pass --archive explicitly."
                )
            logger.info("No local archive given - downloading MO2 itself from %s", manifest.mo2_release_url)
            archive_path = tmp_dir / "ModOrganizer2.7z"
            download_file(manifest.mo2_release_url, archive_path)

        extract_mo2_archive(archive_path, target_root)
        for sub in ("profiles", "mods", "downloads"):
            (target_root / sub).mkdir(parents=True, exist_ok=True)

        game_copy_folder_name = manifest.game_copy_folder_name or "Stock Game"
        local_game_copy_dir: Optional[Path] = None
        if create_local_game_copy:
            local_game_copy_dir = target_root / game_copy_folder_name
            copied = create_stock_game_copy(Path(new_game_path), local_game_copy_dir)
            stock_game_copy_created = str(local_game_copy_dir)
            logger.info(
                "Built a local game copy at %s from %s (%d file(s), Creation Club content excluded) - "
                "MO2 and every mod install will only ever touch this copy, never your real Steam/GOG install.",
                local_game_copy_dir, new_game_path, copied,
            )

        if manifest.has_mo2_ini:
            old_ini_path = tmp_dir / MO2_INI_ARCNAME
            if manifest.source_instance_root:
                old_ini_text = old_ini_path.read_text(encoding="utf-8")
                new_ini_text = rewrite_ini_paths(
                    old_ini_text, manifest.source_instance_root, str(target_root), new_game_path,
                    game_copy_folder_name=game_copy_folder_name, keep_local_game_copy=create_local_game_copy,
                )
                (target_root / "ModOrganizer.ini").write_text(new_ini_text, encoding="utf-8")
            else:
                logger.info(
                    "Backup has ModOrganizer.ini but no source_instance_root recorded - "
                    "copying as-is, paths will likely be wrong."
                )
                safe_copy2(old_ini_path, target_root / "ModOrganizer.ini")
            ini_written = True

        extra_root = tmp_dir / EXTRA_FILES_DIRNAME
        config_rewrite_count = 0
        if extra_root.is_dir():
            for file_path in extra_root.rglob("*"):
                if not file_path.is_file():
                    continue
                relative = file_path.relative_to(extra_root)  # e.g. plugins/mo2-download-manager/__init__.py or tools/SSEEdit/SSEEdit.exe
                if _is_ignored(relative.as_posix()):
                    # Backups taken before .git/.svn/.hg were excluded at
                    # capture time may still carry these - skip them here too
                    # rather than fail the whole restore on a locked git
                    # object file that isn't needed anyway.
                    continue
                destination = target_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)

                # Tool settings files (DynDOLOD_SSE.ini, PGPatcher's
                # settings.json, Synthesis's PipelineSettings.json, ...) can
                # embed absolute paths back to the source instance/game
                # folder - rewrite those the same way ModOrganizer.ini's own
                # paths get rewritten, so they point at this new location
                # instead of silently carrying the old one forward.
                rewritten = False
                if manifest.source_instance_root and file_path.suffix.lower() in TEXT_CONFIG_EXTENSIONS:
                    try:
                        original_text = file_path.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, ValueError):
                        original_text = None
                    if original_text is not None:
                        new_text = rewrite_ini_paths(
                            original_text, manifest.source_instance_root, str(target_root), new_game_path,
                            game_copy_folder_name=game_copy_folder_name, keep_local_game_copy=create_local_game_copy,
                        )
                        destination.write_text(new_text, encoding="utf-8")
                        rewritten = True
                        if new_text != original_text:
                            config_rewrite_count += 1
                if not rewritten:
                    safe_copy2(file_path, destination)
                extra_count += 1

        stock_game_root = tmp_dir / STOCK_GAME_EXTRA_DIRNAME
        if stock_game_root.is_dir():
            # These were dropped straight into the source's local game copy on
            # top of whatever Steam/GOG installed (e.g. a Creation Kit
            # patcher). With create_local_game_copy, applied into the local
            # copy just built above; otherwise applied onto new_game_path
            # itself, since a plain restore points gamePath at the real
            # install rather than keeping a separate local copy of it.
            # Purely additive against a real Steam/GOG install (nothing here
            # overwrites a vanilla game file - verified against the source at
            # backup time), but without create_local_game_copy it does mean
            # writing into that real install folder, not just the MO2 instance.
            overlay_target_dir = local_game_copy_dir or Path(new_game_path)
            for file_path in stock_game_root.rglob("*"):
                if not file_path.is_file():
                    continue
                relative = file_path.relative_to(stock_game_root)
                destination = overlay_target_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                safe_copy2(file_path, destination)
                stock_game_count += 1
            if stock_game_count:
                logger.info(
                    "Applied %d file(s) into %s (files your source instance had added on top of "
                    "the vanilla game install, e.g. a Creation Kit patcher).",
                    stock_game_count, overlay_target_dir,
                )

        for tool_entry in manifest.tools:
            # Some tools (e.g. DynDOLOD) are assembled from several separate
            # downloads - each component gets its own archive fetched and its
            # own recipe applied into the same tool_dir.
            failed_components: List[str] = []
            for component in tool_entry.components:
                try:
                    archive_path_dl = target_root / "downloads" / component.archive_name
                    existing_path = existing_downloads_dir / component.archive_name if existing_downloads_dir else None
                    if archive_path_dl.is_file() and archive_path_dl.stat().st_size > 0:
                        logger.info("'%s' already present in downloads/ - skipping redownload.", component.archive_name)
                    elif existing_path and existing_path.is_file() and existing_path.stat().st_size > 0:
                        logger.info("'%s' found in existing downloads folder - copying instead of redownloading.", component.archive_name)
                        archive_path_dl.parent.mkdir(parents=True, exist_ok=True)
                        safe_copy2(existing_path, archive_path_dl)
                    else:
                        source_url = resolve_source_url(
                            nexus_api, game_domain, component.nexus_mod_id, component.nexus_file_id, component.url,
                            component.archive_name,
                        )
                        if not source_url:
                            raise RuntimeError("no download link available (missing Nexus API key/game domain, or the URL no longer works)")
                        download_file(source_url, archive_path_dl)

                    tool_dir = target_root / "tools" / tool_entry.name
                    tool_dir.mkdir(parents=True, exist_ok=True)
                    recipe = [install_recipe.RecipeEntry(output_path=r["output_path"], archive_path=r["archive_path"]) for r in component.recipe]
                    install_recipe.apply_install_recipe(archive_path_dl, recipe, tool_dir)
                    # Any of this tool's unresolved files are already restored by the
                    # generic extra-files overlay above (same tools/<name>/<path>
                    # layout), since only recipe-covered files were excluded from it.
                except Exception as e:
                    logger.error("Failed to reconstruct part of tool '%s' from %s: %s", tool_entry.name, component.archive_name, e)
                    failed_components.append(component.archive_name)

            if failed_components:
                tools_failed.append(tool_entry.name)
            else:
                tools_reconstructed.append(tool_entry.name)

    logger.info(
        "MO2 setup done: extracted %s, ModOrganizer.ini %s, %d extra/modified file(s) restored "
        "(%d had absolute paths rewritten for the new location), %d stock-game file(s) restored, "
        "%d tool(s) reconstructed, %d tool(s) failed.",
        archive_path.name, "written" if ini_written else "not found in backup", extra_count,
        config_rewrite_count, stock_game_count, len(tools_reconstructed), len(tools_failed),
    )
    return SetupResult(
        extracted=True, ini_written=ini_written, extra_files_restored=extra_count,
        config_paths_rewritten=config_rewrite_count,
        stock_game_files_restored=stock_game_count,
        stock_game_copy_created=stock_game_copy_created,
        tools_reconstructed=tools_reconstructed, tools_failed=tools_failed,
    )
