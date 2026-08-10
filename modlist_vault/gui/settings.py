"""Persists last-used paths/fields across app restarts as plain JSON at
SETTINGS_FILE (%LOCALAPPDATA%\\mo2-modlist-vault\\settings.json on Windows) -
not the Windows registry. That matters for a tool meant to be handed to other
people: settings live in *your* user profile, never inside the distributed
project folder, so copying/sharing the tool never leaks your paths or Nexus
API key to whoever you give it to. Delete the file (or use "Clear All
Settings" in the GUI) and the app just starts fresh next launch - no
uninstall step, no registry cleanup.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict


def _settings_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "mo2-modlist-vault"
    return Path.home() / ".mo2-modlist-vault"  # non-Windows fallback


SETTINGS_FILE = _settings_dir() / "settings.json"


class Settings:
    def __init__(self) -> None:
        self._path = SETTINGS_FILE
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._path.is_file():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get(self, key: str, default: str = "") -> str:
        value = self._data.get(key, default)
        return value if isinstance(value, str) else str(value)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self._save()

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self._data.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")

    def set_bool(self, key: str, value: bool) -> None:
        self._data[key] = value
        self._save()

    def clear_all(self) -> None:
        """Wipes every stored value, including the Nexus API key - used by
        the GUI's "Clear All Settings" button."""
        self._data = {}
        if self._path.is_file():
            self._path.unlink()

    # Shared (top bar)
    @property
    def nexus_api_key(self) -> str:
        return self.get("nexus/api_key")

    @nexus_api_key.setter
    def nexus_api_key(self, value: str) -> None:
        self.set("nexus/api_key", value)

    @property
    def nexus_game_domain(self) -> str:
        return self.get("nexus/game_domain")

    @nexus_game_domain.setter
    def nexus_game_domain(self, value: str) -> None:
        self.set("nexus/game_domain", value)

    # Backup tab
    @property
    def backup_instance(self) -> str:
        return self.get("backup/instance")

    @backup_instance.setter
    def backup_instance(self, value: str) -> None:
        self.set("backup/instance", value)

    @property
    def backup_profile(self) -> str:
        return self.get("backup/profile")

    @backup_profile.setter
    def backup_profile(self, value: str) -> None:
        self.set("backup/profile", value)

    @property
    def backup_output(self) -> str:
        return self.get("backup/output")

    @backup_output.setter
    def backup_output(self, value: str) -> None:
        self.set("backup/output", value)

    @property
    def backup_mo2_stock_archive(self) -> str:
        return self.get("backup/mo2_stock_archive")

    @backup_mo2_stock_archive.setter
    def backup_mo2_stock_archive(self, value: str) -> None:
        self.set("backup/mo2_stock_archive", value)

    @property
    def backup_real_game_path(self) -> str:
        return self.get("backup/real_game_path")

    @backup_real_game_path.setter
    def backup_real_game_path(self, value: str) -> None:
        self.set("backup/real_game_path", value)

    @property
    def backup_max_bundle_mb(self) -> str:
        return self.get("backup/max_bundle_mb")

    @backup_max_bundle_mb.setter
    def backup_max_bundle_mb(self, value: str) -> None:
        self.set("backup/max_bundle_mb", value)

    @property
    def backup_vanilla_patterns(self) -> str:
        return self.get("backup/vanilla_patterns")

    @backup_vanilla_patterns.setter
    def backup_vanilla_patterns(self, value: str) -> None:
        self.set("backup/vanilla_patterns", value)

    # Restore tab
    @property
    def restore_target(self) -> str:
        return self.get("restore/target")

    @restore_target.setter
    def restore_target(self, value: str) -> None:
        self.set("restore/target", value)

    @property
    def restore_backup_zip(self) -> str:
        return self.get("restore/backup_zip")

    @restore_backup_zip.setter
    def restore_backup_zip(self, value: str) -> None:
        self.set("restore/backup_zip", value)

    @property
    def restore_mo2_archive(self) -> str:
        return self.get("restore/mo2_archive")

    @restore_mo2_archive.setter
    def restore_mo2_archive(self, value: str) -> None:
        self.set("restore/mo2_archive", value)

    @property
    def restore_game_path(self) -> str:
        return self.get("restore/game_path")

    @restore_game_path.setter
    def restore_game_path(self, value: str) -> None:
        self.set("restore/game_path", value)

    @property
    def restore_create_stock_game_copy(self) -> bool:
        return self.get_bool("restore/create_stock_game_copy")

    @property
    def restore_downloads_source(self) -> str:
        return self.get("restore/downloads_source")

    @restore_downloads_source.setter
    def restore_downloads_source(self, value: str) -> None:
        self.set("restore/downloads_source", value)

    @restore_create_stock_game_copy.setter
    def restore_create_stock_game_copy(self, value: bool) -> None:
        self.set_bool("restore/create_stock_game_copy", value)

    # Finalize tab
    @property
    def finalize_target(self) -> str:
        return self.get("finalize/target")

    @finalize_target.setter
    def finalize_target(self, value: str) -> None:
        self.set("finalize/target", value)

    @property
    def finalize_backup_zip(self) -> str:
        return self.get("finalize/backup_zip")

    @finalize_backup_zip.setter
    def finalize_backup_zip(self, value: str) -> None:
        self.set("finalize/backup_zip", value)

    # Vault tab
    @property
    def vault_instance(self) -> str:
        return self.get("vault/instance")

    @vault_instance.setter
    def vault_instance(self, value: str) -> None:
        self.set("vault/instance", value)

    @property
    def vault_profile(self) -> str:
        return self.get("vault/profile")

    @vault_profile.setter
    def vault_profile(self, value: str) -> None:
        self.set("vault/profile", value)

    @property
    def vault_folder(self) -> str:
        return self.get("vault/folder")

    @vault_folder.setter
    def vault_folder(self, value: str) -> None:
        self.set("vault/folder", value)

    @property
    def vault_mo2_stock_archive(self) -> str:
        return self.get("vault/mo2_stock_archive")

    @vault_mo2_stock_archive.setter
    def vault_mo2_stock_archive(self, value: str) -> None:
        self.set("vault/mo2_stock_archive", value)

    @property
    def vault_real_game_path(self) -> str:
        return self.get("vault/real_game_path")

    @vault_real_game_path.setter
    def vault_real_game_path(self, value: str) -> None:
        self.set("vault/real_game_path", value)
