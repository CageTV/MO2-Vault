"""Main window: a shared Nexus API key/game-domain bar, four tabs (Backup /
Restore / Finalize / Vault) mirroring cli.py's subcommands, and a persistent
log dock that shows every util.logger line (the same detail level as `-v`
CLI output) plus per-mod restore progress.
"""

import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..backup import DEFAULT_VANILLA_PATTERNS
from ..util import logger as backend_logger
from .log_handler import QtLogHandler
from .settings import SETTINGS_FILE, Settings
from .workers import (
    BackupWorker,
    FinalizeWorker,
    RestoreWorker,
    SetupAndRestoreWorker,
    SetupMo2Worker,
    VaultRestoreToPointWorker,
    VaultSnapshotWorker,
)


def _description_label(text: str) -> QLabel:
    """Small, muted one-liner shown under a field - for the "quick description
    under every option" the user asked for, kept visually secondary (smaller,
    gray) so it doesn't compete with the field labels themselves."""
    label = QLabel(text)
    font = label.font()
    font.setPointSize(max(font.pointSize() - 2, 7))
    label.setFont(font)
    label.setStyleSheet("color: gray;")
    label.setWordWrap(True)
    return label


def _path_row(
    layout: QVBoxLayout, label: str, picker: Callable[[], Optional[str]], initial: str = "",
    description: str = "",
) -> QLineEdit:
    row = QHBoxLayout()
    row.addWidget(QLabel(label), 0)
    edit = QLineEdit(initial)
    row.addWidget(edit, 1)

    def browse() -> None:
        path = picker()
        if path:
            edit.setText(path)

    button = QPushButton("Browse...")
    button.clicked.connect(browse)
    row.addWidget(button, 0)
    layout.addLayout(row)
    if description:
        layout.addWidget(_description_label(description))
    return edit


def _dir_picker(parent: QWidget) -> Callable[[], Optional[str]]:
    return lambda: QFileDialog.getExistingDirectory(parent, "Select Folder") or None


def _open_file_picker(parent: QWidget, filter_str: str) -> Callable[[], Optional[str]]:
    return lambda: QFileDialog.getOpenFileName(parent, "Select File", filter=filter_str)[0] or None


def _save_file_picker(parent: QWidget, filter_str: str) -> Callable[[], Optional[str]]:
    return lambda: QFileDialog.getSaveFileName(parent, "Save File", filter=filter_str)[0] or None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MO2 Modlist Vault")
        self.resize(820, 640)
        icon_path = Path(__file__).parent / "assets" / "icon.ico"
        if icon_path.is_file():
            from PySide6.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))

        self.settings = Settings()
        self._workers: list = []  # keep references alive while running

        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        root_layout.addWidget(self._build_nexus_bar())

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_backup_tab(), "Backup")
        self.tabs.addTab(self._build_restore_tab(), "Restore")
        self.tabs.addTab(self._build_finalize_tab(), "Finalize")
        self.tabs.addTab(self._build_vault_tab(), "Vault")

        self._build_log_dock()

        self.status_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(220)
        self.progress_bar.setVisible(False)
        self.statusBar().addPermanentWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress_bar, 0)

        self._attach_log_handler()

    # region shared Nexus bar
    def _build_nexus_bar(self) -> QGroupBox:
        box = QGroupBox("Nexus (shared across tabs)")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit(self.settings.nexus_api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.api_key_edit, 1)

        row.addWidget(QLabel("Game Domain:"))
        self.game_domain_edit = QLineEdit(self.settings.nexus_game_domain)
        self.game_domain_edit.setPlaceholderText("e.g. skyrimspecialedition")
        self.game_domain_edit.setMaximumWidth(220)
        row.addWidget(self.game_domain_edit, 0)
        outer.addLayout(row)

        utility_row = QHBoxLayout()
        close_mo2_button = QPushButton("Close MO2 Processes")
        close_mo2_button.setToolTip(
            "Force-closes MO2 and its helper processes (usvfs_proxy, nxmhandler, ...) - use this if "
            "a restore fails with a file-in-use/permission error."
        )
        close_mo2_button.clicked.connect(self._run_close_mo2_processes)
        utility_row.addWidget(close_mo2_button)

        clear_settings_button = QPushButton("Clear All Settings...")
        clear_settings_button.setToolTip(
            f"Wipes every remembered field and the API key ({SETTINGS_FILE}). "
            "Use before handing this tool to someone else."
        )
        clear_settings_button.clicked.connect(self._run_clear_settings)
        utility_row.addWidget(clear_settings_button)
        utility_row.addStretch(1)
        outer.addLayout(utility_row)

        return box
    # endregion

    # region log dock
    def _build_log_dock(self) -> None:
        dock = QDockWidget("Log", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    def _attach_log_handler(self) -> None:
        handler = QtLogHandler()
        handler.record_logged.connect(self.log_view.appendPlainText)
        backend_logger.addHandler(handler)
        backend_logger.setLevel(logging.INFO)
        self._log_handler = handler  # keep alive
    # endregion

    # region Backup tab
    def _build_backup_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.backup_instance_edit = _path_row(
            layout, "MO2 Instance Folder", _dir_picker(self), self.settings.backup_instance,
            "The MO2 instance folder that contains profiles/, mods/, and downloads/.",
        )
        # textChanged (not editingFinished) so this also fires when Browse... sets
        # the text programmatically, not just on manual typing + focus loss.
        self.backup_instance_edit.textChanged.connect(self._refresh_backup_profiles)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile"))
        self.backup_profile_combo = QComboBox()
        self.backup_profile_combo.setEditable(True)
        if self.settings.backup_profile:
            self.backup_profile_combo.addItem(self.settings.backup_profile)
        profile_row.addWidget(self.backup_profile_combo, 1)
        layout.addLayout(profile_row)
        layout.addWidget(_description_label(
            "Which profile to back up - fills in automatically once you pick an instance folder above."
        ))

        self.backup_output_edit = _path_row(
            layout, "Backup Output (.zip)", _save_file_picker(self, "Zip files (*.zip)"), self.settings.backup_output,
            "Where to save the backup file.",
        )
        self.backup_mo2_archive_edit = _path_row(
            layout, "MO2 Portable .7z", _open_file_picker(self, "7z files (*.7z)"),
            self.settings.backup_mo2_stock_archive,
            "Your original MO2 portable release - captures ModOrganizer.ini and any customizations "
            "(plugins, tools/, patched exe) so Setup MO2 can rebuild them later.",
        )
        self.backup_real_game_path_edit = _path_row(
            layout, "Real Game Path", _dir_picker(self), self.settings.backup_real_game_path,
            "Your real Steam/GOG install - only needed if gamePath points at a local copy inside the "
            "instance; captures anything dropped on top of vanilla (e.g. a Creation Kit patcher).",
        )

        max_bundle_row = QHBoxLayout()
        max_bundle_row.addWidget(QLabel("Max Bundle Size (MB)"))
        self.backup_max_bundle_edit = QLineEdit(self.settings.backup_max_bundle_mb)
        self.backup_max_bundle_edit.setPlaceholderText("blank = no cap")
        self.backup_max_bundle_edit.setMaximumWidth(100)
        max_bundle_row.addWidget(self.backup_max_bundle_edit, 0)
        max_bundle_row.addWidget(QLabel("mods with no download source larger than this are skipped, not bundled"), 1)
        layout.addLayout(max_bundle_row)

        vanilla_row = QHBoxLayout()
        vanilla_row.addWidget(QLabel("Vanilla Patterns"))
        self.backup_vanilla_edit = QLineEdit(
            self.settings.backup_vanilla_patterns or ",".join(DEFAULT_VANILLA_PATTERNS)
        )
        vanilla_row.addWidget(self.backup_vanilla_edit, 1)
        layout.addLayout(vanilla_row)

        self.backup_run_button = QPushButton("Create Backup")
        self.backup_run_button.clicked.connect(self._run_backup)
        layout.addWidget(self.backup_run_button)

        layout.addWidget(QLabel("Result:"))
        self.backup_summary = QTextEdit()
        self.backup_summary.setReadOnly(True)
        layout.addWidget(self.backup_summary, 1)

        return tab

    def _refresh_backup_profiles(self) -> None:
        instance_path = self.backup_instance_edit.text().strip()
        if not instance_path:
            return
        profiles_dir = Path(instance_path) / "profiles"
        if not profiles_dir.is_dir():
            return
        current = self.backup_profile_combo.currentText()
        self.backup_profile_combo.clear()
        self.backup_profile_combo.addItems(sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()))
        if current:
            self.backup_profile_combo.setCurrentText(current)

    def _run_backup(self) -> None:
        instance_path = self.backup_instance_edit.text().strip()
        output_path = self.backup_output_edit.text().strip()
        if not instance_path or not output_path:
            QMessageBox.warning(self, "Missing info", "Pick both an instance folder and an output file.")
            return

        worker = BackupWorker(
            instance_path, output_path, self.backup_profile_combo.currentText().strip(),
            self.backup_max_bundle_edit.text().strip(), self.backup_vanilla_edit.text().strip(),
            self.backup_mo2_archive_edit.text().strip(), self.backup_real_game_path_edit.text().strip(),
        )
        worker.finished_ok.connect(self._on_backup_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.backup_run_button, "Running backup...")

    def _on_backup_finished(self, manifest) -> None:
        from ..backup import MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER, MODE_DOWNLOAD, MODE_GAME_CONTENT, MODE_UNKNOWN

        bundled_archives = manifest.mods_with_mode(MODE_BUNDLED_ARCHIVE)
        bundled_folders = manifest.mods_with_mode(MODE_BUNDLED_FOLDER)
        oversized = [m for m in bundled_archives + bundled_folders if not m.bundled]
        game_content = manifest.mods_with_mode(MODE_GAME_CONTENT)
        unknown = manifest.mods_with_mode(MODE_UNKNOWN)
        lines = [
            f"Backed up profile '{manifest.profile_name}'",
            f"Re-downloadable: {len(manifest.mods_with_mode(MODE_DOWNLOAD))}",
            f"Bundled archives: {len(bundled_archives)}",
            f"Bundled folders: {len(bundled_folders)}",
            f"Vanilla/game-content: {len(game_content)}"
            + (f" ({', '.join(m.name for m in game_content)})" if game_content else ""),
            f"Tools reconstructed via recipe: {len(manifest.tools)}"
            + (f" ({', '.join(t.name for t in manifest.tools)})" if manifest.tools else ""),
            f"Extra/modified MO2 files: {len(manifest.extra_files)}",
            f"Stock-game overlay files: {len(manifest.stock_game_extra_files)}",
            f"Profile extra files: {len(manifest.profile_extra_files)}",
        ]
        if oversized:
            lines.append(f"Over size cap, NOT bundled: {', '.join(m.name for m in oversized)}")
        if unknown:
            lines.append(f"Skipped (no folder found): {', '.join(m.name for m in unknown)}")
        self.backup_summary.setPlainText("\n".join(lines))
        self._finish_worker(self.backup_run_button, "Backup complete.")
    # endregion

    # region Restore tab
    def _build_restore_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.restore_target_edit = _path_row(
            layout, "Target Instance Folder", _dir_picker(self), self.settings.restore_target,
            "Where to rebuild the modlist - usually a fresh/empty folder.",
        )
        self.restore_backup_edit = _path_row(
            layout, "Backup File (.zip)", _open_file_picker(self, "Zip files (*.zip)"), self.settings.restore_backup_zip,
            "The backup file created on the Backup tab.",
        )
        self.restore_mo2_archive_edit = _path_row(
            layout, "MO2 Portable .7z", _open_file_picker(self, "7z files (*.7z)"),
            self.settings.restore_mo2_archive,
            "Override for the MO2 release archive - leave blank to auto-download from the backup's "
            "recorded URL.",
        )
        self.restore_game_path_edit = _path_row(
            layout, "Game Path", _dir_picker(self), self.settings.restore_game_path,
            "Your real Steam/GOG install - used as gamePath directly, or as the source for a local "
            "Stock Game copy if the checkbox below is checked.",
        )
        self.restore_downloads_source_edit = _path_row(
            layout, "Existing Downloads Folder (optional)", _dir_picker(self),
            self.settings.restore_downloads_source,
            "Checked by filename before downloading anything from Nexus - e.g. a prior MO2/Wabbajack "
            "install's downloads/ folder, or a shared archive cache. Leave blank to always redownload.",
        )

        self.restore_stock_copy_check = QCheckBox(
            "Create local Stock Game copy (keeps MO2/mods off your real Steam/GOG install)"
        )
        self.restore_stock_copy_check.setChecked(self.settings.restore_create_stock_game_copy)
        layout.addWidget(self.restore_stock_copy_check)

        layout.addWidget(QLabel(
            "Setup MO2 extracts MO2 itself + tools + your customizations. Restore Mods downloads/\n"
            "reconstructs every mod directly from a recipe - no MO2 installer, no FOMOD dialogs.\n"
            "Afterwards, open MO2 once (just launch it) so it discovers plugins, then use Finalize."
        ))

        button_row = QHBoxLayout()
        self.setup_run_button = QPushButton("Setup MO2")
        self.setup_run_button.clicked.connect(self._run_setup)
        button_row.addWidget(self.setup_run_button)
        self.restore_run_button = QPushButton("Restore Mods")
        self.restore_run_button.clicked.connect(self._run_restore)
        button_row.addWidget(self.restore_run_button)
        self.setup_and_restore_run_button = QPushButton("Setup + Restore")
        self.setup_and_restore_run_button.clicked.connect(self._run_setup_and_restore)
        button_row.addWidget(self.setup_and_restore_run_button)
        layout.addLayout(button_row)

        layout.addWidget(QLabel("Result:"))
        self.restore_summary = QTextEdit()
        self.restore_summary.setReadOnly(True)
        layout.addWidget(self.restore_summary, 1)

        return tab

    def _restore_field_values(self):
        return (
            self.restore_target_edit.text().strip(),
            self.restore_backup_edit.text().strip(),
            self.restore_mo2_archive_edit.text().strip(),
            self.restore_game_path_edit.text().strip(),
            self.restore_downloads_source_edit.text().strip(),
        )

    def _run_setup(self) -> None:
        target, backup_zip, mo2_archive, game_path, downloads_source = self._restore_field_values()
        if not target or not backup_zip or not game_path:
            QMessageBox.warning(self, "Missing info", "Pick a target folder, backup file, and game path.")
            return
        worker = SetupMo2Worker(
            target, backup_zip, mo2_archive, game_path,
            self.api_key_edit.text().strip(), self.game_domain_edit.text().strip(),
            self.restore_stock_copy_check.isChecked(), downloads_source,
        )
        worker.finished_ok.connect(self._on_setup_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.setup_run_button, "Setting up MO2...")

    def _run_restore(self) -> None:
        target, backup_zip, _mo2_archive, game_path, downloads_source = self._restore_field_values()
        if not target or not backup_zip:
            QMessageBox.warning(self, "Missing info", "Pick a target folder and backup file.")
            return
        worker = RestoreWorker(
            target, backup_zip, self.game_domain_edit.text().strip(), self.api_key_edit.text().strip(), game_path,
            downloads_source,
        )
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_restore_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.restore_run_button, "Restoring mods...")

    def _run_setup_and_restore(self) -> None:
        target, backup_zip, mo2_archive, game_path, downloads_source = self._restore_field_values()
        if not target or not backup_zip or not game_path:
            QMessageBox.warning(self, "Missing info", "Pick a target folder, backup file, and game path.")
            return
        worker = SetupAndRestoreWorker(
            target, backup_zip, mo2_archive, game_path,
            self.api_key_edit.text().strip(), self.game_domain_edit.text().strip(),
            self.restore_stock_copy_check.isChecked(), downloads_source,
        )
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_setup_and_restore_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.setup_and_restore_run_button, "Running setup + restore...")

    def _on_setup_finished(self, result) -> None:
        self.restore_summary.setPlainText(self._format_setup_result(result))
        self._finish_worker(self.setup_run_button, "Setup MO2 complete.")

    def _on_restore_finished(self, result) -> None:
        self.restore_summary.setPlainText(self._format_restore_result(result))
        self._finish_worker(self.restore_run_button, "Restore complete.")

    def _on_setup_and_restore_finished(self, results) -> None:
        setup_result, restore_result = results
        text = self._format_setup_result(setup_result) + "\n\n" + self._format_restore_result(restore_result)
        self.restore_summary.setPlainText(text)
        self._finish_worker(self.setup_and_restore_run_button, "Setup + Restore complete.")

    @staticmethod
    def _format_setup_result(result) -> str:
        lines = [
            "=== Setup MO2 ===",
            f"Extracted: {result.extracted}",
            f"ModOrganizer.ini: {'written' if result.ini_written else 'not found in backup'}",
            f"Extra/modified files restored: {result.extra_files_restored}",
            f"Config paths rewritten: {result.config_paths_rewritten}",
        ]
        if result.stock_game_copy_created:
            lines.append(f"Local game copy: {result.stock_game_copy_created}")
        if result.stock_game_files_restored:
            lines.append(f"Stock-game overlay files applied: {result.stock_game_files_restored}")
        if result.tools_reconstructed:
            lines.append(f"Tools reconstructed: {', '.join(result.tools_reconstructed)}")
        if result.tools_failed:
            lines.append(f"Tools FAILED: {', '.join(result.tools_failed)}")
        return "\n".join(lines)

    @staticmethod
    def _format_restore_result(result) -> str:
        lines = [
            "=== Restore Mods ===",
            f"Fully reconstructed: {len(result.placed)}",
            f"Separators recreated: {len(result.separators_recreated)}",
            f"Empty placeholder slots (no content, order kept): {len(result.placeholder_slots)}",
        ]
        if result.game_content_skipped:
            lines.append(
                f"Game-content mods with an empty slot (pass a Game Path to reconstruct): "
                f"{', '.join(result.game_content_skipped)}"
            )
        if result.failed:
            lines.append(f"FAILED ({len(result.failed)}):")
            lines += [f"  - {f.name}: {f.reason}" for f in result.failed]
        lines.append(
            "\nOpen MO2 once (just launch it - nothing to click) so it discovers plugins, "
            "then use the Finalize tab."
        )
        return "\n".join(lines)
    # endregion

    # region Finalize tab
    def _build_finalize_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.finalize_target_edit = _path_row(
            layout, "Target Instance Folder", _dir_picker(self), self.settings.finalize_target,
            "The same instance you just restored into.",
        )
        self.finalize_backup_edit = _path_row(
            layout, "Backup File (.zip)", _open_file_picker(self, "Zip files (*.zip)"), self.settings.finalize_backup_zip,
            "The same backup file used for Setup MO2 / Restore Mods.",
        )

        layout.addWidget(QLabel(
            "Run this AFTER opening MO2 once post-restore (just launching it is enough).\n"
            "It reorders plugins.txt to match the backup - modlist.txt was already written by Restore."
        ))

        self.finalize_run_button = QPushButton("Finalize Restore")
        self.finalize_run_button.clicked.connect(self._run_finalize)
        layout.addWidget(self.finalize_run_button)

        layout.addWidget(QLabel("Result:"))
        self.finalize_summary = QTextEdit()
        self.finalize_summary.setReadOnly(True)
        layout.addWidget(self.finalize_summary, 1)

        return tab

    def _run_finalize(self) -> None:
        target = self.finalize_target_edit.text().strip() or self.restore_target_edit.text().strip()
        backup_zip = self.finalize_backup_edit.text().strip() or self.restore_backup_edit.text().strip()
        if not target or not backup_zip:
            QMessageBox.warning(self, "Missing info", "Pick a target folder and backup file.")
            return
        self.finalize_target_edit.setText(target)
        self.finalize_backup_edit.setText(backup_zip)
        worker = FinalizeWorker(target, backup_zip)
        worker.finished_ok.connect(self._on_finalize_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.finalize_run_button, "Finalizing...")

    def _on_finalize_finished(self, result) -> None:
        lines = [f"Reordered {result.reordered_plugins} plugin(s)."]
        if result.still_missing_plugins:
            lines.append(f"MO2 hasn't discovered these plugins yet ({len(result.still_missing_plugins)}):")
            lines += [f"  - {name}" for name in result.still_missing_plugins]
        else:
            lines.append("Plugin order matches the backup.")
        self.finalize_summary.setPlainText("\n".join(lines))
        self._finish_worker(self.finalize_run_button, "Finalize complete.")
    # endregion

    # region Vault tab
    def _build_vault_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.vault_instance_edit = _path_row(
            layout, "MO2 Instance Folder", _dir_picker(self), self.settings.vault_instance,
            "The MO2 instance folder that contains profiles/, mods/, and downloads/.",
        )
        self.vault_instance_edit.textChanged.connect(self._refresh_vault_profiles)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile"))
        self.vault_profile_combo = QComboBox()
        self.vault_profile_combo.setEditable(True)
        if self.settings.vault_profile:
            self.vault_profile_combo.addItem(self.settings.vault_profile)
        profile_row.addWidget(self.vault_profile_combo, 1)
        layout.addLayout(profile_row)
        layout.addWidget(_description_label(
            "Which profile to snapshot - fills in automatically once you pick an instance folder above."
        ))

        self.vault_folder_edit = _path_row(
            layout, "Vault Folder", _dir_picker(self), self.settings.vault_folder,
            "Where snapshots are stored - created automatically on the first snapshot. Reuse the same "
            "folder every time to build up a history.",
        )
        self.vault_mo2_archive_edit = _path_row(
            layout, "MO2 Portable .7z", _open_file_picker(self, "7z files (*.7z)"),
            self.settings.vault_mo2_stock_archive,
            "Your original MO2 portable release - same as the Backup tab's field.",
        )
        self.vault_real_game_path_edit = _path_row(
            layout, "Real Game Path", _dir_picker(self), self.settings.vault_real_game_path,
            "Your real Steam/GOG install - same as the Backup tab's field.",
        )

        self.vault_snapshot_button = QPushButton("Take Snapshot")
        self.vault_snapshot_button.clicked.connect(self._run_vault_snapshot)
        layout.addWidget(self.vault_snapshot_button)

        layout.addWidget(QLabel("History:"))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.vault_snapshot_list = QListWidget()
        self.vault_snapshot_list.currentItemChanged.connect(self._on_vault_snapshot_selected)
        splitter.addWidget(self.vault_snapshot_list)
        self.vault_snapshot_detail = QTextEdit()
        self.vault_snapshot_detail.setReadOnly(True)
        splitter.addWidget(self.vault_snapshot_detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        button_row = QHBoxLayout()
        self.vault_refresh_button = QPushButton("Refresh History")
        self.vault_refresh_button.clicked.connect(self._refresh_vault_history)
        button_row.addWidget(self.vault_refresh_button)
        self.vault_restore_button = QPushButton("Restore to This Point...")
        self.vault_restore_button.clicked.connect(self._run_vault_restore_to_point)
        self.vault_restore_button.setEnabled(False)
        button_row.addWidget(self.vault_restore_button)
        layout.addLayout(button_row)

        self._vault_snapshots_by_id: dict = {}
        if self.vault_folder_edit.text().strip():
            self._refresh_vault_history()

        return tab

    def _refresh_vault_profiles(self) -> None:
        instance_path = self.vault_instance_edit.text().strip()
        if not instance_path:
            return
        profiles_dir = Path(instance_path) / "profiles"
        if not profiles_dir.is_dir():
            return
        current = self.vault_profile_combo.currentText()
        self.vault_profile_combo.clear()
        self.vault_profile_combo.addItems(sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()))
        if current:
            self.vault_profile_combo.setCurrentText(current)

    def _run_vault_snapshot(self) -> None:
        instance_path = self.vault_instance_edit.text().strip()
        vault_path = self.vault_folder_edit.text().strip()
        if not instance_path or not vault_path:
            QMessageBox.warning(self, "Missing info", "Pick both an instance folder and a vault folder.")
            return
        worker = VaultSnapshotWorker(
            instance_path, vault_path, self.vault_profile_combo.currentText().strip(),
            "", "",  # max bundle size / vanilla patterns - vault snapshots always use the defaults
            self.vault_mo2_archive_edit.text().strip(), self.vault_real_game_path_edit.text().strip(),
        )
        worker.finished_ok.connect(self._on_vault_snapshot_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.vault_snapshot_button, "Taking vault snapshot...")

    def _on_vault_snapshot_finished(self, info) -> None:
        self._finish_worker(self.vault_snapshot_button, f"Snapshot '{info.snapshot_id}' complete.")
        self._refresh_vault_history()

    def _refresh_vault_history(self) -> None:
        vault_path = self.vault_folder_edit.text().strip()
        if not vault_path or not Path(vault_path).is_dir():
            return
        from .. import vault as vault_mod

        infos = vault_mod.list_snapshots(Path(vault_path))
        self.vault_snapshot_list.clear()
        self._vault_snapshots_by_id = {}
        for info in reversed(infos):  # newest first
            summary = info.changelog.summary() if info.changelog else "Initial snapshot"
            item = QListWidgetItem(f"{info.snapshot_id}  -  {summary}")
            item.setData(Qt.ItemDataRole.UserRole, info.snapshot_id)
            self.vault_snapshot_list.addItem(item)
            self._vault_snapshots_by_id[info.snapshot_id] = info

    def _on_vault_snapshot_selected(self, current, _previous) -> None:
        self.vault_restore_button.setEnabled(current is not None)
        if current is None:
            self.vault_snapshot_detail.clear()
            return
        snapshot_id = current.data(Qt.ItemDataRole.UserRole)
        info = self._vault_snapshots_by_id.get(snapshot_id)
        if info is None:
            return
        lines = [f"Snapshot: {info.snapshot_id}", f"Profile: {info.manifest.profile_name}", ""]
        cl = info.changelog
        if cl is None or cl.is_empty():
            lines.append("Initial snapshot - full baseline." if cl is None else "No changes since the previous snapshot.")
        else:
            if cl.mods_added:
                lines.append(f"Mods added ({len(cl.mods_added)}): {', '.join(cl.mods_added)}")
            if cl.mods_removed:
                lines.append(f"Mods removed ({len(cl.mods_removed)}): {', '.join(cl.mods_removed)}")
            if cl.mods_changed:
                lines.append(f"Mods updated ({len(cl.mods_changed)}): {', '.join(cl.mods_changed)}")
            if cl.enabled_changed:
                lines.append(f"Enabled/disabled toggled ({len(cl.enabled_changed)}): {', '.join(cl.enabled_changed)}")
            if cl.order_changed:
                lines.append("Load order changed.")
            if cl.plugins_added or cl.plugins_removed:
                lines.append(f"Plugins: +{len(cl.plugins_added)} / -{len(cl.plugins_removed)}")
            if cl.plugin_order_changed:
                lines.append("Plugin order changed.")
            if cl.tools_changed:
                lines.append(f"Tools updated ({len(cl.tools_changed)}): {', '.join(cl.tools_changed)}")
        if cl is not None:
            lines.append("")
            lines.append(
                f"New content stored this snapshot: {cl.new_blob_count} file(s), "
                f"{cl.new_blob_bytes / (1024 * 1024):.1f} MB"
            )
        self.vault_snapshot_detail.setPlainText("\n".join(lines))

    def _run_vault_restore_to_point(self) -> None:
        current = self.vault_snapshot_list.currentItem()
        if current is None:
            return
        snapshot_id = current.data(Qt.ItemDataRole.UserRole)
        vault_path = self.vault_folder_edit.text().strip()

        # A true one-click restore is anchored to THIS vault, so its own
        # Instance Folder / MO2 Archive / Real Game Path fields take priority
        # - the Restore tab's matching fields are just a fallback for when
        # the Vault tab's own fields haven't been filled in yet. Preferring
        # the Restore tab here was the wrong default: those fields are
        # shared/reused for any restore workflow, so a restore just run for
        # a *different* modlist (different instance, different MO2 archive)
        # leaves stale values sitting there - reusing them for this vault's
        # snapshot silently pointed setup_mo2 at the wrong MO2 archive.
        restore_target, _backup_zip, restore_mo2_archive, restore_game_path, downloads_source = self._restore_field_values()

        target = self.vault_instance_edit.text().strip() or restore_target
        mo2_archive = self.vault_mo2_archive_edit.text().strip() or restore_mo2_archive
        game_path = self.vault_real_game_path_edit.text().strip() or restore_game_path

        # Keep the Restore tab in sync so its fields reflect what actually
        # ran (useful if the user then wants to inspect/finalize from there).
        if target:
            self.restore_target_edit.setText(target)
        if game_path:
            self.restore_game_path_edit.setText(game_path)
        if mo2_archive:
            self.restore_mo2_archive_edit.setText(mo2_archive)

        if not target or not game_path:
            QMessageBox.warning(
                self, "Missing info",
                "Fill in the Target Instance Folder and Game Path on either the Restore tab "
                "or the Vault tab first - \"Restore to This Point\" reuses those fields.",
            )
            return

        # "Restore to This Point" should faithfully reproduce that snapshot's
        # setup, not depend on the Restore tab's checkbox being remembered -
        # the snapshot's own manifest already records whether the source
        # instance had a local Stock Game copy (gamePath pointed inside the
        # instance rather than at the real Steam/GOG install), so honor that
        # directly instead of silently falling back to "off" and losing the
        # gamePath redirect.
        info = self._vault_snapshots_by_id.get(snapshot_id)
        create_stock_copy = self.restore_stock_copy_check.isChecked()
        if info is not None and info.manifest.game_copy_folder_name:
            create_stock_copy = True
            self.restore_stock_copy_check.setChecked(True)

        output_zip = str(Path(tempfile.gettempdir()) / f"mo2_modlist_vault_{snapshot_id}.zip")
        worker = VaultRestoreToPointWorker(
            vault_path, snapshot_id, output_zip,
            target, mo2_archive, game_path,
            self.api_key_edit.text().strip(), self.game_domain_edit.text().strip(),
            create_stock_copy, downloads_source,
        )
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_vault_restore_to_point_finished)
        worker.failed.connect(self._on_worker_failed)
        self._start_worker(worker, self.vault_restore_button, f"Restoring to snapshot '{snapshot_id}'...")

    def _on_vault_restore_to_point_finished(self, results) -> None:
        setup_result, restore_result = results
        text = self._format_setup_result(setup_result) + "\n\n" + self._format_restore_result(restore_result)
        self.restore_summary.setPlainText(text)
        self._finish_worker(self.vault_restore_button, "Restore to snapshot complete.")
        self.tabs.setCurrentIndex(1)  # Restore tab, to show the summary above
    # endregion

    # region worker plumbing
    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _start_worker(self, worker, button: QPushButton, status_text: str) -> None:
        button.setEnabled(False)
        self.status_label.setText(status_text)
        self._log(f"=== {status_text} ===")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # indeterminate until/unless progress() fires
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        self._workers.append(worker)
        worker.start()

    def _on_progress(self, current: int, total: int, message: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(current)
        self.status_label.setText(message)
        self._log(f"[{current}/{total}] {message}")

    def _finish_worker(self, button: QPushButton, status_text: str) -> None:
        button.setEnabled(True)
        self.status_label.setText(status_text)
        self._log(f"=== {status_text} ===")
        self.progress_bar.setVisible(False)

    def _on_worker_failed(self, message: str) -> None:
        for button in (
            self.backup_run_button, self.setup_run_button, self.restore_run_button,
            self.setup_and_restore_run_button, self.finalize_run_button, self.vault_snapshot_button,
        ):
            button.setEnabled(True)
        self.vault_restore_button.setEnabled(self.vault_snapshot_list.currentItem() is not None)
        self.status_label.setText("Failed.")
        self._log(f"=== FAILED: {message} ===")
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "Error", message)
    # endregion

    # region utility buttons
    def _run_close_mo2_processes(self) -> None:
        from .. import process_utils

        killed = process_utils.kill_mo2_processes()
        message = f"Closed: {', '.join(killed)}" if killed else "No MO2 processes were running."
        self._log(f"=== {message} ===")
        self.status_label.setText(message)

    def _run_clear_settings(self) -> None:
        reply = QMessageBox.question(
            self, "Clear All Settings",
            f"This wipes every remembered field and the Nexus API key ({SETTINGS_FILE}).\n\n"
            "This cannot be undone. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.settings.clear_all()
        for edit in (
            self.api_key_edit, self.game_domain_edit,
            self.backup_instance_edit, self.backup_output_edit, self.backup_mo2_archive_edit,
            self.backup_real_game_path_edit, self.backup_max_bundle_edit,
            self.restore_target_edit, self.restore_backup_edit, self.restore_mo2_archive_edit,
            self.restore_game_path_edit, self.restore_downloads_source_edit,
            self.finalize_target_edit, self.finalize_backup_edit,
            self.vault_instance_edit, self.vault_folder_edit, self.vault_mo2_archive_edit,
            self.vault_real_game_path_edit,
        ):
            edit.clear()
        self.backup_vanilla_edit.setText(",".join(DEFAULT_VANILLA_PATTERNS))
        self.backup_profile_combo.clear()
        self.vault_profile_combo.clear()
        self.restore_stock_copy_check.setChecked(False)
        self._log("=== All settings cleared. ===")
        self.status_label.setText("Settings cleared.")
    # endregion

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.settings.nexus_api_key = self.api_key_edit.text().strip()
        self.settings.nexus_game_domain = self.game_domain_edit.text().strip()

        self.settings.backup_instance = self.backup_instance_edit.text().strip()
        self.settings.backup_profile = self.backup_profile_combo.currentText().strip()
        self.settings.backup_output = self.backup_output_edit.text().strip()
        self.settings.backup_mo2_stock_archive = self.backup_mo2_archive_edit.text().strip()
        self.settings.backup_real_game_path = self.backup_real_game_path_edit.text().strip()
        self.settings.backup_max_bundle_mb = self.backup_max_bundle_edit.text().strip()
        self.settings.backup_vanilla_patterns = self.backup_vanilla_edit.text().strip()

        self.settings.restore_target = self.restore_target_edit.text().strip()
        self.settings.restore_backup_zip = self.restore_backup_edit.text().strip()
        self.settings.restore_mo2_archive = self.restore_mo2_archive_edit.text().strip()
        self.settings.restore_game_path = self.restore_game_path_edit.text().strip()
        self.settings.restore_downloads_source = self.restore_downloads_source_edit.text().strip()
        self.settings.restore_create_stock_game_copy = self.restore_stock_copy_check.isChecked()

        self.settings.finalize_target = self.finalize_target_edit.text().strip()
        self.settings.finalize_backup_zip = self.finalize_backup_edit.text().strip()

        self.settings.vault_instance = self.vault_instance_edit.text().strip()
        self.settings.vault_profile = self.vault_profile_combo.currentText().strip()
        self.settings.vault_folder = self.vault_folder_edit.text().strip()
        self.settings.vault_mo2_stock_archive = self.vault_mo2_archive_edit.text().strip()
        self.settings.vault_real_game_path = self.vault_real_game_path_edit.text().strip()

        super().closeEvent(event)
