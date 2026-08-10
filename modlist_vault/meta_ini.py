"""
Reads MO2's own `mods/<ModName>/meta.ini` - the per-installed-mod metadata MO2
writes itself (which archive installed it, its Nexus id/version, etc).
"""

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ModMeta:
    mod_name: str
    installation_file: Optional[str]
    nexus_mod_id: Optional[int]
    version: Optional[str]
    repository: Optional[str]


def _get(section: configparser.SectionProxy, *keys: str) -> Optional[str]:
    # configparser lowercases option names by default, so this is case-insensitive
    # to whatever casing a given MO2 version happens to use.
    for key in keys:
        value = section.get(key.lower())
        if value:
            return value
    return None


def _to_int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_mod_meta(mod_dir: Path) -> Optional[ModMeta]:
    meta_path = mod_dir / "meta.ini"
    if not meta_path.is_file():
        return None

    parser = configparser.ConfigParser(strict=False)
    try:
        # utf-8-sig transparently strips a leading BOM if present - plain
        # "utf-8" makes configparser choke on a BOM'd file and silently
        # return None, misclassifying the mod as having no metadata at all.
        parser.read(meta_path, encoding="utf-8-sig")
    except configparser.Error:
        return None

    if "General" not in parser:
        return None

    general = parser["General"]
    installation_file = _get(general, "installationFile", "installedFile")
    mod_id = _to_int_or_none(_get(general, "modid", "modID"))

    return ModMeta(
        mod_name=mod_dir.name,
        installation_file=installation_file if installation_file and installation_file != "" else None,
        nexus_mod_id=mod_id if mod_id and mod_id > 0 else None,
        version=_get(general, "version"),
        repository=_get(general, "repository"),
    )


def read_color(mod_dir: Path) -> Optional[str]:
    """Reads the raw color=@Variant(...) field from a mod/separator's meta.ini,
    if any - a Qt-serialized QColor blob. We never parse it, just round-trip it
    byte-for-byte, so a custom mod/separator highlight color set in MO2
    survives a restore."""
    meta_path = mod_dir / "meta.ini"
    if not meta_path.is_file():
        return None
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(meta_path, encoding="utf-8-sig")
    except configparser.Error:
        return None
    if "General" not in parser:
        return None
    return parser["General"].get("color") or None


def write_mod_meta(
    mod_dir: Path,
    *,
    game_name: str,
    mod_id: Optional[int],
    file_id: Optional[int],
    version: str,
    installation_file: str,
    repository: str = "Nexus",
    color: Optional[str] = None,
) -> Path:
    """Writes a minimal but valid meta.ini for a mod folder we populated
    ourselves via install_recipe.py, bypassing MO2's own installer entirely.
    Cosmetic-only fields real MO2 installs also carry (description, category,
    endorsement state, etc.) are omitted - MO2 tolerates their absence. color,
    if given, is the mod's captured highlight color (see read_color)."""
    meta_path = mod_dir / "meta.ini"

    def esc(value) -> str:
        return str(value).replace("\n", " ").replace("\r", " ")

    lines = [
        "[General]",
        f"gameName={esc(game_name)}",
        f"modid={mod_id or 0}",
        f"version={esc(version)}",
        "newestVersion=",
        'category="-1,"',
        f"installationFile={esc(installation_file)}",
        f"repository={esc(repository)}",
        "ignoredVersion=",
        "comments=",
        "notes=",
        "url=",
        "hasCustomURL=false",
        "converted=false",
        "validated=false",
    ]
    if color:
        lines.append(f"color={color}")
    lines += [
        "endorsed=0",
        "tracked=0",
        "",
        "[installedFiles]",
        "size=1",
        f"1\\modid={mod_id or 0}",
        f"1\\fileid={file_id or 0}",
        "",
    ]
    meta_path.write_text("\n".join(lines), encoding="utf-8")
    return meta_path


def write_separator_meta(mod_dir: Path, color: Optional[str]) -> Path:
    """Minimal meta.ini for a separator - separators are never installed from
    an archive, so this only ever needs to carry the one field MO2 actually
    reads back for them: their custom highlight color, if one was set."""
    meta_path = mod_dir / "meta.ini"
    lines = [
        "[General]",
        "modid=0",
        "version=",
        "newestVersion=",
        'category="-1,"',
        "installationFile=",
        "repository=Nexus",
        "ignoredVersion=",
        "comments=",
        "notes=",
        "url=",
        "hasCustomURL=false",
        "converted=false",
        "validated=false",
    ]
    if color:
        lines.append(f"color={color}")
    lines += [
        "tracked=0",
        "",
        "[installedFiles]",
        "size=0",
        "",
    ]
    meta_path.write_text("\n".join(lines), encoding="utf-8")
    return meta_path
