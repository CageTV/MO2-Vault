"""A vault holds many point-in-time snapshots of the same MO2 modlist, the
same way Time Machine/System Restore do: every snapshot is a complete,
independently-restorable backup, but any file whose content is identical to
one already in the vault is stored exactly once (content-addressed by SHA1),
no matter how many snapshots reference it. Re-snapshotting a 500GB modlist
weekly should cost roughly "whatever actually changed", not 500GB again.

Reuses backup._prepare_backup() - the exact same manifest-building and
"what raw content needs storing" logic create_backup() uses - so a vault
snapshot and a plain create_backup() zip never disagree about what belongs in
a backup. create_backup() itself is untouched.

Layout:
    <vault_root>/
        vault.json                     {schema_version, created_at, source_instance_root}
        blobs/<sha1[:2]>/<sha1>         content-addressed store, shared by every snapshot
        snapshots/<timestamp>/
            manifest.json               a BackupManifest - same shape as a plain backup's manifest.json
            content_index.json          {"<zip-relative-path>": "<blob-sha1>", ...}
            changelog.json               diff vs. the previous snapshot (null for the first one)

materialize_snapshot() turns a snapshot back into a normal, standalone backup
zip (byte-for-byte the same shape create_backup() produces), so setup_mo2(),
restore(), and finalize_restore() need zero changes to consume it.
"""

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .backup import (
    BUNDLED_ARCHIVES_DIRNAME,
    BUNDLED_FOLDERS_DIRNAME,
    DEFAULT_MAX_BUNDLE_SIZE_BYTES,
    DEFAULT_VANILLA_PATTERNS,
    EXTRA_FILES_DIRNAME,
    MANIFEST_FILENAME,
    MO2_INI_ARCNAME,
    MODE_BUNDLED_ARCHIVE,
    MODE_BUNDLED_FOLDER,
    MODE_DOWNLOAD,
    MODE_GAME_CONTENT,
    PROFILE_EXTRA_DIRNAME,
    RIC_SAVES_DIRNAME,
    STOCK_GAME_EXTRA_DIRNAME,
    UNRESOLVED_FILES_DIRNAME,
    BackupManifest,
    ModBackupEntry,
    _prepare_backup,
)
from .mo2_instance import Mo2Instance
from .util import logger, safe_copy2

VAULT_METADATA_FILENAME = "vault.json"
SNAPSHOT_MANIFEST_FILENAME = "manifest.json"
CONTENT_INDEX_FILENAME = "content_index.json"
CHANGELOG_FILENAME = "changelog.json"
HASH_CHUNK_SIZE = 1024 * 1024


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _blob_path(vault_root: Path, blob_hash: str) -> Path:
    return vault_root / "blobs" / blob_hash[:2] / blob_hash


@dataclass
class Changelog:
    mods_added: List[str] = field(default_factory=list)
    mods_removed: List[str] = field(default_factory=list)
    mods_changed: List[str] = field(default_factory=list)  # version/archive/recipe differs
    enabled_changed: List[str] = field(default_factory=list)
    order_changed: bool = False
    plugins_added: List[str] = field(default_factory=list)
    plugins_removed: List[str] = field(default_factory=list)
    plugin_order_changed: bool = False
    tools_changed: List[str] = field(default_factory=list)
    new_blob_count: int = 0
    new_blob_bytes: int = 0

    def is_empty(self) -> bool:
        return not (
            self.mods_added or self.mods_removed or self.mods_changed or self.enabled_changed
            or self.order_changed or self.plugins_added or self.plugins_removed
            or self.plugin_order_changed or self.tools_changed
        )

    def summary(self) -> str:
        if self.is_empty() and self.new_blob_count == 0:
            return "No changes"
        parts = []
        if self.mods_added:
            parts.append(f"+{len(self.mods_added)} mod(s)")
        if self.mods_removed:
            parts.append(f"-{len(self.mods_removed)} mod(s)")
        if self.mods_changed:
            parts.append(f"{len(self.mods_changed)} updated")
        if self.enabled_changed:
            parts.append(f"{len(self.enabled_changed)} enabled/disabled")
        if self.order_changed:
            parts.append("load order changed")
        if self.plugins_added or self.plugins_removed:
            parts.append(f"plugins +{len(self.plugins_added)}/-{len(self.plugins_removed)}")
        if self.plugin_order_changed:
            parts.append("plugin order changed")
        if self.tools_changed:
            parts.append(f"{len(self.tools_changed)} tool(s) updated")
        return ", ".join(parts) if parts else "No manifest changes"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(data: str) -> "Changelog":
        raw = json.loads(data)
        return Changelog(**raw)


def compute_changelog(previous: Optional[BackupManifest], current: BackupManifest) -> Changelog:
    """Pure diff between two already-built manifests - no filesystem access,
    no new capture-time logic."""
    changelog = Changelog()
    if previous is None:
        return changelog

    prev_by_name = {m.name: m for m in previous.mods}
    curr_by_name = {m.name: m for m in current.mods}

    changelog.mods_added = sorted(set(curr_by_name) - set(prev_by_name))
    changelog.mods_removed = sorted(set(prev_by_name) - set(curr_by_name))

    for name in sorted(set(curr_by_name) & set(prev_by_name)):
        prev_mod, curr_mod = prev_by_name[name], curr_by_name[name]
        # size_bytes matters here, not just version/archive_name/mode - a
        # bundled_folder/bundled_archive mod (no download source, so no
        # version at all) is the only case where content actually changing
        # underneath it (files added/removed inside the folder) has no other
        # signal in the manifest to catch it.
        if (
            (prev_mod.version, prev_mod.archive_name, prev_mod.mode, prev_mod.size_bytes)
            != (curr_mod.version, curr_mod.archive_name, curr_mod.mode, curr_mod.size_bytes)
        ):
            changelog.mods_changed.append(name)
        if prev_mod.enabled != curr_mod.enabled:
            changelog.enabled_changed.append(name)

    prev_order = [m.name for m in sorted(previous.mods, key=lambda m: m.priority)]
    curr_order = [m.name for m in sorted(current.mods, key=lambda m: m.priority)]
    common = [n for n in curr_order if n in prev_by_name]
    prev_common = [n for n in prev_order if n in curr_by_name]
    changelog.order_changed = common != prev_common

    prev_plugins = {p.name for p in previous.plugins}
    curr_plugins = {p.name for p in current.plugins}
    changelog.plugins_added = sorted(curr_plugins - prev_plugins)
    changelog.plugins_removed = sorted(prev_plugins - curr_plugins)
    prev_plugin_order = [p.name for p in sorted(previous.plugins, key=lambda p: p.priority)]
    curr_plugin_order = [p.name for p in sorted(current.plugins, key=lambda p: p.priority)]
    common_plugins = [n for n in curr_plugin_order if n in prev_plugins]
    prev_common_plugins = [n for n in prev_plugin_order if n in curr_plugins]
    changelog.plugin_order_changed = common_plugins != prev_common_plugins

    prev_tools = {t.name: t for t in previous.tools}
    curr_tools = {t.name: t for t in current.tools}
    for name in sorted(set(curr_tools) | set(prev_tools)):
        if name not in prev_tools or name not in curr_tools:
            changelog.tools_changed.append(name)
            continue
        prev_versions = sorted(c.version for c in prev_tools[name].components)
        curr_versions = sorted(c.version for c in curr_tools[name].components)
        if prev_versions != curr_versions:
            changelog.tools_changed.append(name)

    return changelog


@dataclass
class SnapshotInfo:
    snapshot_id: str
    created_at: str
    manifest: BackupManifest
    changelog: Optional[Changelog]


def _content_items(
    instance: Mo2Instance,
    manifest: BackupManifest,
    extra_files: List[Path],
    stock_game_extra_paths: List[Path],
    stock_game_dir: Optional[Path],
):
    """Yields (source_path, arcname) for every raw-content file a plain
    create_backup() zip would contain - the exact same enumeration as
    create_backup()'s zip-writing block, just yielding instead of zf.write()."""
    mo2_ini_path = instance.root / "ModOrganizer.ini"
    if manifest.has_mo2_ini:
        yield mo2_ini_path, MO2_INI_ARCNAME

    for file_path in extra_files:
        relative = file_path.relative_to(instance.root).as_posix()
        yield file_path, f"{EXTRA_FILES_DIRNAME}/{relative}"

    for file_path in stock_game_extra_paths:
        relative = file_path.relative_to(stock_game_dir).as_posix()
        yield file_path, f"{STOCK_GAME_EXTRA_DIRNAME}/{relative}"

    profile_dir = instance.profile_dir(manifest.profile_name)
    for relative_path in manifest.profile_extra_files:
        source = profile_dir / relative_path
        if source.is_file():
            yield source, f"{PROFILE_EXTRA_DIRNAME}/{relative_path}"

    if manifest.ric_game_folder:
        from . import ric_interop
        saves_dir = ric_interop.saves_dir_for(instance.root, manifest.ric_game_folder)
        for save_file in saves_dir.glob("*.json"):
            yield save_file, f"{RIC_SAVES_DIRNAME}/{save_file.name}"

    for mod_entry in manifest.mods_with_mode(MODE_BUNDLED_ARCHIVE):
        if not mod_entry.bundled:
            continue
        archive_path = instance.downloads_dir / mod_entry.archive_name
        if archive_path.is_file():
            yield archive_path, f"{BUNDLED_ARCHIVES_DIRNAME}/{mod_entry.archive_name}"

    for mod_entry in manifest.mods_with_mode(MODE_BUNDLED_FOLDER):
        if not mod_entry.bundled:
            continue
        mod_dir = instance.mod_dir(mod_entry.name)
        if not mod_dir.is_dir():
            continue
        for file_path in mod_dir.rglob("*"):
            if file_path.is_file():
                relative = file_path.relative_to(mod_dir).as_posix()
                yield file_path, f"{BUNDLED_FOLDERS_DIRNAME}/{mod_entry.name}/{relative}"

    for mod_entry in manifest.mods:
        if mod_entry.mode not in (MODE_DOWNLOAD, MODE_BUNDLED_ARCHIVE, MODE_GAME_CONTENT) or not mod_entry.unresolved_files:
            continue
        mod_dir = instance.mod_dir(mod_entry.name)
        for relative_path in mod_entry.unresolved_files:
            source = mod_dir / relative_path
            if source.is_file():
                yield source, f"{UNRESOLVED_FILES_DIRNAME}/{mod_entry.name}/{relative_path}"


def _load_vault_metadata(vault_root: Path) -> Optional[dict]:
    path = vault_root / VAULT_METADATA_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_snapshot_id(vault_root: Path) -> Optional[str]:
    snapshots_dir = vault_root / "snapshots"
    if not snapshots_dir.is_dir():
        return None
    ids = sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir() and (p / SNAPSHOT_MANIFEST_FILENAME).is_file())
    return ids[-1] if ids else None


def _load_snapshot_manifest(vault_root: Path, snapshot_id: str) -> BackupManifest:
    path = vault_root / "snapshots" / snapshot_id / SNAPSHOT_MANIFEST_FILENAME
    return BackupManifest.from_json(path.read_text(encoding="utf-8"))


def create_vault_snapshot(
    instance: Mo2Instance,
    vault_root: Path,
    profile_name: Optional[str] = None,
    max_bundle_size_bytes: Optional[int] = DEFAULT_MAX_BUNDLE_SIZE_BYTES,
    vanilla_patterns: Sequence[str] = DEFAULT_VANILLA_PATTERNS,
    mo2_stock_archive: Optional[Path] = None,
    real_game_path: Optional[Path] = None,
) -> SnapshotInfo:
    manifest, extra_files, stock_game_extra_paths, stock_game_dir = _prepare_backup(
        instance, profile_name, max_bundle_size_bytes, vanilla_patterns, mo2_stock_archive, real_game_path,
    )

    vault_root.mkdir(parents=True, exist_ok=True)
    (vault_root / "blobs").mkdir(exist_ok=True)
    (vault_root / "snapshots").mkdir(exist_ok=True)
    if not (vault_root / VAULT_METADATA_FILENAME).is_file():
        (vault_root / VAULT_METADATA_FILENAME).write_text(
            json.dumps({
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_instance_root": str(instance.root),
            }, indent=2),
            encoding="utf-8",
        )

    previous_snapshot_id = _latest_snapshot_id(vault_root)
    previous_manifest = _load_snapshot_manifest(vault_root, previous_snapshot_id) if previous_snapshot_id else None
    changelog = compute_changelog(previous_manifest, manifest)

    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Written into a temp dir under snapshots/ and only moved into its final
    # name once everything succeeded, so an interrupted/failed snapshot never
    # leaves a half-written entry for list_snapshots() to trip over.
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{snapshot_id}.partial_", dir=str(vault_root / "snapshots")))
    try:
        content_index: Dict[str, str] = {}
        new_blob_count = 0
        new_blob_bytes = 0

        for source_path, arcname in _content_items(instance, manifest, extra_files, stock_game_extra_paths, stock_game_dir):
            blob_hash = _hash_file(source_path)
            content_index[arcname] = blob_hash
            blob_path = _blob_path(vault_root, blob_hash)
            if not blob_path.is_file():
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                safe_copy2(source_path, blob_path)
                new_blob_count += 1
                new_blob_bytes += blob_path.stat().st_size

        changelog.new_blob_count = new_blob_count
        changelog.new_blob_bytes = new_blob_bytes

        (tmp_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
        (tmp_dir / CONTENT_INDEX_FILENAME).write_text(json.dumps(content_index, indent=2), encoding="utf-8")
        (tmp_dir / CHANGELOG_FILENAME).write_text(changelog.to_json(), encoding="utf-8")

        final_dir = vault_root / "snapshots" / snapshot_id
        tmp_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    logger.info(
        "Vault snapshot '%s' created: %d new blob(s) (%.1f MB), %d total content item(s). %s",
        snapshot_id, new_blob_count, new_blob_bytes / (1024 * 1024), len(content_index), changelog.summary(),
    )
    return SnapshotInfo(snapshot_id=snapshot_id, created_at=snapshot_id, manifest=manifest, changelog=changelog)


def list_snapshots(vault_root: Path) -> List[SnapshotInfo]:
    snapshots_dir = vault_root / "snapshots"
    if not snapshots_dir.is_dir():
        return []
    infos = []
    for snapshot_dir in sorted(snapshots_dir.iterdir()):
        manifest_path = snapshot_dir / SNAPSHOT_MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
        changelog_path = snapshot_dir / CHANGELOG_FILENAME
        changelog = Changelog.from_json(changelog_path.read_text(encoding="utf-8")) if changelog_path.is_file() else None
        infos.append(SnapshotInfo(
            snapshot_id=snapshot_dir.name, created_at=snapshot_dir.name, manifest=manifest, changelog=changelog,
        ))
    return infos


def materialize_snapshot(vault_root: Path, snapshot_id: str, output_zip: Path) -> Path:
    """Turns a vault snapshot back into a normal, standalone backup zip -
    byte-for-byte the same shape create_backup() produces, so setup_mo2(),
    restore(), and finalize_restore() need zero changes to consume it."""
    snapshot_dir = vault_root / "snapshots" / snapshot_id
    manifest_path = snapshot_dir / SNAPSHOT_MANIFEST_FILENAME
    content_index_path = snapshot_dir / CONTENT_INDEX_FILENAME
    if not manifest_path.is_file() or not content_index_path.is_file():
        raise FileNotFoundError(f"Snapshot '{snapshot_id}' not found in {vault_root}.")

    manifest_json = manifest_path.read_text(encoding="utf-8")
    content_index = json.loads(content_index_path.read_text(encoding="utf-8"))

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, manifest_json)
        for arcname, blob_hash in content_index.items():
            blob_path = _blob_path(vault_root, blob_hash)
            if not blob_path.is_file():
                raise FileNotFoundError(
                    f"Snapshot '{snapshot_id}' references blob {blob_hash} for '{arcname}', "
                    f"but it's missing from {vault_root / 'blobs'}."
                )
            zf.write(blob_path, arcname)

    logger.info("Materialized snapshot '%s' (%d file(s)) to %s", snapshot_id, len(content_index), output_zip)
    return output_zip
