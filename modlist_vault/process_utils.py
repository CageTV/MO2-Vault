"""Force-closes MO2 and its known helper processes. setup_mo2()/restore()
overwrite files directly inside the live MO2 instance folder - if MO2 (or a
helper process it spawned) still has one of them open, that write fails with
a permission error. This is a manual, explicit action (a GUI button), not run
automatically before every restore - killing processes without being asked is
not something to do silently.
"""

import subprocess
import sys
from typing import List

KNOWN_LOCKING_PROCESSES = [
    "ModOrganizer.exe",
    "usvfs_proxy_x64.exe",
    "usvfs_proxy_x86.exe",
    "nxmhandler.exe",
    "helper.exe",
    "explorer++.exe",
]


def kill_mo2_processes() -> List[str]:
    """Returns the process names that were actually found and closed."""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    killed = []
    for name in KNOWN_LOCKING_PROCESSES:
        try:
            result = subprocess.run(
                ["taskkill", "/IM", name, "/F"],
                capture_output=True, text=True, creationflags=creationflags,
            )
        except FileNotFoundError:
            break  # not on Windows / no taskkill - nothing more to try
        if result.returncode == 0:
            killed.append(name)
    return killed
