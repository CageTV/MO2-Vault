"""
Snapshots an MO2 instance's modlist into a small, portable backup: which mods are
installed, in what order, enabled/disabled state, and (per mod) enough information
to get it back - a redownload source if one exists, otherwise the archive if it's
still sitting in downloads/, otherwise the installed mod's own files as a last
resort. Plus a copy of Remember Installation Choices' saved FOMOD answers.

Nothing here needs MO2 to be running - it all comes from files already on disk.
"""

import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import download_meta, meta_ini, ric_interop
from .mo2_instance import Mo2Instance, resolve_profile
from .modlist_txt import read_modlist, read_pluginlist
from .util import logger

_NEXUS_URL_DOMAIN_RE = re.compile(r"nexusmods\.com/([a-z0-9]+)/mods", re.IGNORECASE)


def _extract_nexus_domain(url: Optional[str]) -> Optional[str]:
    """Recovers the Nexus game-domain slug (e.g. 'skyrimspecialedition') from a
    mod's own recorded URL, so restore doesn't need it typed in by hand."""
    if not url:
        return None
    match = _NEXUS_URL_DOMAIN_RE.search(url)
    return match.group(1).lower() if match else None

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
RIC_SAVES_DIRNAME = "ric_saves"
BUNDLED_ARCHIVES_DIRNAME = "bundled_archives"
BUNDLED_FOLDERS_DIRNAME = "bundled_folders"
MO2_INI_ARCNAME = "ModOrganizer.ini"
# Not just plugins/ anymore - anything in the MO2 install that's new or
# modified vs. the stock release (tools/, a custom splash.png, a patched
# ModOrganizer.exe, extra DLLs, ...).
EXTRA_FILES_DIRNAME = "mo2_extra_files"
UNRESOLVED_FILES_DIRNAME = "unresolved_files"
# Files dropped straight into a local "Stock Game" copy on top of whatever
# Steam/GOG installed - e.g. a Creation Kit patcher like CK Platform Extended.
STOCK_GAME_EXTRA_DIRNAME = "stock_game_extra_files"
# Everything in the profile folder besides modlist.txt/plugins.txt (which
# restore regenerates itself from manifest.mods/plugins) and saves/ (gameplay
# progress, not modlist configuration - out of scope here): settings.ini,
# skyrim.ini/skyrimprefs.ini/skyrimcustom.ini, loadorder.txt, lockedorder.txt,
# plugingroups.txt, archives.txt, initweaks.ini, BethINI's own cache, etc.
PROFILE_EXTRA_DIRNAME = "profile_extra_files"
PROFILE_MANAGED_FILES = {"modlist.txt", "plugins.txt"}
PROFILE_EXCLUDED_TOP_LEVEL = {"saves"}

# How a mod will be brought back on restore, cheapest/smallest first.
MODE_DOWNLOAD = "download"           # redownload via Nexus id or generic URL
MODE_BUNDLED_ARCHIVE = "bundled_archive"  # no redownload source, but the archive is still in downloads/
MODE_BUNDLED_FOLDER = "bundled_folder"    # no source and no archive - bundle the installed folder itself
MODE_GAME_CONTENT = "game_content"   # Creation Club/Creation Kit/etc - Steam reinstalls this, never bundled
MODE_SEPARATOR = "separator"         # a modlist.txt group separator, not a real mod
MODE_UNKNOWN = "unknown"             # listed in modlist.txt but its mods/ folder is missing


# No cap by default - a mod with no redownload source gets bundled raw
# regardless of size, since there's no other way to preserve it (a real
# modlist can have several-GB custom mods/tool outputs with nothing else to
# fall back to). Pass a value explicitly (max_bundle_size_bytes / --max-bundle-mb)
# to cap it instead.
DEFAULT_MAX_BUNDLE_SIZE_BYTES: Optional[int] = None

# Mod names containing any of these (case-insensitive) are treated as vanilla/
# Steam-delivered content rather than an actual mod to back up - Steam reinstalls
# Creation Club content and default Creation Kit files into the game folder
# automatically for anyone who owns them, so there's nothing to preserve here.
DEFAULT_VANILLA_PATTERNS = ("creation club", "creation kit")


@dataclass
class ModBackupEntry:
    name: str
    priority: int
    enabled: bool
    mode: str
    nexus_mod_id: Optional[int] = None
    nexus_file_id: Optional[int] = None
    version: Optional[str] = None
    url: Optional[str] = None
    archive_name: Optional[str] = None
    file_display_name: Optional[str] = None
    # Only meaningful for bundled_archive/bundled_folder: how big the thing being
    # bundled is, and whether it actually got written into the zip (a mod over the
    # size cap keeps its entry - and its place in modlist.txt order - but the
    # files themselves are left out; you handle those manually).
    size_bytes: int = 0
    bundled: bool = True
    # For download/bundled_archive: which files from the archive become which
    # installed files (by content hash, not path) - restoring just replays this,
    # no installer/MO2 process ever needed. [{"output_path":.., "archive_path":..}]
    recipe: List[Dict[str, str]] = field(default_factory=list)
    # Installed files that didn't hash-match anything in the archive (installer
    # side-effects, etc.) - bundled individually since the recipe can't cover them.
    unresolved_files: List[str] = field(default_factory=list)
    # Raw color=@Variant(...) from meta.ini, if a custom highlight color was set
    # (mods and separators both use this) - see meta_ini.read_color.
    color: Optional[str] = None


@dataclass
class PluginBackupEntry:
    name: str
    priority: int
    enabled: bool


@dataclass
class ToolArchiveComponent:
    """One of the (possibly several) downloads that together populate a
    tools/<name> folder - see ToolBackupEntry."""
    archive_name: str
    nexus_mod_id: Optional[int] = None
    nexus_file_id: Optional[int] = None
    url: Optional[str] = None
    version: Optional[str] = None
    recipe: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class ToolBackupEntry:
    """An external tool (SSEEdit, DynDOLOD, Synthesis, ...) living under
    tools/<name>/, outside MO2's mods/ system entirely - recognized because
    one or more downloads/*.meta modName(s) contain the folder name. Some
    tools (e.g. DynDOLOD) are assembled from several separate downloads, so
    this holds a list of components rather than a single archive - each
    reconstructed via its own install recipe, exactly like a mod's."""
    name: str
    components: List[ToolArchiveComponent] = field(default_factory=list)
    unresolved_files: List[str] = field(default_factory=list)


@dataclass
class BackupManifest:
    schema_version: int
    created_at: str
    profile_name: str
    ric_game_folder: Optional[str]
    game_name: Optional[str] = None
    nexus_game_domain: Optional[str] = None
    # The source instance's own root path, and whether ModOrganizer.ini/extra
    # files were captured - both needed by `setup-mo2` to rewrite the ini and
    # overlay your customizations onto a freshly-extracted MO2 release.
    source_instance_root: Optional[str] = None
    has_mo2_ini: bool = False
    # Where the stock MO2 release archive itself came from (e.g. MO2's own
    # self-updater writes a .meta with this next to the .7z) - lets `setup-mo2`
    # redownload MO2 itself instead of requiring a pre-staged local copy.
    mo2_release_url: Optional[str] = None
    extra_files: List[str] = field(default_factory=list)
    # Files dropped straight into a local "Stock Game" copy on top of whatever
    # Steam/GOG installed there (e.g. CK Platform Extended's files) - relative
    # to the game-copy folder itself. By default applied onto the target's
    # real game path by `setup-mo2` (not into a copy, since a fresh restore
    # doesn't create one unless asked to - see rewrite_ini_paths); with
    # --create-stock-game-copy, applied into the freshly-created local copy
    # instead.
    stock_game_extra_files: List[str] = field(default_factory=list)
    # The source instance's local game-copy folder name (e.g. "Stock Game"),
    # if ModOrganizer.ini's gamePath pointed at one - lets `setup-mo2` create
    # a same-named local copy on restore instead of guessing the name.
    game_copy_folder_name: Optional[str] = None
    # Everything in the profile folder besides modlist.txt/plugins.txt/saves -
    # see PROFILE_EXTRA_DIRNAME. Relative to the profile folder itself.
    profile_extra_files: List[str] = field(default_factory=list)
    mods: List[ModBackupEntry] = field(default_factory=list)
    plugins: List[PluginBackupEntry] = field(default_factory=list)
    tools: List[ToolBackupEntry] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(data: str) -> "BackupManifest":
        raw = json.loads(data)
        return BackupManifest(
            schema_version=raw["schema_version"],
            created_at=raw["created_at"],
            profile_name=raw["profile_name"],
            ric_game_folder=raw.get("ric_game_folder"),
            game_name=raw.get("game_name"),
            nexus_game_domain=raw.get("nexus_game_domain"),
            source_instance_root=raw.get("source_instance_root"),
            has_mo2_ini=raw.get("has_mo2_ini", False),
            mo2_release_url=raw.get("mo2_release_url"),
            extra_files=raw.get("extra_files", raw.get("extra_plugin_files", [])),
            stock_game_extra_files=raw.get("stock_game_extra_files", []),
            game_copy_folder_name=raw.get("game_copy_folder_name"),
            profile_extra_files=raw.get("profile_extra_files", []),
            mods=[ModBackupEntry(**m) for m in raw.get("mods", [])],
            plugins=[PluginBackupEntry(**p) for p in raw.get("plugins", [])],
            tools=[
                ToolBackupEntry(
                    name=t["name"],
                    components=[ToolArchiveComponent(**c) for c in t.get("components", [])],
                    unresolved_files=t.get("unresolved_files", []),
                )
                for t in raw.get("tools", [])
            ],
        )

    def mods_with_mode(self, mode: str) -> List[ModBackupEntry]:
        return [m for m in self.mods if m.mode == mode]


@dataclass
class _Classification:
    entry: ModBackupEntry
    game_name: Optional[str] = None
    nexus_game_domain: Optional[str] = None


def _classify_mod(
    instance: Mo2Instance, name: str, vanilla_patterns: Sequence[str] = DEFAULT_VANILLA_PATTERNS,
    real_game_data_dir: Optional[Path] = None, real_game_data_index: Optional[Dict[str, List[Path]]] = None,
) -> _Classification:
    """game_name/nexus_game_domain come from whichever archive's own .meta happens
    to carry them (MO2's meta files do; we have no live IOrganizer to ask
    directly) - None if this mod's classification found neither."""
    mod_dir = instance.mod_dir(name)
    if not mod_dir.is_dir():
        return _Classification(ModBackupEntry(name=name, priority=0, enabled=False, mode=MODE_UNKNOWN))

    mod_meta = meta_ini.read_mod_meta(mod_dir)
    # Only meaningful for MODE_DOWNLOAD/MODE_BUNDLED_ARCHIVE, which regenerate
    # meta.ini from scratch on restore (meta_ini.write_mod_meta) - a custom
    # highlight color would otherwise be silently dropped. MODE_BUNDLED_FOLDER
    # already carries it forward for free since the whole folder (including
    # its real meta.ini) gets copied verbatim.
    color = meta_ini.read_color(mod_dir)

    archive_meta = None
    archive_path: Optional[Path] = None
    if mod_meta and mod_meta.installation_file:
        # meta.ini's installationFile is just a bare filename (e.g. "Mod-123-1.7z"),
        # not a path - it always lives directly in downloads/. Path.__truediv__
        # still does the right thing even in the rare case it's already absolute
        # (an absolute right-hand operand replaces the left side entirely).
        archive_path = instance.downloads_dir / mod_meta.installation_file
        archive_meta = download_meta.read_meta(archive_path)

    # Only treat a mod as vanilla/Steam-delivered content once it's clear it has no
    # real redownload source - a name merely mentioning "Creation Club" (e.g. a
    # compatibility patch mod) must never override an actual Nexus source below.
    is_vanilla_content = any(pattern.lower() in name.lower() for pattern in vanilla_patterns)

    has_download_source = archive_meta and (
        archive_meta.nexus_mod_id and archive_meta.nexus_file_id or archive_meta.url
    )

    # Skip recipe computation only when there's truly no real Nexus/URL source AND
    # the name matches a vanilla pattern - a confirmed download source always wins
    # over a name-based guess, no matter what the mod is called.
    if archive_path and archive_path.is_file() and (has_download_source or not is_vanilla_content):
        from . import install_recipe
        recipe_result = install_recipe.compute_install_recipe(archive_path, mod_dir)

        if recipe_result.supported:
            recipe = [{"output_path": r.output_path, "archive_path": r.archive_path} for r in recipe_result.matched]
            if has_download_source:
                entry = ModBackupEntry(
                    name=name, priority=0, enabled=False, mode=MODE_DOWNLOAD,
                    nexus_mod_id=archive_meta.nexus_mod_id,
                    nexus_file_id=archive_meta.nexus_file_id,
                    version=archive_meta.version,
                    url=archive_meta.url,
                    archive_name=archive_meta.archive_path.name,
                    file_display_name=archive_meta.name,
                    recipe=recipe, unresolved_files=recipe_result.unresolved_output_paths,
                    color=color,
                )
                return _Classification(entry, archive_meta.game_name, _extract_nexus_domain(archive_meta.url))

            entry = ModBackupEntry(
                name=name, priority=0, enabled=False, mode=MODE_BUNDLED_ARCHIVE,
                archive_name=archive_path.name,
                version=mod_meta.version if mod_meta else None,
                size_bytes=archive_path.stat().st_size,
                recipe=recipe, unresolved_files=recipe_result.unresolved_output_paths,
                color=color,
            )
            return _Classification(entry, archive_meta.game_name if archive_meta else None)

        # Archive exists but couldn't be extracted (e.g. .rar - unsupported
        # format) - no recipe possible, fall through to bundling the folder as-is.
        logger.info("Could not build an install recipe for '%s' (%s) - bundling its files directly instead.",
                    name, archive_path.suffix)

    # Guarded by "not has_download_source" too, in case a mod has a vanilla-ish
    # name AND a real Nexus source AND its archive just failed to extract - the
    # real source still must not be discarded in that rare combination.
    if is_vanilla_content and not has_download_source:
        entry = ModBackupEntry(name=name, priority=0, enabled=False, mode=MODE_GAME_CONTENT, size_bytes=_dir_size(mod_dir))
        # This mod's files might just be Steam-installed content (e.g. Creation
        # Club) copied into MO2's mod system for load-order control - if so,
        # it's byte-identical to what's already sitting in the real game's
        # Data/ folder, and can be reconstructed by copying instead of either
        # bundling it raw (could be several GB) or dropping it (loses the
        # user's load-order placement for it entirely).
        if real_game_data_dir is not None:
            from . import install_recipe
            recipe_result = install_recipe.compute_folder_recipe(real_game_data_dir, mod_dir, real_game_data_index)
            if recipe_result.supported and recipe_result.matched:
                entry.recipe = [{"output_path": r.output_path, "archive_path": r.archive_path} for r in recipe_result.matched]
                entry.unresolved_files = recipe_result.unresolved_output_paths
        return _Classification(entry)

    entry = ModBackupEntry(
        name=name, priority=0, enabled=False, mode=MODE_BUNDLED_FOLDER,
        size_bytes=_dir_size(mod_dir),
    )
    return _Classification(entry)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def build_manifest(
    instance: Mo2Instance,
    profile_name: Optional[str] = None,
    vanilla_patterns: Sequence[str] = DEFAULT_VANILLA_PATTERNS,
    real_game_path: Optional[Path] = None,
) -> BackupManifest:
    resolved_profile = resolve_profile(instance, profile_name)
    profile_dir = instance.profile_dir(resolved_profile)

    manifest = BackupManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        created_at=datetime.now().isoformat(timespec="seconds"),
        profile_name=resolved_profile,
        ric_game_folder=ric_interop.find_existing_game_folder(instance.root),
    )

    profile_extra: List[str] = []
    for file_path in profile_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(profile_dir)
        if relative.parts[0].lower() in PROFILE_EXCLUDED_TOP_LEVEL:
            continue
        if len(relative.parts) == 1 and relative.parts[0].lower() in PROFILE_MANAGED_FILES:
            continue
        profile_extra.append(relative.as_posix())
    manifest.profile_extra_files = sorted(profile_extra)

    real_game_data_dir = (real_game_path / "Data") if real_game_path is not None else None
    # Lazily hashed once on first use (not up front) - most backups won't have
    # any vanilla-pattern mod folder at all, and this can be a large (tens of
    # GB) folder not worth indexing unless something actually needs it.
    real_game_data_index: Optional[Dict[str, List[Path]]] = None

    for priority, entry in enumerate(read_modlist(profile_dir)):
        if entry.is_separator:
            manifest.mods.append(ModBackupEntry(
                name=entry.name, priority=priority, enabled=entry.enabled, mode=MODE_SEPARATOR,
                color=meta_ini.read_color(instance.mod_dir(entry.name)),
            ))
            continue

        if (
            real_game_data_dir is not None and real_game_data_index is None
            and any(pattern.lower() in entry.name.lower() for pattern in vanilla_patterns)
        ):
            from . import install_recipe
            real_game_data_index = install_recipe._index_by_hash(real_game_data_dir)

        classification = _classify_mod(
            instance, entry.name, vanilla_patterns,
            real_game_data_dir=real_game_data_dir, real_game_data_index=real_game_data_index,
        )
        mod_entry = classification.entry
        mod_entry.priority = priority
        mod_entry.enabled = entry.enabled
        manifest.mods.append(mod_entry)
        if classification.game_name and not manifest.game_name:
            manifest.game_name = classification.game_name
        if classification.nexus_game_domain and not manifest.nexus_game_domain:
            manifest.nexus_game_domain = classification.nexus_game_domain

    for priority, plugin in enumerate(read_pluginlist(profile_dir)):
        manifest.plugins.append(PluginBackupEntry(
            name=plugin.name, priority=priority, enabled=plugin.enabled,
        ))

    return manifest


def _zip_directory(zf: zipfile.ZipFile, source_dir: Path, arc_prefix: str) -> None:
    if not source_dir.is_dir():
        return
    for file_path in source_dir.rglob("*"):
        if file_path.is_file():
            arcname = f"{arc_prefix}/{file_path.relative_to(source_dir).as_posix()}"
            zf.write(file_path, arcname)


def _prepare_backup(
    instance: Mo2Instance,
    profile_name: Optional[str],
    max_bundle_size_bytes: Optional[int],
    vanilla_patterns: Sequence[str],
    mo2_stock_archive: Optional[Path],
    real_game_path: Optional[Path],
) -> Tuple[BackupManifest, List[Path], List[Path], Optional[Path]]:
    """Builds the manifest and figures out every raw-content file that needs
    storing somewhere, without writing anything anywhere - shared by
    create_backup() (writes into a zip) and vault.create_vault_snapshot()
    (writes into a content-addressed blob store) so the two never drift on
    what counts as "content to capture". Returns (manifest, extra_files,
    stock_game_extra_paths, stock_game_dir)."""
    manifest = build_manifest(instance, profile_name, vanilla_patterns, real_game_path=real_game_path)
    manifest.source_instance_root = str(instance.root)

    for mod_entry in manifest.mods:
        if mod_entry.mode in (MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER):
            mod_entry.bundled = max_bundle_size_bytes is None or mod_entry.size_bytes <= max_bundle_size_bytes

    mo2_ini_path = instance.root / "ModOrganizer.ini"
    manifest.has_mo2_ini = mo2_ini_path.is_file()

    extra_files: List[Path] = []
    if mo2_stock_archive is not None:
        from . import download_meta as _download_meta
        # MO2's own self-updater writes downloads/<archive>.meta with a
        # directURL - check there first (where the user would actually have
        # it), then next to the archive itself as a fallback.
        for candidate_meta_source in (instance.downloads_dir / mo2_stock_archive.name, mo2_stock_archive):
            mo2_meta = _download_meta.read_meta(candidate_meta_source)
            if mo2_meta and mo2_meta.url:
                manifest.mo2_release_url = mo2_meta.url
                break

        from . import mo2_setup
        extra_files = mo2_setup.find_extra_or_modified_files(instance.root, mo2_stock_archive)

        from . import install_recipe, tool_sources
        for tool_name, tool_metas in tool_sources.find_tool_sources(instance).items():
            tool_dir = instance.root / "tools" / tool_name
            # A tool folder can be assembled from more than one download (e.g.
            # DynDOLOD = DynDOLOD + "DynDOLOD DLL NG and Scripts" + "DynDOLOD NG
            # - Settings Loader" + "DynDOLOD Resources SE") - try every candidate
            # download in turn, each time only checking whatever the previous
            # candidate(s) left unresolved, and keep whichever actually matched
            # at least one file.
            components: List[ToolArchiveComponent] = []
            covered: set = set()
            remaining: Optional[set] = None
            for tool_meta in tool_metas:
                recipe_result = install_recipe.compute_install_recipe(
                    tool_meta.archive_path, tool_dir, restrict_to=remaining
                )
                if not recipe_result.supported:
                    continue
                if recipe_result.matched:
                    components.append(ToolArchiveComponent(
                        archive_name=tool_meta.archive_path.name,
                        nexus_mod_id=tool_meta.nexus_mod_id,
                        nexus_file_id=tool_meta.nexus_file_id,
                        url=tool_meta.url,
                        version=tool_meta.version,
                        recipe=[{"output_path": r.output_path, "archive_path": r.archive_path} for r in recipe_result.matched],
                    ))
                    covered |= {(tool_dir / r.output_path).resolve() for r in recipe_result.matched}
                remaining = set(recipe_result.unresolved_output_paths)
                if not remaining:
                    break
            if not components:
                continue  # none of the candidates matched anything - leave it to the raw extra_files bundle below
            manifest.tools.append(ToolBackupEntry(
                name=tool_name,
                components=components,
                unresolved_files=sorted(remaining) if remaining else [],
            ))
            # Files the recipes already cover don't need to also ride along in
            # the raw extra_files bundle - only genuinely unresolved ones do,
            # and those are already still present in extra_files untouched.
            extra_files = [f for f in extra_files if f.resolve() not in covered]

        manifest.extra_files = sorted(
            f.relative_to(instance.root).as_posix() for f in extra_files
        )

    stock_game_extra_paths: List[Path] = []
    stock_game_dir: Optional[Path] = None
    from . import mo2_setup
    game_copy_folder = mo2_setup._detect_game_copy_folder(instance.root)
    manifest.game_copy_folder_name = game_copy_folder
    if real_game_path is not None:
        if game_copy_folder:
            stock_game_dir = instance.root / game_copy_folder
            stock_game_extra_paths = mo2_setup.find_game_copy_extra_files(stock_game_dir, real_game_path)
            manifest.stock_game_extra_files = sorted(
                f.relative_to(stock_game_dir).as_posix() for f in stock_game_extra_paths
            )
        else:
            logger.info(
                "--real-game-path given but ModOrganizer.ini's gamePath doesn't point inside "
                "the instance (no local game copy) - nothing to compare, skipping."
            )

    return manifest, extra_files, stock_game_extra_paths, stock_game_dir


def create_backup(
    instance: Mo2Instance,
    destination_zip: Path,
    profile_name: Optional[str] = None,
    max_bundle_size_bytes: Optional[int] = DEFAULT_MAX_BUNDLE_SIZE_BYTES,
    vanilla_patterns: Sequence[str] = DEFAULT_VANILLA_PATTERNS,
    mo2_stock_archive: Optional[Path] = None,
    real_game_path: Optional[Path] = None,
) -> BackupManifest:
    manifest, extra_files, stock_game_extra_paths, stock_game_dir = _prepare_backup(
        instance, profile_name, max_bundle_size_bytes, vanilla_patterns, mo2_stock_archive, real_game_path,
    )
    oversized = [
        m for m in manifest.mods
        if m.mode in (MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER) and not m.bundled
    ]
    mo2_ini_path = instance.root / "ModOrganizer.ini"

    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # Written after the size-cap pass above, so `bundled` reflects what's
        # actually in this zip, not just what we wanted to include.
        zf.writestr(MANIFEST_FILENAME, manifest.to_json())

        if manifest.has_mo2_ini:
            zf.write(mo2_ini_path, MO2_INI_ARCNAME)

        for file_path in extra_files:
            relative = file_path.relative_to(instance.root).as_posix()  # e.g. "plugins/mo2-download-manager/__init__.py" or "tools/SSEEdit/SSEEdit.exe"
            zf.write(file_path, f"{EXTRA_FILES_DIRNAME}/{relative}")

        for file_path in stock_game_extra_paths:
            relative = file_path.relative_to(stock_game_dir).as_posix()  # e.g. "ckpe_loader.exe"
            zf.write(file_path, f"{STOCK_GAME_EXTRA_DIRNAME}/{relative}")

        profile_dir = instance.profile_dir(manifest.profile_name)
        for relative_path in manifest.profile_extra_files:
            source = profile_dir / relative_path
            if source.is_file():
                zf.write(source, f"{PROFILE_EXTRA_DIRNAME}/{relative_path}")

        if manifest.ric_game_folder:
            saves_dir = ric_interop.saves_dir_for(instance.root, manifest.ric_game_folder)
            for save_file in saves_dir.glob("*.json"):
                zf.write(save_file, f"{RIC_SAVES_DIRNAME}/{save_file.name}")

        for mod_entry in manifest.mods_with_mode(MODE_BUNDLED_ARCHIVE):
            if not mod_entry.bundled:
                continue
            archive_path = instance.downloads_dir / mod_entry.archive_name
            if archive_path.is_file():
                zf.write(archive_path, f"{BUNDLED_ARCHIVES_DIRNAME}/{mod_entry.archive_name}")

        for mod_entry in manifest.mods_with_mode(MODE_BUNDLED_FOLDER):
            if not mod_entry.bundled:
                continue
            _zip_directory(zf, instance.mod_dir(mod_entry.name), f"{BUNDLED_FOLDERS_DIRNAME}/{mod_entry.name}")

        unresolved_total = 0
        for mod_entry in manifest.mods:
            if mod_entry.mode not in (MODE_DOWNLOAD, MODE_BUNDLED_ARCHIVE, MODE_GAME_CONTENT) or not mod_entry.unresolved_files:
                continue
            mod_dir = instance.mod_dir(mod_entry.name)
            for relative_path in mod_entry.unresolved_files:
                source = mod_dir / relative_path
                if source.is_file():
                    zf.write(source, f"{UNRESOLVED_FILES_DIRNAME}/{mod_entry.name}/{relative_path}")
                    unresolved_total += 1

    counts = {mode: len(manifest.mods_with_mode(mode)) for mode in
              (MODE_DOWNLOAD, MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER, MODE_GAME_CONTENT, MODE_SEPARATOR, MODE_UNKNOWN)}
    logger.info("Backup written to %s: %s, %d plugins", destination_zip, counts, len(manifest.plugins))
    if manifest.mo2_release_url:
        logger.info("MO2 release itself can be redownloaded from %s (no local copy needed for setup-mo2).", manifest.mo2_release_url)
    if manifest.tools:
        logger.info(
            "%d tool(s) under tools/ matched a download source and will be reconstructed via "
            "recipe instead of bundled raw: %s",
            len(manifest.tools), ", ".join(t.name for t in manifest.tools),
        )
    recipe_mods = [m for m in manifest.mods if m.recipe]
    logger.info(
        "%d mod(s) have an install recipe (reconstructed directly from their archive, no MO2 needed on restore); "
        "%d unresolved file(s) bundled individually.",
        len(recipe_mods), unresolved_total,
    )
    if manifest.has_mo2_ini:
        logger.info("Captured ModOrganizer.ini for later path rewriting.")
    if manifest.extra_files:
        logger.info(
            "Captured %d file(s) that are new or modified vs. the stock MO2 release "
            "(plugins, tools/, splash.png, a patched exe, etc).",
            len(manifest.extra_files),
        )
    if manifest.stock_game_extra_files:
        logger.info(
            "Captured %d file(s) dropped into the local Stock Game copy on top of the real "
            "game install (e.g. a Creation Kit patcher): %s",
            len(manifest.stock_game_extra_files), ", ".join(manifest.stock_game_extra_files),
        )
    if manifest.profile_extra_files:
        logger.info(
            "Captured %d profile file(s) beyond modlist.txt/plugins.txt (settings.ini, "
            "skyrim*.ini, load order/grouping, BethINI cache, etc; saves/ excluded).",
            len(manifest.profile_extra_files),
        )
    if counts[MODE_UNKNOWN]:
        logger.info(
            "%d mod(s) listed in modlist.txt have no matching folder in mods/ and were skipped entirely.",
            counts[MODE_UNKNOWN],
        )
    game_content = manifest.mods_with_mode(MODE_GAME_CONTENT)
    if game_content:
        total_gb = sum(m.size_bytes for m in game_content) / (1024 ** 3)
        with_recipe = [m for m in game_content if m.recipe]
        logger.info(
            "%d mod(s) matched vanilla-content patterns (%.1f GB) and were NOT bundled raw - "
            "%d have a recipe to reconstruct from your real game install (--game-path on restore): %s",
            len(game_content), total_gb, len(with_recipe), ", ".join(m.name for m in game_content),
        )
    if oversized:
        logger.info(
            "%d mod(s) exceeded the %.0f MB bundle cap and were NOT included in the zip "
            "(their modlist.txt slot is preserved, but you'll need to source these yourself): %s",
            len(oversized), max_bundle_size_bytes / (1024 * 1024),
            ", ".join(f"{m.name} ({m.size_bytes / (1024 * 1024):.0f} MB)" for m in oversized),
        )
    return manifest
