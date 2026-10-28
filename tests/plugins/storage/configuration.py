from pathlib import Path

from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.storage_models import StorageLimits


def local_configuration(root: Path) -> LocalStorageConfiguration:
    return LocalStorageConfiguration(root=str(root), limits=StorageLimits(version=1))
