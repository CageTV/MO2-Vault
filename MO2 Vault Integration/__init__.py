from typing import List

from mobase import IPlugin

from .vault_restore_point import VaultRestorePointPlugin


def createPlugins() -> List["IPlugin"]:
    return [VaultRestorePointPlugin()]
