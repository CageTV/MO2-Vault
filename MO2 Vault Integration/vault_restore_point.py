"""One-click "Create Restore Point" for mo2-modlist-vault.

Deliberately does NOT embed mo2-modlist-vault's own logic (PySide6, py7zr,
the backup/vault code) inside MO2's Python environment - it shells out to
the separately-built mo2-modlist-vault.exe, same as ESLifier MO2
Integration shells out to ESLifier.exe. That keeps this plugin's only
dependencies mobase + Qt (both already provided by MO2 itself), avoids any
PySide6/PyQt version clash with MO2's embedded Qt, and reuses the exact,
already-validated `vault-snapshot` CLI command unchanged.

Restoring is intentionally NOT exposed here - restoring writes into the live
instance and needs MO2 closed (file-locking issues, see mo2-modlist-vault's
own restore.py/util.py), so it stays in the standalone GUI/CLI. This plugin
only ever takes read-only snapshots, which is safe to do with MO2 open.
"""

import os
import subprocess
from typing import List

import mobase  # type: ignore

try:
    from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import (
        QDialog, QFileDialog, QGridLayout, QLabel, QMessageBox,
        QPushButton, QToolBar, QToolButton, QVBoxLayout, QWidget,
    )
except ImportError:
    from PyQt5.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import (
        QDialog, QFileDialog, QGridLayout, QLabel, QMessageBox,
        QPushButton, QToolBar, QToolButton, QVBoxLayout, QWidget,
    )

TOOL_FOLDER_SETTING = "Tool Folder"
VAULT_FOLDER_SETTING = "Vault Folder"
EXE_NAME = "mo2-modlist-vault.exe"


class SnapshotWorker(QObject):
    """Runs `mo2-modlist-vault.exe vault-snapshot ...` off the UI thread.
    CREATE_NO_WINDOW hides the console window the exe would otherwise flash
    open (it's built with console=True so it also works from a normal
    terminal) - stdout/stderr are still captured fine via pipes either way."""

    finished_signal = pyqtSignal(bool, str)

    def __init__(self, exe_path: str, args: List[str]):
        super().__init__()
        self._exe_path = exe_path
        self._args = args

    def run(self) -> None:
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                [self._exe_path, *self._args],
                capture_output=True, text=True, creationflags=creationflags,
            )
            output = (result.stdout or "").strip()
            if result.stderr:
                output = (output + "\n" + result.stderr.strip()).strip()
            self.finished_signal.emit(result.returncode == 0, output or f"(exit code {result.returncode}, no output)")
        except Exception as e:
            self.finished_signal.emit(False, str(e))


class VaultRestorePointPlugin(mobase.IPluginTool):
    def __init__(self):
        super(VaultRestorePointPlugin, self).__init__()

    def name(self) -> str:
        return "MO2 Vault Integration"

    def localizedName(self) -> str:
        return self.tr("MO2 Vault Integration")

    def author(self) -> str:
        return "JulioV"

    def description(self) -> str:
        return self.tr("One-click Create Restore Point via mo2-modlist-vault.")

    def version(self) -> "mobase.VersionInfo":
        return mobase.VersionInfo(1, 0, 0, mobase.ReleaseType.FINAL)

    def requirements(self):
        return []

    def settings(self):
        return [
            mobase.PluginSetting(
                TOOL_FOLDER_SETTING,
                self.tr(f"Folder that holds {EXE_NAME}."),
                "",
            ),
            mobase.PluginSetting(
                VAULT_FOLDER_SETTING,
                self.tr("Vault folder to snapshot into (created on first snapshot if it doesn't exist)."),
                "",
            ),
        ]

    def displayName(self) -> str:
        return self.tr("Vault Restore Point")

    def tooltip(self) -> str:
        return self.tr("Create a vault restore point of the current modlist.")

    def icon(self) -> "QIcon":
        return self._icon

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("VaultRestorePointPlugin", text)

    def display(self) -> None:
        self.main_dialog.raise_()
        self.main_dialog.show()

    def init(self, organizer: "mobase.IOrganizer") -> bool:
        self._organizer = organizer
        icons_dir = os.path.join(os.path.dirname(__file__), "icons")
        self._icon_path = os.path.join(icons_dir, "vault_icon.ico")
        self._icon = QIcon(self._icon_path)

        # Spinner frames (a rotating amber ring, not the padlock itself
        # rotated - a rotated padlock reads as "upside down/broken" rather
        # than "busy", especially since a snapshot on a large modlist can
        # run for minutes, not seconds) cycled on the toolbar button while a
        # snapshot is running - the same purpose as the small moving
        # activity indicator in MO2's own status bar.
        self._spinner_frames = [
            QIcon(os.path.join(icons_dir, f"vault_icon_spin_{i}.png")) for i in range(8)
        ]
        self._spinner_index = 0
        self._spinner_timer = QTimer()
        self._spinner_timer.setInterval(120)
        self._spinner_timer.timeout.connect(self._advance_spinner)

        self.main_dialog = QDialog()
        self.main_dialog.setWindowIcon(self._icon)
        self.settings_dialog = QDialog()
        self.settings_dialog.setWindowIcon(self._icon)

        self.thread = QThread()
        self.worker = None
        self.running = False

        self._organizer.onUserInterfaceInitialized(self._create_ui)
        return True

    def _advance_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.toolbar_button.setIcon(self._spinner_frames[self._spinner_index])

    # region UI construction
    def _create_ui(self, *_args) -> None:
        self._build_main_dialog()
        self._build_settings_dialog()
        self._install_toolbar_button()

    def _build_main_dialog(self) -> None:
        layout = QVBoxLayout()
        self.main_dialog.setLayout(layout)
        self.main_dialog.setWindowTitle(self.tr("MO2 Vault Integration"))

        self.restore_point_button = QPushButton(self.tr("Create Restore Point"))
        self.restore_point_button.clicked.connect(self._create_restore_point)
        layout.addWidget(self.restore_point_button)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        open_vault_button = QPushButton(self.tr("Open Vault"))
        open_vault_button.clicked.connect(self._open_vault)
        layout.addWidget(open_vault_button)

        settings_button = QPushButton(self.tr("Settings..."))
        settings_button.clicked.connect(self._show_settings)
        layout.addWidget(settings_button)

        exit_button = QPushButton(self.tr("Exit"))
        exit_button.clicked.connect(self.main_dialog.hide)
        layout.addWidget(exit_button)

    def _build_settings_dialog(self) -> None:
        layout = QGridLayout()
        self.settings_dialog.setLayout(layout)
        self.settings_dialog.setWindowTitle(self.tr("MO2 Vault Integration Settings"))

        self.tool_folder_label = QLabel()
        layout.addWidget(QLabel(self.tr("Tool Folder:")), 0, 0)
        layout.addWidget(self.tool_folder_label, 0, 1)
        tool_folder_button = QPushButton(self.tr("Browse..."))
        tool_folder_button.clicked.connect(self._browse_tool_folder)
        layout.addWidget(tool_folder_button, 0, 2)

        self.vault_folder_label = QLabel()
        layout.addWidget(QLabel(self.tr("Vault Folder:")), 1, 0)
        layout.addWidget(self.vault_folder_label, 1, 1)
        vault_folder_button = QPushButton(self.tr("Browse..."))
        vault_folder_button.clicked.connect(self._browse_vault_folder)
        layout.addWidget(vault_folder_button, 1, 2)

        done_button = QPushButton(self.tr("Done"))
        done_button.clicked.connect(self.settings_dialog.hide)
        layout.addWidget(done_button, 2, 0)

    def _install_toolbar_button(self) -> None:
        self.toolbar_button = QToolButton()
        self.toolbar_button.setIcon(self._icon)
        self.toolbar_button.setToolTip(self.tooltip())
        self.toolbar_button.clicked.connect(self.display)

        tool_bar = self._parentWidget().findChild(QToolBar, "toolBar")
        if tool_bar:
            tool_bar.addWidget(self.toolbar_button)
    # endregion

    # region settings dialog
    def _refresh_settings_labels(self) -> None:
        self.tool_folder_label.setText(self._tool_folder() or self.tr("(not set)"))
        self.vault_folder_label.setText(self._vault_folder() or self.tr("(not set)"))

    def _show_settings(self) -> None:
        self._refresh_settings_labels()
        self.settings_dialog.raise_()
        self.settings_dialog.show()

    def _browse_tool_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            None, self.tr(f"Select the folder that holds {EXE_NAME}."), self._tool_folder(),
        )
        if path:
            self._organizer.setPluginSetting(self.name(), TOOL_FOLDER_SETTING, path)
            self._refresh_settings_labels()

    def _browse_vault_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            None, self.tr("Select (or create) the vault folder."), self._vault_folder(),
        )
        if path:
            self._organizer.setPluginSetting(self.name(), VAULT_FOLDER_SETTING, path)
            self._refresh_settings_labels()

    def _tool_folder(self) -> str:
        return str(self._organizer.pluginSetting(self.name(), TOOL_FOLDER_SETTING) or "")

    def _vault_folder(self) -> str:
        return str(self._organizer.pluginSetting(self.name(), VAULT_FOLDER_SETTING) or "")
    # endregion

    # region snapshot action
    def _real_game_path(self) -> str:
        game_dir = self._organizer.managedGame().gameDirectory()
        return game_dir.absolutePath() if hasattr(game_dir, "absolutePath") else str(game_dir)

    def _resolve_exe_path(self) -> str:
        """Returns the exe path, or "" (after showing a warning) if the Tool
        Folder setting isn't set/valid yet."""
        exe_path = os.path.join(self._tool_folder(), EXE_NAME)
        if not self._tool_folder() or not os.path.exists(exe_path):
            self._warn(self.tr(
                f"Set the Tool Folder (the folder containing {EXE_NAME}) in Settings first."
            ))
            return ""
        return exe_path

    def _open_vault(self) -> None:
        """Launches mo2-modlist-vault.exe with no arguments - GUI mode, same
        as double-clicking it. Mirrors ESLifier MO2 Integration's own "Start
        ESLifier" button. Useful for browsing vault history or restoring -
        restoring still needs MO2 closed first, which the vault GUI's own
        "Close MO2 Processes" button handles."""
        exe_path = self._resolve_exe_path()
        if not exe_path:
            return
        try:
            if os.name == "nt":
                os.startfile(exe_path)
            else:
                subprocess.Popen([exe_path])
        except Exception as e:
            self._warn(self.tr(f"Could not start {EXE_NAME}: {e}"))

    def _create_restore_point(self) -> None:
        if self.running:
            return

        exe_path = self._resolve_exe_path()
        vault_folder = self._vault_folder()
        if not exe_path:
            return
        if not vault_folder:
            self._warn(self.tr("Set the Vault Folder in Settings first."))
            return

        args = [
            "vault-snapshot",
            "--instance", self._organizer.basePath(),
            "--profile", self._organizer.profileName(),
            "--vault", vault_folder,
            "--real-game-path", self._real_game_path(),
        ]

        self.running = True
        self.restore_point_button.setEnabled(False)
        self.status_label.setText(self.tr("Creating restore point..."))
        self.toolbar_button.setToolTip(self.tr("Creating restore point..."))
        self._spinner_index = 0
        self._spinner_timer.start()

        self.worker = SnapshotWorker(exe_path, args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished_signal.connect(self._on_snapshot_finished)
        self.thread.start()

    def _on_snapshot_finished(self, success: bool, output: str) -> None:
        self.thread.quit()
        self.thread.wait()
        self.running = False
        self._spinner_timer.stop()
        self.toolbar_button.setIcon(self._icon)
        self.toolbar_button.setToolTip(self.tooltip())
        self.restore_point_button.setEnabled(True)
        self.status_label.setText(self.tr("Done.") if success else self.tr("Failed."))

        box = QMessageBox(parent=self._parentWidget())
        box.setWindowTitle(self.tr("Restore Point Created") if success else self.tr("Restore Point Failed"))
        box.setIcon(QMessageBox.Icon.Information if success else QMessageBox.Icon.Warning)
        box.setText(output)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()

    def _warn(self, text: str) -> None:
        box = QMessageBox(parent=self._parentWidget())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("MO2 Vault Integration"))
        box.setText(text)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
    # endregion
