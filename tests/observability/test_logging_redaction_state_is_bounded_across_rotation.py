import threading

import pytest

from kora.observability import SecretAdmissionError
from kora.observability.redaction import MASK, SecretRegistry, StreamMasker
from kora.observability.settings import MAX_SECRETS

THREADS: int = 8
CYCLES_PER_THREAD: int = 200
ROTATIONS: int = 10_000


def rotate(registry: SecretRegistry, thread: int, failures: list[str]) -> None:
    for cycle in range(CYCLES_PER_THREAD):
        value = f"thread-{thread:02d}-cycle-{cycle:04d}"
        registration = registry.register(value)
        if registry.mask(value) != MASK:
            failures.append(value)
        registration.release()


def test_concurrent_scopes_return_to_baseline_and_mask_at_once() -> None:
    registry = SecretRegistry()
    failures: list[str] = []
    threads = [
        threading.Thread(target=rotate, args=(registry, index, failures))
        for index in range(THREADS)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert (registry.count(), registry.retained_bytes()) == (0, 0)
    assert registry.snapshot().values == ()


def test_one_open_masker_stays_bounded_through_many_rotations() -> None:
    registry = SecretRegistry()
    parts: list[str] = []
    held: list[int] = []

    with StreamMasker(registry, parts.append) as masker:
        for rotation in range(ROTATIONS):
            registration = registry.register(f"rotated-secret-{rotation:06d}")
            masker.feed(f"line rotated-secret-{rotation:06d}\n".encode())
            held.append(masker.held)
            registration.release()

    assert "".join(parts) == f"line {MASK}\n" * ROTATIONS
    assert max(held) == 0
    assert (registry.count(), registry.retained_bytes()) == (0, 0)


def test_outstanding_output_keeps_its_mask_until_the_masker_closes() -> None:
    registry = SecretRegistry()
    registration = registry.register("outstanding-secret-42")
    parts: list[str] = []

    with StreamMasker(registry, parts.append) as masker:
        masker.feed(b"prefix outstanding-secret")
        held = masker.held
        masker.feed(b"-42 suffix")
    registration.release()

    assert held == len("outstanding-secret")
    assert "".join(parts) == f"prefix {MASK} suffix"


def test_exhausted_admission_fails_before_exposure_without_eviction() -> None:
    registry = SecretRegistry()
    registrations = [
        registry.register(f"filler-secret-{index:04d}") for index in range(MAX_SECRETS)
    ]

    with pytest.raises(SecretAdmissionError) as raised:
        registry.register("one-more-secret-9999")

    assert "one-more-secret-9999" not in str(raised.value)
    assert registry.mask("filler-secret-0000") == MASK
    assert registry.count() == MAX_SECRETS
    for registration in registrations:
        registration.release()
