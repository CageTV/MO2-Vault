"""
Read/write helpers for the on-disk data of the third-party "Remember Installation
Choices" (RIC) plugin: https://github.com/miere43/remember-installation-choices

RIC has no API to call into, but its save format is just one JSON file per mod at:

    <instance>/plugins/data/remember_installation_choices/saves_v4/{escapedGameName}/{modName}.json

where `escapedGameName` replaces every character outside `[a-zA-Z0-9_.-]` with `_`
(RIC's `escapeFileName`, applied to MO2's IPluginGame.gameName()). Backup/restore
of RIC's choices is therefore just copying that whole per-game directory - no need
to parse or reproduce RIC's JSON schema.

Since this tool has no live MO2 process to ask for gameName(), `game_folder` below
is always the already-escaped folder name discovered on disk via
find_existing_game_folder(), never recomputed from a hand-typed game name.
"""

import shutil
from pathlib import Path
from typing import Optional

from .util import logger

_SAVES_ROOT = ("remember_installation_choices", "saves_v4")


def _saves_root(instance_root: Path) -> Path:
    return Path(instance_root, "plugins", "data", *_SAVES_ROOT)


def saves_dir_for(instance_root: Path, game_folder: str) -> Path:
    """Public accessor for the exact on-disk saves directory for an
    already-escaped game folder name (as returned by find_existing_game_folder)."""
    return _saves_root(instance_root) / game_folder


def find_existing_game_folder(instance_root: Path) -> Optional[str]:
    """We can't query MO2's IPluginGame.gameName() without MO2 running, and hand
    typing it is error-prone (exact display string, e.g. 'Skyrim Special Edition').
    Instead, discover the already-escaped folder name directly from whatever RIC
    has already written on disk. Returns None if RIC has no saves yet for this
    instance (nothing to back up, not an error)."""
    root = _saves_root(instance_root)
    if not root.is_dir():
        return None
    game_folders = [p.name for p in root.iterdir() if p.is_dir()]
    if not game_folders:
        return None
    if len(game_folders) > 1:
        logger.info(
            "Multiple RIC game folders found under %s (%s) - this shouldn't happen "
            "for a single-game instance, using the first one.", root, game_folders,
        )
    return game_folders[0]


def copy_saves_to(instance_root: Path, game_folder: str, destination_dir: Path) -> int:
    """Copies every recorded FOMOD choice for the given (already-escaped) game
    folder into destination_dir. Returns the number of files copied."""
    saves_dir = _saves_root(instance_root) / game_folder
    if not saves_dir.is_dir():
        logger.info("No RIC saves directory found at %s, skipping.", saves_dir)
        return 0

    destination_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for save_file in saves_dir.glob("*.json"):
        shutil.copy2(save_file, destination_dir / save_file.name)
        count += 1
    return count


def restore_saves_from(instance_root: Path, game_folder: str, source_dir: Path) -> int:
    """Copies backed-up RIC choice files into the live saves directory for the
    given (already-escaped) game folder on the target instance, ahead of
    installing, so RIC pre-fills each FOMOD dialog. Returns the number restored."""
    if not source_dir.is_dir():
        logger.info("No backed-up RIC saves at %s, skipping.", source_dir)
        return 0

    saves_dir = _saves_root(instance_root) / game_folder
    saves_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for save_file in source_dir.glob("*.json"):
        shutil.copy2(save_file, saves_dir / save_file.name)
        count += 1
    return count
