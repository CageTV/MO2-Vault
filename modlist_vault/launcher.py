"""Combined entry point for the packaged .exe - double-clicking it (no
arguments) opens the GUI, exactly like running `python -m modlist_vault.gui`;
any arguments route to the CLI instead, exactly like `python -m modlist_vault
<subcommand> ...`. This lets one build serve both as a normal desktop app and
as the binary external tools (e.g. the MO2 plugin) shell out to headlessly
for commands like `vault-snapshot`.
"""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        return cli_main()

    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
