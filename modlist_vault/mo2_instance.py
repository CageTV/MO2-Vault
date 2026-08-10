"""
Locates the standard folders inside an MO2 *instance* directory - the folder that
directly contains `profiles/`, `mods/`, `downloads/` (usually - see
_read_downloads_dir), and `plugins/`. Works purely off the filesystem; MO2 itself
does not need to be running.
"""

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class InvalidInstanceError(Exception):
    pass


def _read_downloads_dir(root: Path) -> Path:
    """MO2 defaults downloads/ to <instance>/downloads, but it's configurable
    (Settings > Paths > Downloads) - stored as [Settings] download_directory
    in ModOrganizer.ini, either an absolute path or one using a %BASE_DIR%
    placeholder for the instance root (confirmed against MO2's own
    PathSettings::getConfigurablePath/setConfigurablePath in settings.cpp).
    Wabbajack-installed lists commonly relocate it outside the instance
    folder entirely, so this must not be assumed to always be the default."""
    ini_path = root / "ModOrganizer.ini"
    if not ini_path.is_file():
        return root / "downloads"

    parser = configparser.ConfigParser(strict=False)
    try:
        # utf-8-sig transparently strips a leading BOM if present - plain
        # "utf-8" makes configparser choke on a BOM'd file and silently
        # return None, which would wrongly fall back to the default path.
        parser.read(ini_path, encoding="utf-8-sig")
    except configparser.Error:
        return root / "downloads"

    if "Settings" not in parser:
        return root / "downloads"
    raw = parser["Settings"].get("download_directory")
    if not raw:
        return root / "downloads"

    resolved = raw.replace("%BASE_DIR%", str(root))
    path = Path(resolved)
    if not path.is_absolute():
        path = root / path
    return path


@dataclass
class Mo2Instance:
    root: Path

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def mods_dir(self) -> Path:
        return self.root / "mods"

    @property
    def downloads_dir(self) -> Path:
        return _read_downloads_dir(self.root)

    @property
    def plugins_data_dir(self) -> Path:
        return self.root / "plugins" / "data"

    def profile_dir(self, profile_name: str) -> Path:
        return self.profiles_dir / profile_name

    def mod_dir(self, mod_name: str) -> Path:
        return self.mods_dir / mod_name

    def list_profiles(self):
        if not self.profiles_dir.is_dir():
            return []
        return sorted(p.name for p in self.profiles_dir.iterdir() if p.is_dir())


def open_instance(root: Path) -> Mo2Instance:
    root = Path(root)
    instance = Mo2Instance(root=root)
    missing = [
        name for name, path in (
            ("profiles/", instance.profiles_dir),
            ("mods/", instance.mods_dir),
            ("downloads/", instance.downloads_dir),
        )
        if not path.is_dir()
    ]
    if missing:
        raise InvalidInstanceError(
            f"'{root}' doesn't look like an MO2 instance folder (missing {', '.join(missing)})."
        )
    return instance


def resolve_profile(instance: Mo2Instance, profile_name: Optional[str], create_if_missing: bool = False) -> str:
    if profile_name:
        if not instance.profile_dir(profile_name).is_dir():
            if create_if_missing:
                instance.profile_dir(profile_name).mkdir(parents=True, exist_ok=True)
                return profile_name
            raise InvalidInstanceError(f"Profile '{profile_name}' not found in {instance.profiles_dir}.")
        return profile_name

    profiles = instance.list_profiles()
    if len(profiles) == 1:
        return profiles[0]
    if not profiles:
        raise InvalidInstanceError(f"No profiles found in {instance.profiles_dir}.")
    raise InvalidInstanceError(
        f"Multiple profiles found ({', '.join(profiles)}) - pass --profile to pick one."
    )
