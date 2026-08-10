"""QThread wrappers around the four backend operations (backup, setup-mo2,
restore, finalize-restore) - each mirrors the exact call shape cli.py already
uses (see modlist_vault/cli.py's cmd_backup/cmd_setup_mo2/cmd_restore/
cmd_finalize_restore), just run off the Qt main thread so the UI stays
responsive. Detailed narration comes from util.logger (routed to the GUI's
log panel via log_handler.QtLogHandler, attached once at app startup) - these
workers only need to surface progress and the final result/error.
"""

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..mo2_instance import InvalidInstanceError, open_instance


class _BaseWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            result = self._do_work()
        except InvalidInstanceError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(repr(e))
        else:
            self.finished_ok.emit(result)

    def _do_work(self):
        raise NotImplementedError


class BackupWorker(_BaseWorker):
    def __init__(
        self,
        instance_path: str,
        output_path: str,
        profile_name: str,
        max_bundle_mb: str,
        vanilla_patterns: str,
        mo2_stock_archive: str,
        real_game_path: str,
    ) -> None:
        super().__init__()
        self._instance_path = instance_path
        self._output_path = output_path
        self._profile_name = profile_name or None
        self._max_bundle_mb = max_bundle_mb
        self._vanilla_patterns = vanilla_patterns
        self._mo2_stock_archive = mo2_stock_archive or None
        self._real_game_path = real_game_path or None

    def _do_work(self):
        from ..backup import DEFAULT_VANILLA_PATTERNS, create_backup

        instance = open_instance(Path(self._instance_path))
        patterns = (
            [p.strip() for p in self._vanilla_patterns.split(",") if p.strip()]
            if self._vanilla_patterns else DEFAULT_VANILLA_PATTERNS
        )
        max_bundle_bytes = int(self._max_bundle_mb) * 1024 * 1024 if self._max_bundle_mb.strip() else None
        return create_backup(
            instance, Path(self._output_path), profile_name=self._profile_name,
            max_bundle_size_bytes=max_bundle_bytes, vanilla_patterns=patterns,
            mo2_stock_archive=Path(self._mo2_stock_archive) if self._mo2_stock_archive else None,
            real_game_path=Path(self._real_game_path) if self._real_game_path else None,
        )


class SetupMo2Worker(_BaseWorker):
    def __init__(
        self,
        target_path: str,
        backup_zip: str,
        mo2_archive: str,
        game_path: str,
        api_key: str,
        game_domain: str,
        create_stock_game_copy: bool,
        downloads_source: str = "",
    ) -> None:
        super().__init__()
        self._target_path = target_path
        self._backup_zip = backup_zip
        self._mo2_archive = mo2_archive or None
        self._game_path = game_path
        self._api_key = api_key or None
        self._game_domain = game_domain or None
        self._create_stock_game_copy = create_stock_game_copy
        self._downloads_source = downloads_source or None

    def _do_work(self):
        from .. import mo2_setup

        return mo2_setup.setup_mo2(
            archive_path=Path(self._mo2_archive) if self._mo2_archive else None,
            target_root=Path(self._target_path),
            backup_zip=Path(self._backup_zip),
            new_game_path=self._game_path,
            nexus_api_key=self._api_key,
            game_domain=self._game_domain,
            create_local_game_copy=self._create_stock_game_copy,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
        )


class RestoreWorker(_BaseWorker):
    def __init__(
        self, target_path: str, backup_zip: str, game_domain: str, api_key: str, game_path: str,
        downloads_source: str = "",
    ) -> None:
        super().__init__()
        self._target_path = target_path
        self._backup_zip = backup_zip
        self._game_domain = game_domain or None
        self._api_key = api_key or None
        self._game_path = game_path or None
        self._downloads_source = downloads_source or None

    def _do_work(self):
        from .. import restore as restore_mod

        instance = open_instance(Path(self._target_path))
        return restore_mod.restore(
            instance, Path(self._backup_zip),
            game_domain=self._game_domain, nexus_api_key=self._api_key,
            real_game_path=Path(self._game_path) if self._game_path else None,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
            progress_callback=lambda current, total, message: self.progress.emit(current, total, message),
        )


class SetupAndRestoreWorker(_BaseWorker):
    """Convenience worker for the "Setup + Restore" one-click button - runs
    setup_mo2 then restore in sequence, surfacing both results together as
    (SetupResult, RestoreResult)."""

    def __init__(
        self,
        target_path: str,
        backup_zip: str,
        mo2_archive: str,
        game_path: str,
        api_key: str,
        game_domain: str,
        create_stock_game_copy: bool,
        downloads_source: str = "",
    ) -> None:
        super().__init__()
        self._target_path = target_path
        self._backup_zip = backup_zip
        self._mo2_archive = mo2_archive or None
        self._game_path = game_path
        self._api_key = api_key or None
        self._game_domain = game_domain or None
        self._create_stock_game_copy = create_stock_game_copy
        self._downloads_source = downloads_source or None

    def _do_work(self):
        from .. import mo2_setup
        from .. import restore as restore_mod

        setup_result = mo2_setup.setup_mo2(
            archive_path=Path(self._mo2_archive) if self._mo2_archive else None,
            target_root=Path(self._target_path),
            backup_zip=Path(self._backup_zip),
            new_game_path=self._game_path,
            nexus_api_key=self._api_key,
            game_domain=self._game_domain,
            create_local_game_copy=self._create_stock_game_copy,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
        )
        instance = open_instance(Path(self._target_path))
        restore_result = restore_mod.restore(
            instance, Path(self._backup_zip),
            game_domain=self._game_domain, nexus_api_key=self._api_key,
            real_game_path=Path(self._game_path) if self._game_path else None,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
            progress_callback=lambda current, total, message: self.progress.emit(current, total, message),
        )
        return (setup_result, restore_result)


class FinalizeWorker(_BaseWorker):
    def __init__(self, target_path: str, backup_zip: str) -> None:
        super().__init__()
        self._target_path = target_path
        self._backup_zip = backup_zip

    def _do_work(self):
        from .. import restore as restore_mod

        instance = open_instance(Path(self._target_path))
        return restore_mod.finalize_restore(instance, Path(self._backup_zip))


class VaultSnapshotWorker(_BaseWorker):
    def __init__(
        self,
        instance_path: str,
        vault_path: str,
        profile_name: str,
        max_bundle_mb: str,
        vanilla_patterns: str,
        mo2_stock_archive: str,
        real_game_path: str,
    ) -> None:
        super().__init__()
        self._instance_path = instance_path
        self._vault_path = vault_path
        self._profile_name = profile_name or None
        self._max_bundle_mb = max_bundle_mb
        self._vanilla_patterns = vanilla_patterns
        self._mo2_stock_archive = mo2_stock_archive or None
        self._real_game_path = real_game_path or None

    def _do_work(self):
        from .. import vault
        from ..backup import DEFAULT_VANILLA_PATTERNS

        instance = open_instance(Path(self._instance_path))
        patterns = (
            [p.strip() for p in self._vanilla_patterns.split(",") if p.strip()]
            if self._vanilla_patterns else DEFAULT_VANILLA_PATTERNS
        )
        max_bundle_bytes = int(self._max_bundle_mb) * 1024 * 1024 if self._max_bundle_mb.strip() else None
        return vault.create_vault_snapshot(
            instance, Path(self._vault_path), profile_name=self._profile_name,
            max_bundle_size_bytes=max_bundle_bytes, vanilla_patterns=patterns,
            mo2_stock_archive=Path(self._mo2_stock_archive) if self._mo2_stock_archive else None,
            real_game_path=Path(self._real_game_path) if self._real_game_path else None,
        )


class MaterializeSnapshotWorker(_BaseWorker):
    def __init__(self, vault_path: str, snapshot_id: str, output_zip: str) -> None:
        super().__init__()
        self._vault_path = vault_path
        self._snapshot_id = snapshot_id
        self._output_zip = output_zip

    def _do_work(self):
        from .. import vault

        return vault.materialize_snapshot(Path(self._vault_path), self._snapshot_id, Path(self._output_zip))


class VaultRestoreToPointWorker(_BaseWorker):
    """The Vault tab's "Restore to This Point" - a true one-click restore:
    materializes the snapshot, then runs setup_mo2 and restore against it in
    sequence, exactly like the Restore tab's "Setup + Restore" button. Reuses
    whatever the Restore tab's own fields are currently set to (target
    instance, game path, MO2 archive override, downloads source, create
    local game copy) so there's only one place to configure those."""

    def __init__(
        self,
        vault_path: str,
        snapshot_id: str,
        output_zip: str,
        target_path: str,
        mo2_archive: str,
        game_path: str,
        api_key: str,
        game_domain: str,
        create_stock_game_copy: bool,
        downloads_source: str = "",
    ) -> None:
        super().__init__()
        self._vault_path = vault_path
        self._snapshot_id = snapshot_id
        self._output_zip = output_zip
        self._target_path = target_path
        self._mo2_archive = mo2_archive or None
        self._game_path = game_path
        self._api_key = api_key or None
        self._game_domain = game_domain or None
        self._create_stock_game_copy = create_stock_game_copy
        self._downloads_source = downloads_source or None

    def _do_work(self):
        from .. import mo2_setup
        from .. import restore as restore_mod
        from .. import vault

        materialized_zip = vault.materialize_snapshot(Path(self._vault_path), self._snapshot_id, Path(self._output_zip))

        setup_result = mo2_setup.setup_mo2(
            archive_path=Path(self._mo2_archive) if self._mo2_archive else None,
            target_root=Path(self._target_path),
            backup_zip=materialized_zip,
            new_game_path=self._game_path,
            nexus_api_key=self._api_key,
            game_domain=self._game_domain,
            create_local_game_copy=self._create_stock_game_copy,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
        )
        instance = open_instance(Path(self._target_path))
        restore_result = restore_mod.restore(
            instance, materialized_zip,
            game_domain=self._game_domain, nexus_api_key=self._api_key,
            real_game_path=Path(self._game_path) if self._game_path else None,
            existing_downloads_dir=Path(self._downloads_source) if self._downloads_source else None,
            progress_callback=lambda current, total, message: self.progress.emit(current, total, message),
        )
        return (setup_result, restore_result)
