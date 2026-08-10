"""
Reads/writes the ".meta" sidecar MO2 keeps next to every downloaded archive in
`downloads/` (a Qt QSettings INI file, `[General]` section with plain key=value
pairs for simple string/int values - readable/writable with plain configparser,
no Qt dependency needed).
"""

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DownloadMeta:
    archive_path: Path
    name: str
    mod_name: str
    version: str
    url: Optional[str]
    game_name: Optional[str]
    nexus_mod_id: Optional[int]
    nexus_file_id: Optional[int]


def _unquote(value: Optional[str]) -> Optional[str]:
    if value and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _to_int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_meta(archive_path: Path) -> Optional[DownloadMeta]:
    meta_path = archive_path.with_name(f"{archive_path.name}.meta")
    if not meta_path.is_file():
        return None

    parser = configparser.ConfigParser(strict=False)
    try:
        # utf-8-sig transparently strips a leading BOM if present (some tools/
        # editors write one) and behaves exactly like utf-8 otherwise - plain
        # "utf-8" makes configparser choke on a BOM'd file and silently return
        # None, which would misclassify a mod as having no metadata at all.
        parser.read(meta_path, encoding="utf-8-sig")
    except configparser.Error:
        return None

    if "General" not in parser:
        return None

    general = parser["General"]
    mod_id = _to_int_or_none(general.get("modid"))
    file_id = _to_int_or_none(general.get("fileid"))

    return DownloadMeta(
        archive_path=archive_path,
        name=_unquote(general.get("name")) or archive_path.name,
        mod_name=_unquote(general.get("modname")) or "",
        version=_unquote(general.get("version")) or "",
        # MO2's own self-updater writes a minimal .meta (no modName/name/version)
        # using "directURL" instead of "url" - recognize either.
        url=_unquote(general.get("url")) or _unquote(general.get("directurl")) or None,
        game_name=_unquote(general.get("gamename")) or None,
        nexus_mod_id=mod_id if mod_id and mod_id > 0 else None,
        nexus_file_id=file_id if file_id and file_id > 0 else None,
    )


def write_meta(
    archive_path: Path,
    *,
    game_name: str,
    mod_id: Optional[int],
    file_id: Optional[int],
    name: str,
    mod_name: str,
    version: str,
    url: str = "",
) -> Path:
    meta_path = archive_path.with_name(f"{archive_path.name}.meta")

    def esc(value: str) -> str:
        return str(value).replace("\n", " ").replace("\r", " ")

    lines = [
        "[General]",
        f"gameName={esc(game_name)}",
        f"modID={mod_id or 0}",
        f"fileID={file_id or 0}",
        f"url={esc(url)}",
        f"name={esc(name)}",
        f"modName={esc(mod_name)}",
        f"version={esc(version)}",
        "newestVersion=",
        "repository=Nexus",
        "installed=false",
        "uninstalled=false",
        "paused=false",
        "removed=false",
        "",
    ]
    meta_path.write_text("\n".join(lines), encoding="utf-8")
    return meta_path
