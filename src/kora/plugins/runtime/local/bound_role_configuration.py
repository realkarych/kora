import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from kora.plugins.runtime.local.bound_checkout import (
    BoundCheckout,
    BoundCheckoutError,
    CheckoutFailure,
)
from kora.protocols.time import Clock
from kora.runner.filesystem import (
    FileIdentity,
    FilesystemError,
    file_identity,
    open_file_at,
    require_local_filesystem,
)

CONFIG_NAME: Final = "config.toml"
MAX_CONFIG_BYTES: Final = 65_536


class RoleConfigurationFailure(StrEnum):
    PLACEMENT = "bound_role_configuration_placement"
    FILE = "bound_role_configuration_file"
    BYTES = "bound_role_configuration_bytes"
    DEADLINE = "bound_role_configuration_deadline"
    UNAVAILABLE = "bound_role_configuration_unavailable"
    CLEANUP = "bound_role_configuration_cleanup"


class BoundRoleConfigurationError(ValueError):
    def __init__(self, reason: RoleConfigurationFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class RoleDirectories:
    home: Path
    codex_home: Path
    temporary: Path

    @property
    def paths(self) -> tuple[Path, Path, Path]:
        return self.home, self.codex_home, self.temporary


class BoundRoleConfiguration:
    def __init__(self, directories: RoleDirectories, content: bytes) -> None:
        if not 0 < len(content) <= MAX_CONFIG_BYTES:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.BYTES)
        self._directories = directories
        self._content = content
        self._parent: BoundCheckout | None = None
        self._placements: list[BoundCheckout] = []
        self._config: int | None = None
        self._identity: FileIdentity | None = None
        self._started = False
        self._closing = False
        self._close_failed = False

    @property
    def directories(self) -> RoleDirectories:
        return self._directories

    @property
    def placement_identities(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (placement.anchor.identity.device, placement.anchor.identity.inode)
            for placement in self._placements
        )

    def prepare(self, root: BoundCheckout, cutoff_ns: int, clock: Clock) -> None:
        if self._started or self._closing:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.UNAVAILABLE)
        self._started = True
        _require_time(cutoff_ns, clock)
        try:
            self._parent = root.create_child(self._directories.home.parent)
            for path in self._directories.paths:
                _require_time(cutoff_ns, clock)
                self._placements.append(self._parent.create_child(path))
        except BoundCheckoutError:
            raise BoundRoleConfigurationError(
                RoleConfigurationFailure.PLACEMENT
            ) from None
        _require_time(cutoff_ns, clock)
        try:
            descriptor = open_file_at(
                self._placements[1].anchor,
                CONFIG_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            self._config = descriptor
            require_local_filesystem(descriptor)
            identity = file_identity(descriptor, CONFIG_NAME)
            with os.fdopen(descriptor, "wb", closefd=False) as target:
                target.write(self._content)
        except (OSError, FilesystemError):
            raise BoundRoleConfigurationError(RoleConfigurationFailure.FILE) from None
        _require_time(cutoff_ns, clock)
        self._identity = identity

    def verify(
        self, directories: RoleDirectories, cutoff_ns: int, clock: Clock
    ) -> None:
        _require_time(cutoff_ns, clock)
        if self._identity is None or self._closing:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.UNAVAILABLE)
        for placement, path in zip(self._placements, directories.paths, strict=True):
            try:
                placement.verify(path, cutoff_ns, clock)
            except BoundCheckoutError as error:
                failure = (
                    RoleConfigurationFailure.DEADLINE
                    if error.reason is CheckoutFailure.DEADLINE
                    else RoleConfigurationFailure.PLACEMENT
                )
                raise BoundRoleConfigurationError(failure) from None
        try:
            comparison = open_file_at(
                self._placements[1].anchor,
                CONFIG_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.FILE) from None
        try:
            self._verify_file(comparison)
        finally:
            self._close_descriptor(comparison)
        if self._close_failed:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.CLEANUP)
        _require_time(cutoff_ns, clock)

    def _verify_file(self, descriptor: int) -> None:
        try:
            if file_identity(descriptor, CONFIG_NAME) != self._identity:
                raise BoundRoleConfigurationError(RoleConfigurationFailure.FILE)
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                matches = source.read(len(self._content) + 1) == self._content
        except (OSError, FilesystemError):
            raise BoundRoleConfigurationError(RoleConfigurationFailure.FILE) from None
        if not matches:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.BYTES)

    def _close_descriptor(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            self._close_failed = True

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            if self._config is not None:
                self._close_descriptor(self._config)
            for placement in reversed(self._placements):
                self._close_placement(placement)
            if self._parent is not None:
                self._close_placement(self._parent)
        if self._close_failed:
            raise BoundRoleConfigurationError(RoleConfigurationFailure.CLEANUP)

    def _close_placement(self, placement: BoundCheckout) -> None:
        try:
            placement.close()
        except BoundCheckoutError:
            self._close_failed = True


def _require_time(cutoff_ns: int, clock: Clock) -> None:
    if clock.monotonic_ns() >= cutoff_ns:
        raise BoundRoleConfigurationError(RoleConfigurationFailure.DEADLINE)
