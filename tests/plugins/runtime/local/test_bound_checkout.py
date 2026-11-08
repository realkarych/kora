import errno
import os
from pathlib import Path

import pytest

from kora.plugins.runtime.local.bound_checkout import (
    BoundCheckout,
    BoundCheckoutError,
    CheckoutFailure,
)
from tests.plugins.runtime.local.frames_support import (
    ORIGIN_NS,
    StepClock,
    TickingClock,
)


def test_checkout_rejects_replacement_and_releases_exact_descriptor(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "workspace"
    path.mkdir(mode=0o700)
    clock = StepClock()
    cutoff = ORIGIN_NS + 10
    baseline = set(os.listdir("/dev/fd"))
    checkout = BoundCheckout.capture(path)
    descriptor = checkout.anchor.descriptor
    original = path.with_name("original")
    try:
        checkout.verify(path, cutoff, clock)
        path.rename(original)
        path.mkdir(mode=0o700)

        with pytest.raises(BoundCheckoutError) as replaced:
            checkout.verify(path, cutoff, clock)
        held = os.fstat(descriptor)
        live = path.stat()
    finally:
        checkout.close()

    assert replaced.value.reason is CheckoutFailure.IDENTITY
    assert held.st_ino == original.stat().st_ino
    assert (held.st_dev, held.st_ino) != (live.st_dev, live.st_ino)
    assert checkout.anchor.closed
    assert set(os.listdir("/dev/fd")) == baseline
    with pytest.raises(OSError, match=rf"^\[Errno {errno.EBADF}\]"):
        os.fstat(descriptor)


def test_checkout_checks_cutoff_after_view_probe_and_closes_temporary_descriptor(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "workspace"
    path.mkdir(mode=0o700)
    baseline = set(os.listdir("/dev/fd"))
    checkout = BoundCheckout.capture(path)
    clock = TickingClock(1)
    descriptor = checkout.anchor.descriptor
    try:
        with pytest.raises(BoundCheckoutError) as expired:
            checkout.verify(path, ORIGIN_NS + 2, clock)
        observed = set(os.listdir("/dev/fd"))
    finally:
        checkout.close()

    assert expired.value.reason is CheckoutFailure.DEADLINE
    assert len(observed) == len(baseline) + 1
    assert str(descriptor) in observed
    assert set(os.listdir("/dev/fd")) == baseline
