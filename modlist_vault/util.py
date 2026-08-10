import logging
import shutil
from pathlib import Path

logger: logging.Logger = logging.getLogger("ModlistVault")


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)


_LOCKED_FILE_HINT = (
    "something else has it open - MO2, a helper process (usvfs_proxy_*.exe), an antivirus "
    "scan, or the game itself still running. Close it and try again."
)


def safe_copy2(source: Path, destination: Path) -> None:
    """shutil.copy2, but a PermissionError names the exact file instead of a
    bare 'Permission denied' - restoring often writes into an already-live
    MO2 instance, where something else holding a file open is the single
    most common failure."""
    try:
        shutil.copy2(source, destination)
    except PermissionError as e:
        raise RuntimeError(f"Could not write '{e.filename or destination}' - {_LOCKED_FILE_HINT}") from e


def safe_copytree(source: Path, destination: Path, dirs_exist_ok: bool = False) -> None:
    try:
        shutil.copytree(source, destination, dirs_exist_ok=dirs_exist_ok)
    except PermissionError as e:
        raise RuntimeError(f"Could not write '{e.filename or destination}' - {_LOCKED_FILE_HINT}") from e
