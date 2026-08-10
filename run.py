"""PyInstaller entry point. A plain top-level script (not inside the
modlist_vault package) so modlist_vault.launcher can be imported normally -
importing it this way keeps its internal `from .cli import ...`-style
relative imports working, which they wouldn't if launcher.py itself were run
directly as __main__.
"""

import sys

from modlist_vault.launcher import main

if __name__ == "__main__":
    sys.exit(main())
