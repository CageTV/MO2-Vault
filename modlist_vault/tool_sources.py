"""
Matches tools/<name> folders (external utilities like SSEEdit, DynDOLOD,
Synthesis - not managed by MO2's mods/ system at all) to the downloads/*.meta
file(s) that populate them, so they can be redownloaded and reconstructed via
an install recipe instead of being bundled raw - the exact same mechanism
mods/ already uses.

A tool folder can be assembled from MORE THAN ONE download - e.g. DynDOLOD's
tools/DynDOLOD folder is actually the combined output of four separate Nexus
mods (the main DynDOLOD archive, "DynDOLOD DLL NG and Scripts", "DynDOLOD NG -
Settings Loader", and "DynDOLOD Resources SE"). We can't know in advance which
downloads contribute to a given tool folder, so any download whose modName
contains the tool folder's name as a substring is treated as a *candidate* -
backup.py then hash-diffs each candidate against the tool folder in turn and
only keeps the ones that actually matched at least one installed file.
"""

from pathlib import Path
from typing import Dict, List

from . import download_meta
from .mo2_instance import Mo2Instance


def _normalize(name: str) -> str:
    return name.strip().lower()


def find_tool_sources(instance: Mo2Instance) -> Dict[str, List[download_meta.DownloadMeta]]:
    """Returns {tool_folder_name: [DownloadMeta, ...]} - every downloads/*.meta
    whose modName contains the tools/<name> folder's name, for every
    tools/<name> folder that has at least one such candidate."""
    tools_dir = instance.root / "tools"
    if not tools_dir.is_dir() or not instance.downloads_dir.is_dir():
        return {}

    tool_names = {_normalize(p.name): p.name for p in tools_dir.iterdir() if p.is_dir()}
    if not tool_names:
        return {}

    sources: Dict[str, List[download_meta.DownloadMeta]] = {}
    for meta_path in instance.downloads_dir.glob("*.meta"):
        archive_path = meta_path.with_name(meta_path.stem)
        meta = download_meta.read_meta(archive_path)
        if not meta or not meta.mod_name:
            continue
        normalized_mod_name = _normalize(meta.mod_name)
        for key, folder_name in tool_names.items():
            if key in normalized_mod_name:
                sources.setdefault(folder_name, []).append(meta)

    # Exact/closer name matches first - they're the most likely "main" archive
    # and resolving the bulk of the files with fewer archives keeps things fast.
    for folder_name, metas in sources.items():
        metas.sort(key=lambda m: len(_normalize(m.mod_name)))
    return sources
