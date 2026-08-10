"""
Restoring a backup no longer needs MO2 to be running at all for the mods
themselves: every mod with a computed install recipe (see install_recipe.py)
gets its files reconstructed directly - download or copy the archive, extract
it, copy the recipe's matched files into mods/<name>/, write meta.ini, and add
the modlist.txt line ourselves. No installer dialogs, no FOMOD choices to
replay, nothing to click through.

restore() does the whole thing in one pass and writes the final modlist.txt
order/enabled state directly, since we placed every file ourselves and know
exactly what's there. The one thing that still benefits from opening MO2 once
(just launching it - no clicking, no choices) is plugins.txt: MO2 discovers
.esp/.esm/.esl files by scanning active mods' content itself on startup, so
finalize_restore() is kept as a small follow-up step that reorders whatever
MO2 already found to match the backup, the same conservative "never invent
lines" approach as before.
"""

import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import install_recipe, meta_ini, ric_interop
from .backup import (
    BUNDLED_ARCHIVES_DIRNAME,
    BUNDLED_FOLDERS_DIRNAME,
    MANIFEST_FILENAME,
    MODE_BUNDLED_ARCHIVE,
    MODE_BUNDLED_FOLDER,
    MODE_DOWNLOAD,
    MODE_GAME_CONTENT,
    MODE_SEPARATOR,
    MODE_UNKNOWN,
    PROFILE_EXTRA_DIRNAME,
    RIC_SAVES_DIRNAME,
    UNRESOLVED_FILES_DIRNAME,
    BackupManifest,
    ModBackupEntry,
)
from .mo2_instance import Mo2Instance, resolve_profile
from .modlist_txt import append_modlist_entry, read_pluginlist, rewrite_modlist, rewrite_pluginlist
from .nexus_api import NexusApi, download_file
from .util import logger, safe_copy2, safe_copytree

# (current_index, total, message)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class RestoreFailure:
    name: str
    reason: str


@dataclass
class RestoreResult:
    placed: List[str] = field(default_factory=list)  # fully reconstructed, no MO2 needed
    separators_recreated: List[str] = field(default_factory=list)
    # Game-content mods (e.g. "Creation Club Files") that couldn't be
    # reconstructed via recipe (no --game-path given, or its files didn't
    # match) - still get an empty placeholder folder + modlist.txt slot (see
    # placeholder_slots), just no content.
    game_content_skipped: List[str] = field(default_factory=list)
    # Kept a modlist.txt slot + empty mods/ folder, but has no real content -
    # either an oversized mod that exceeded the backup's size cap, or a
    # game-content mod (see game_content_skipped). Load order/position is
    # preserved either way; the user fills the folder in manually.
    placeholder_slots: List[str] = field(default_factory=list)
    failed: List[RestoreFailure] = field(default_factory=list)


def _load_manifest(backup_zip: Path, extract_dir: Path) -> BackupManifest:
    with zipfile.ZipFile(backup_zip, "r") as zf:
        zf.extractall(extract_dir)
    return BackupManifest.from_json((extract_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))


def resolve_source_url(
    nexus_api: Optional[NexusApi],
    game_domain: Optional[str],
    nexus_mod_id: Optional[int],
    nexus_file_id: Optional[int],
    fallback_url: Optional[str],
    archive_name: Optional[str] = None,
) -> Optional[str]:
    """Shared by mods and tools/ entries - anything with a (mod_id, file_id,
    url) triple resolves the same way."""
    if nexus_api and game_domain and nexus_mod_id and nexus_file_id:
        link = nexus_api.get_download_link(game_domain, nexus_mod_id, nexus_file_id)
        if link:
            return link
        # The exact recorded file_id can go stale (404) even while the mod page
        # itself is still up - authors delete/replace individual files more
        # often than you'd expect. Try to find whatever file_id replaced it
        # before falling back to a non-Nexus URL or giving up.
        current_file_id = nexus_api.find_current_file_id(game_domain, nexus_mod_id, nexus_file_id, archive_name)
        if current_file_id and current_file_id != nexus_file_id:
            link = nexus_api.get_download_link(game_domain, nexus_mod_id, current_file_id)
            if link:
                logger.info(
                    "Nexus file %d for mod %d was stale - resolved via current file %d instead.",
                    nexus_file_id, nexus_mod_id, current_file_id,
                )
                return link
        # The recorded url for a Nexus mod is just its page URL, not a download
        # link, so trying it only produces a misleading 403 - only fall through
        # for genuinely non-Nexus sources (e.g. GitHub).
        if fallback_url and "nexusmods.com" not in fallback_url.lower():
            return fallback_url
        return None
    if fallback_url and fallback_url.startswith(("http://", "https://")):
        return fallback_url
    return None


def _resolve_source_url(nexus_api: Optional[NexusApi], game_domain: Optional[str], mod_entry: ModBackupEntry) -> Optional[str]:
    return resolve_source_url(
        nexus_api, game_domain, mod_entry.nexus_mod_id, mod_entry.nexus_file_id, mod_entry.url, mod_entry.archive_name
    )


def restore(
    instance: Mo2Instance,
    backup_zip: Path,
    game_domain: Optional[str] = None,
    nexus_api_key: Optional[str] = None,
    profile_name: Optional[str] = None,
    real_game_path: Optional[Path] = None,
    existing_downloads_dir: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> RestoreResult:
    """real_game_path, if given, is the real Steam/GOG game install (same as
    setup-mo2's --game-path) - lets game-content mods with a captured recipe
    (e.g. "Creation Club Files") be reconstructed by copying from its Data/
    folder instead of just leaving an empty placeholder slot.

    existing_downloads_dir, if given, is checked (by exact archive filename)
    before downloading any mod from Nexus - lets a prior MO2/Wabbajack
    install's downloads/ folder (or any shared archive cache) supply files
    without hitting the network at all."""
    result = RestoreResult()
    nexus_api = NexusApi(nexus_api_key) if nexus_api_key else None
    real_game_data_dir = (real_game_path / "Data") if real_game_path is not None else None

    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_") as tmp:
        tmp_dir = Path(tmp)
        manifest = _load_manifest(backup_zip, tmp_dir)
        # create_if_missing=True since our restore never launches MO2 itself
        # (that's normally what creates a profile folder in the first place) -
        # a fresh target instance genuinely has no profiles/ subfolder yet.
        resolved_profile = resolve_profile(instance, profile_name or manifest.profile_name, create_if_missing=True)
        profile_dir = instance.profile_dir(resolved_profile)
        game_name = manifest.game_name or ""

        if manifest.profile_extra_files:
            profile_extra_root = tmp_dir / PROFILE_EXTRA_DIRNAME
            restored_profile_extra = 0
            for relative_path in manifest.profile_extra_files:
                source = profile_extra_root / relative_path
                if source.is_file():
                    destination = profile_dir / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    safe_copy2(source, destination)
                    restored_profile_extra += 1
            logger.info(
                "Restored %d profile file(s) beyond modlist.txt/plugins.txt (settings.ini, "
                "skyrim*.ini, load order/grouping, BethINI cache, etc).",
                restored_profile_extra,
            )
        # Nexus game-domain slugs (e.g. "skyrimspecialedition") were recovered from
        # mods' own recorded URLs at backup time - an explicit --game-domain always
        # wins, but usually nothing needs to be typed in by hand.
        game_domain = game_domain or manifest.nexus_game_domain

        if manifest.ric_game_folder:
            count = ric_interop.restore_saves_from(
                instance.root, manifest.ric_game_folder, tmp_dir / RIC_SAVES_DIRNAME
            )
            logger.info("Restored %d Remember Installation Choices save file(s) (kept for reference).", count)

        ordered_mods = sorted(manifest.mods, key=lambda m: m.priority)
        total = len(ordered_mods)

        for index, mod_entry in enumerate(ordered_mods):
            if should_cancel and should_cancel():
                logger.info("Restore cancelled after %d/%d mods.", index, total)
                break
            if progress_callback:
                progress_callback(index, total, f"Restoring {mod_entry.name}")

            if mod_entry.mode == MODE_SEPARATOR:
                # MO2 needs an actual (empty) folder under mods/ for a separator
                # to render at all - the modlist.txt line alone isn't enough.
                sep_dir = instance.mod_dir(mod_entry.name)
                sep_dir.mkdir(parents=True, exist_ok=True)
                if mod_entry.color:
                    meta_ini.write_separator_meta(sep_dir, mod_entry.color)
                append_modlist_entry(profile_dir, mod_entry.name, mod_entry.enabled)
                result.separators_recreated.append(mod_entry.name)
                continue

            if mod_entry.mode == MODE_GAME_CONTENT:
                # Some vanilla-pattern mods (e.g. "Creation Club Files") are
                # real managed mod folders the user built for load-order
                # control, not something Steam recreates as an MO2 mod - if a
                # recipe was captured (real_game_path given at backup time)
                # and we have somewhere to copy from now, reconstruct it for
                # real; otherwise it still gets its modlist.txt slot and an
                # empty folder so order/position isn't lost, just no content.
                game_content_dir = instance.mod_dir(mod_entry.name)
                game_content_dir.mkdir(parents=True, exist_ok=True)
                reconstructed = False
                if mod_entry.recipe and real_game_data_dir is not None:
                    try:
                        recipe = [install_recipe.RecipeEntry(output_path=r["output_path"], archive_path=r["archive_path"]) for r in mod_entry.recipe]
                        install_recipe.apply_folder_recipe(real_game_data_dir, recipe, game_content_dir)
                        if mod_entry.unresolved_files:
                            unresolved_root = tmp_dir / UNRESOLVED_FILES_DIRNAME / mod_entry.name
                            for relative_path in mod_entry.unresolved_files:
                                source = unresolved_root / relative_path
                                if source.is_file():
                                    destination = game_content_dir / relative_path
                                    destination.parent.mkdir(parents=True, exist_ok=True)
                                    safe_copy2(source, destination)
                        reconstructed = True
                    except Exception as e:
                        logger.error("Failed to reconstruct game-content mod '%s': %s", mod_entry.name, e)
                append_modlist_entry(profile_dir, mod_entry.name, mod_entry.enabled)
                if reconstructed:
                    result.placed.append(mod_entry.name)
                else:
                    result.game_content_skipped.append(mod_entry.name)
                    result.placeholder_slots.append(mod_entry.name)
                continue

            if mod_entry.mode == MODE_UNKNOWN:
                result.failed.append(RestoreFailure(
                    name=mod_entry.name, reason="had no source, archive, or files in the original backup"
                ))
                continue

            if mod_entry.mode in (MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER) and not mod_entry.bundled:
                # Still gets an empty folder + modlist.txt slot so its
                # load-order position isn't lost - only the content is
                # missing, which the user copies in themselves.
                oversized_dir = instance.mod_dir(mod_entry.name)
                oversized_dir.mkdir(parents=True, exist_ok=True)
                append_modlist_entry(profile_dir, mod_entry.name, mod_entry.enabled)
                result.placeholder_slots.append(mod_entry.name)
                result.failed.append(RestoreFailure(
                    name=mod_entry.name,
                    reason=f"exceeded the backup's size cap ({mod_entry.size_bytes / (1024 * 1024):.0f} MB) "
                           f"and wasn't included - its modlist.txt slot was kept (empty); "
                           f"you'll need to copy the files into mods/{mod_entry.name} yourself",
                ))
                continue

            try:
                if mod_entry.mode == MODE_DOWNLOAD:
                    archive_path = _download_one(
                        instance, nexus_api, game_domain, game_name, mod_entry, existing_downloads_dir
                    )
                    _reconstruct_mod(instance, tmp_dir, archive_path, mod_entry, game_name)
                elif mod_entry.mode == MODE_BUNDLED_ARCHIVE:
                    archive_path = _place_bundled_archive(instance, tmp_dir, mod_entry)
                    _reconstruct_mod(instance, tmp_dir, archive_path, mod_entry, game_name)
                elif mod_entry.mode == MODE_BUNDLED_FOLDER:
                    _place_bundled_folder(instance, tmp_dir, mod_entry)
                else:
                    continue
                append_modlist_entry(profile_dir, mod_entry.name, mod_entry.enabled)
                result.placed.append(mod_entry.name)
            except Exception as e:
                logger.error("Failed to restore %s: %s", mod_entry.name, e)
                result.failed.append(RestoreFailure(name=mod_entry.name, reason=str(e)))

        if progress_callback:
            progress_callback(total, total, "Writing mod order")

        # We placed every mod ourselves in this pass, so (unlike plugins.txt,
        # which MO2 must discover itself) we can write the final order/enabled
        # state directly - no need to wait for anything else to happen first.
        # Only MODE_UNKNOWN is excluded now - everything else (including
        # placeholder-only oversized/game-content slots) keeps its position.
        trackable = [m for m in manifest.mods if m.mode != MODE_UNKNOWN]
        slotted = set(result.placed) | set(result.separators_recreated) | set(result.placeholder_slots)
        desired_order = [m.name for m in trackable if m.name in slotted]
        enabled_by_name = {m.name: m.enabled for m in trackable}
        rewrite_modlist(profile_dir, desired_order, enabled_by_name)

    logger.info(
        "Restore finished: %d mod(s) fully reconstructed (no MO2 needed), %d separator(s), "
        "%d empty placeholder slot(s) kept (no content - oversized or game-content without a "
        "usable recipe), %d failed.",
        len(result.placed), len(result.separators_recreated),
        len(result.placeholder_slots), len(result.failed),
    )
    if result.failed:
        logger.info("Failed: %s", ", ".join(f"{f.name} ({f.reason})" for f in result.failed))
    logger.info(
        "Open MO2 once (just launch it, nothing to click) so it discovers plugins from the "
        "mods you now have, then run finalize-restore to match plugin order to the backup."
    )
    return result


def _download_one(
    instance: Mo2Instance, nexus_api: Optional[NexusApi], game_domain: Optional[str], game_name: str,
    mod_entry: ModBackupEntry, existing_downloads_dir: Optional[Path] = None,
) -> Path:
    from . import download_meta

    archive_name = mod_entry.archive_name or f"{mod_entry.name}.7z"
    archive_path = instance.downloads_dir / archive_name

    # Re-running restore against a partially-populated instance (e.g. after
    # fixing something and rerunning) shouldn't re-fetch everything that's
    # already sitting in downloads/ from a prior run.
    if archive_path.is_file() and archive_path.stat().st_size > 0:
        logger.info("'%s' already present in downloads/ - skipping redownload.", archive_name)
        meta_path = archive_path.with_name(f"{archive_path.name}.meta")
        if not meta_path.is_file():
            download_meta.write_meta(
                archive_path, game_name=game_name, mod_id=mod_entry.nexus_mod_id,
                file_id=mod_entry.nexus_file_id, name=mod_entry.file_display_name or archive_name,
                mod_name=mod_entry.name, version=mod_entry.version or "", url="",
            )
        return archive_path

    # An existing downloads folder (e.g. a prior MO2/Wabbajack install's
    # downloads/, or a shared archive cache) can supply the file without
    # touching the network at all - checked by exact archive filename.
    if existing_downloads_dir is not None:
        existing_path = existing_downloads_dir / archive_name
        if existing_path.is_file() and existing_path.stat().st_size > 0:
            logger.info("'%s' found in existing downloads folder - copying instead of redownloading.", archive_name)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            safe_copy2(existing_path, archive_path)
            meta_path = archive_path.with_name(f"{archive_path.name}.meta")
            if not meta_path.is_file():
                existing_meta_path = existing_path.with_name(f"{existing_path.name}.meta")
                if existing_meta_path.is_file():
                    safe_copy2(existing_meta_path, meta_path)
                else:
                    download_meta.write_meta(
                        archive_path, game_name=game_name, mod_id=mod_entry.nexus_mod_id,
                        file_id=mod_entry.nexus_file_id, name=mod_entry.file_display_name or archive_name,
                        mod_name=mod_entry.name, version=mod_entry.version or "", url="",
                    )
            return archive_path

    source_url = _resolve_source_url(nexus_api, game_domain, mod_entry)
    if not source_url:
        raise RuntimeError(
            "No download link available (missing Nexus API key/game domain, "
            "or the recorded URL no longer works)."
        )
    download_file(source_url, archive_path)

    download_meta.write_meta(
        archive_path,
        game_name=game_name,
        mod_id=mod_entry.nexus_mod_id,
        file_id=mod_entry.nexus_file_id,
        name=mod_entry.file_display_name or archive_name,
        mod_name=mod_entry.name,
        version=mod_entry.version or "",
        url=source_url,
    )
    return archive_path


def _place_bundled_archive(instance: Mo2Instance, tmp_dir: Path, mod_entry: ModBackupEntry) -> Path:
    source = tmp_dir / BUNDLED_ARCHIVES_DIRNAME / mod_entry.archive_name
    if not source.is_file():
        raise RuntimeError(f"Bundled archive {mod_entry.archive_name} missing from backup zip.")
    destination = instance.downloads_dir / mod_entry.archive_name
    safe_copy2(source, destination)
    return destination


def _reconstruct_mod(instance: Mo2Instance, tmp_dir: Path, archive_path: Path, mod_entry: ModBackupEntry, game_name: str) -> None:
    """Applies mod_entry's install recipe (extract archive, copy matched files),
    restores any individually-bundled unresolved files, and writes meta.ini -
    reproducing exactly what MO2's installer would have produced, without ever
    running it."""
    mod_dir = instance.mod_dir(mod_entry.name)
    mod_dir.mkdir(parents=True, exist_ok=True)

    recipe = [install_recipe.RecipeEntry(output_path=r["output_path"], archive_path=r["archive_path"]) for r in mod_entry.recipe]
    install_recipe.apply_install_recipe(archive_path, recipe, mod_dir)

    if mod_entry.unresolved_files:
        unresolved_root = tmp_dir / UNRESOLVED_FILES_DIRNAME / mod_entry.name
        for relative_path in mod_entry.unresolved_files:
            source = unresolved_root / relative_path
            if source.is_file():
                destination = mod_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                safe_copy2(source, destination)

    meta_ini.write_mod_meta(
        mod_dir,
        game_name=game_name,
        mod_id=mod_entry.nexus_mod_id,
        file_id=mod_entry.nexus_file_id,
        version=mod_entry.version or "",
        installation_file=archive_path.name,
        color=mod_entry.color,
    )


def _place_bundled_folder(instance: Mo2Instance, tmp_dir: Path, mod_entry: ModBackupEntry) -> None:
    source_dir = tmp_dir / BUNDLED_FOLDERS_DIRNAME / mod_entry.name
    if not source_dir.is_dir():
        raise RuntimeError(f"Bundled folder for {mod_entry.name} missing from backup zip.")
    destination = instance.mod_dir(mod_entry.name)
    safe_copytree(source_dir, destination, dirs_exist_ok=True)


@dataclass
class FinalizeResult:
    reordered_plugins: int
    still_missing_plugins: List[str] = field(default_factory=list)


def finalize_restore(instance: Mo2Instance, backup_zip: Path, profile_name: Optional[str] = None) -> FinalizeResult:
    """Run once after opening MO2 at least once post-restore (just launching it
    is enough - it scans active mods for .esp/.esm/.esl itself). Only reorders
    plugins.txt lines MO2 already discovered; never invents entries. modlist.txt
    doesn't need this anymore - restore() already wrote its final order/state."""
    with tempfile.TemporaryDirectory(prefix="mo2_modlist_vault_finalize_") as tmp:
        manifest = _load_manifest(backup_zip, Path(tmp))

    resolved_profile = resolve_profile(instance, profile_name or manifest.profile_name)
    profile_dir = instance.profile_dir(resolved_profile)

    desired_plugin_order = [p.name for p in sorted(manifest.plugins, key=lambda p: p.priority)]
    plugin_enabled = {p.name: p.enabled for p in manifest.plugins}

    current_plugin_names = {p.name for p in read_pluginlist(profile_dir)}
    still_missing = [name for name in desired_plugin_order if name not in current_plugin_names]

    rewrite_pluginlist(profile_dir, desired_plugin_order, plugin_enabled)

    logger.info(
        "Finalize done. %d plugin(s) reordered, %d still not discovered by MO2 yet: %s",
        len(desired_plugin_order) - len(still_missing), len(still_missing), still_missing,
    )
    return FinalizeResult(
        reordered_plugins=len(desired_plugin_order) - len(still_missing),
        still_missing_plugins=still_missing,
    )
