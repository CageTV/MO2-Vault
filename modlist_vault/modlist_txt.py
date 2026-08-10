"""
Reads and safely rewrites a profile's `modlist.txt` and `plugins.txt`.

modlist.txt: one `+ModName` (enabled) / `-ModName` (disabled) per line. Order in
the file matches MO2's own mod-list UI top-to-bottom; the LAST line has the
HIGHEST priority (bottom of the list wins conflicts against everything above it).

plugins.txt: one `*Plugin.esp` (active) / `Plugin.esp` (inactive) per line, in
load order (first line loads first). Lines starting with `#` are comments MO2/LOOT
sometimes write and are left untouched.

Rewriting is deliberately conservative: it only ever REORDERS lines that already
exist in the file (with a `.bak` written first) - it never invents new lines for
mods that were never actually installed, since MO2 itself must be the one to
create those.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence


@dataclass
class ModlistEntry:
    name: str
    enabled: bool
    is_separator: bool


@dataclass
class PluginEntry:
    name: str
    enabled: bool


def read_modlist(profile_dir: Path) -> List[ModlistEntry]:
    path = profile_dir / "modlist.txt"
    if not path.is_file():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line[0] not in "+-":
            continue
        name = line[1:]
        entries.append(ModlistEntry(
            name=name,
            enabled=(line[0] == "+"),
            is_separator=name.endswith("_separator"),
        ))
    return entries


def read_pluginlist(profile_dir: Path) -> List[PluginEntry]:
    path = profile_dir / "plugins.txt"
    if not path.is_file():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            entries.append(PluginEntry(name=line[1:], enabled=True))
        else:
            entries.append(PluginEntry(name=line, enabled=False))
    return entries


def _reorder_lines(
    raw_lines: List[str],
    desired_order: Sequence[str],
    desired_enabled: Dict[str, bool],
    name_from_line,
    render_line,
) -> List[str]:
    by_name = {}
    unmatched: List[str] = []
    for line in raw_lines:
        name = name_from_line(line)
        if name is None:
            unmatched.append(line)  # blank/comment lines etc, keep as-is at the end
            continue
        if name in desired_enabled:
            by_name[name] = line
        else:
            unmatched.append(line)

    ordered = [
        render_line(by_name[name], desired_enabled[name])
        for name in desired_order
        if name in by_name
    ]
    return ordered + unmatched


def rewrite_modlist(profile_dir: Path, desired_order: Sequence[str], enabled_by_name: Dict[str, bool]) -> None:
    """desired_order must be ascending priority (last = highest), matching how
    modlist.txt itself is ordered."""
    path = profile_dir / "modlist.txt"
    if not path.is_file():
        return
    shutil.copy2(path, path.with_suffix(".txt.bak"))

    raw_lines = path.read_text(encoding="utf-8").splitlines()

    def name_from_line(line):
        stripped = line.strip()
        if not stripped or stripped[0] not in "+-":
            return None
        return stripped[1:]

    def render_line(_old_line, enabled):
        name = name_from_line(_old_line)
        return f"{'+' if enabled else '-'}{name}"

    new_lines = _reorder_lines(raw_lines, desired_order, enabled_by_name, name_from_line, render_line)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def rewrite_pluginlist(profile_dir: Path, desired_order: Sequence[str], enabled_by_name: Dict[str, bool]) -> None:
    """desired_order must be ascending load order (first = loads first)."""
    path = profile_dir / "plugins.txt"
    if not path.is_file():
        return
    shutil.copy2(path, path.with_suffix(".txt.bak"))

    raw_lines = path.read_text(encoding="utf-8").splitlines()

    def name_from_line(line):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return None
        return stripped[1:] if stripped.startswith("*") else stripped

    def render_line(_old_line, enabled):
        name = name_from_line(_old_line)
        return f"{'*' if enabled else ''}{name}"

    new_lines = _reorder_lines(raw_lines, desired_order, enabled_by_name, name_from_line, render_line)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def append_modlist_entry(profile_dir: Path, name: str, enabled: bool = True) -> None:
    """Used only for the last-resort case: a mod with no redownloadable source and
    no leftover archive, whose folder was copied straight into mods/ (no MO2
    install step will ever add its modlist.txt line for us)."""
    path = profile_dir / "modlist.txt"
    existing = read_modlist(profile_dir)
    if any(e.name == name for e in existing):
        return
    prefix = "+" if enabled else "-"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{name}\n")
