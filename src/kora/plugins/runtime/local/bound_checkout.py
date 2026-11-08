from enum import StrEnum
from pathlib import Path

from kora.protocols.time import Clock
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    create_directory,
    open_local_directory,
)


class CheckoutFailure(StrEnum):
    IDENTITY = "bound_checkout_identity"
    UNAVAILABLE = "bound_checkout_unavailable"
    DEADLINE = "bound_checkout_deadline"


class BoundCheckoutError(ValueError):
    def __init__(self, reason: CheckoutFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


class BoundCheckout:
    def __init__(self, path: Path, anchor: DirectoryAnchor) -> None:
        self._path = path
        self._anchor = anchor
        self._closing = False
        self._close_failed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def anchor(self) -> DirectoryAnchor:
        return self._anchor

    @classmethod
    def capture(cls, path: Path) -> "BoundCheckout":
        return cls(path, _open_checkout(path))

    def create_child(self, path: Path) -> "BoundCheckout":
        if self._closing or path.parent != self.path:
            raise BoundCheckoutError(CheckoutFailure.IDENTITY)
        try:
            child = create_directory(self.anchor, path.name)
        except (OSError, FilesystemError):
            raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE) from None
        child.ancestors = (*self.anchor.ancestors, child.identity)
        return BoundCheckout(path, child)

    def verify(self, path: Path, cutoff_ns: int, clock: Clock) -> None:
        _require_time(cutoff_ns, clock)
        if self._closing:
            raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE)
        if path != self.path:
            raise BoundCheckoutError(CheckoutFailure.IDENTITY)
        try:
            self.anchor.verify()
        except (OSError, FilesystemError):
            raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE) from None
        observed = _open_checkout(path)
        matches = (
            observed.identity == self.anchor.identity
            and observed.ancestors == self.anchor.ancestors
        )
        try:
            observed.close()
        except OSError:
            self._close_failed = True
            if matches:
                raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE) from None
        if not matches:
            raise BoundCheckoutError(CheckoutFailure.IDENTITY)
        _require_time(cutoff_ns, clock)

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            try:
                self.anchor.close()
            except OSError:
                self._close_failed = True
        if self._close_failed:
            raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE)


def _open_checkout(path: Path) -> DirectoryAnchor:
    try:
        return open_local_directory(path)
    except (OSError, FilesystemError):
        raise BoundCheckoutError(CheckoutFailure.UNAVAILABLE) from None


def _require_time(cutoff_ns: int, clock: Clock) -> None:
    if clock.monotonic_ns() >= cutoff_ns:
        raise BoundCheckoutError(CheckoutFailure.DEADLINE)
