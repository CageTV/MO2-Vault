"""PySide6 GUI for mo2-modlist-vault - a thin Qt shell around the same
backup/setup-mo2/restore/finalize-restore operations cli.py exposes.

Run with: python -m modlist_vault.gui
"""

import sys
from pathlib import Path

ICON_PATH = Path(__file__).parent / "assets" / "icon.ico"


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MO2 Modlist Vault")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
