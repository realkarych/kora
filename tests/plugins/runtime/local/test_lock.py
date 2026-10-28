import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.protocols.errors import KoraProtocolError
from kora.runner.filesystem import DirectoryAnchor, open_local_directory
from kora.runner.registry import (
    Entry,
    Transition,
    TransitionExecutor,
    bind_workspace,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    READY_DEADLINE_SECONDS,
    OpenedRegistry,
    open_descriptors,
    open_registry,
    runtime_context,
    state_root,
    stopped_holder,
)

__all__ = ["kora_capture"]

WAITERS = 8


async def bind(attempt: str, registry: OpenedRegistry, capture: Capture) -> str:
    try:
        await registry.transition(
            attempt,
            lambda entry: bind_workspace(entry, None, False),
            runtime_context(capture),
        )
    except KoraProtocolError as error:
        return error.code
    return "accepted"


def runtime_threads() -> int:
    return sum(
        thread.name.startswith("kora-runtime-local") for thread in threading.enumerate()
    )


@pytest.mark.asyncio
async def test_registry_lock_is_bounded(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    registry = await open_registry(root, runtime_context(kora_capture))
    await bind("attempt-held", registry, kora_capture)
    lock = root / "registry" / f"{registry.key('attempt-held').name}.lock"

    try:
        with stopped_holder(lock):
            started = time.monotonic()
            waiters = [
                asyncio.create_task(bind("attempt-held", registry, kora_capture))
                for _ in range(WAITERS)
            ]
            other = await bind("attempt-free", registry, kora_capture)
            other_seconds = time.monotonic() - started
            looked = await registry.entry("attempt-held")
            lookup_seconds = time.monotonic() - started
            codes = await asyncio.gather(*waiters)
            waited_seconds = time.monotonic() - started
    finally:
        await registry.close()

    records = kora_capture.finish()
    timeouts = [
        record for record in records if record["event"] == "runtime.local.lock_timeout"
    ]
    assert other == "accepted"
    assert looked is not None
    assert codes == ["registry_busy"] * WAITERS
    assert other_seconds < 1.0
    assert lookup_seconds < 1.0
    assert 2.0 <= waited_seconds < 4.0
    assert len(timeouts) == WAITERS
    assert {record["operation"] for record in timeouts} == {"transition"}
    assert all(PATH_MARKER not in str(record) for record in records)


async def lock_timeout(root: Path, capture: Capture) -> None:
    registry = await open_registry(root, runtime_context(capture))
    await bind("attempt-held", registry, capture)
    lock = root / "registry" / f"{registry.key('attempt-held').name}.lock"
    try:
        with stopped_holder(lock):
            refused = await bind("attempt-held", registry, capture)
    finally:
        await registry.close()
    assert refused == "registry_busy"


async def admission_refusal(root: Path, capture: Capture) -> None:
    executor = TransitionExecutor(workers=1, admitted=1)
    started = threading.Event()
    gate = threading.Event()

    def hold() -> bool:
        started.set()
        return gate.wait(READY_DEADLINE_SECONDS)

    try:
        first = asyncio.create_task(executor.run(hold))
        assert await asyncio.to_thread(started.wait, READY_DEADLINE_SECONDS)
        refused = await asyncio.gather(
            executor.run(gate.is_set), return_exceptions=True
        )
        gate.set()
        held = await first
    finally:
        gate.set()
        await executor.close()
    assert held is True
    assert isinstance(refused[0], KoraProtocolError)
    assert (refused[0].code, refused[0].retryable) == ("registry_busy", True)


async def cancelled_caller(root: Path, capture: Capture) -> None:
    registry = await open_registry(root, runtime_context(capture))
    started = threading.Event()
    gate = threading.Event()
    key = registry.key("attempt-1")

    def decide(entry: Entry | None) -> Transition:
        started.set()
        gate.wait(READY_DEADLINE_SECONDS)
        return bind_workspace(entry, None, False)

    try:
        caller = asyncio.create_task(
            registry.executor.run(lambda: registry.registry.try_transition(key, decide))
        )
        assert await asyncio.to_thread(started.wait, READY_DEADLINE_SECONDS)
        caller.cancel()
        gate.set()
        outcome = await asyncio.gather(caller, return_exceptions=True)
        recorded = await registry.entry("attempt-1")
    finally:
        gate.set()
        await registry.close()
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert recorded is not None
    assert recorded.state == "unsubmitted"


async def cancelled_open(root: Path, capture: Capture) -> None:
    executor = TransitionExecutor()
    started = threading.Event()
    gate = threading.Event()

    def gated_open() -> DirectoryAnchor:
        started.set()
        gate.wait(READY_DEADLINE_SECONDS)
        return open_local_directory(root)

    try:
        caller = asyncio.create_task(executor.run(gated_open, DirectoryAnchor.close))
        assert await asyncio.to_thread(started.wait, READY_DEADLINE_SECONDS)
        caller.cancel()
        gate.set()
        outcome = await asyncio.gather(caller, return_exceptions=True)
    finally:
        gate.set()
        await executor.close()
    assert isinstance(outcome[0], asyncio.CancelledError)


LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "lock_timeout": lock_timeout,
    "admission_refusal": admission_refusal,
    "cancelled_caller": cancelled_caller,
    "cancelled_open": cancelled_open,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", sorted(LEGS))
async def test_local_runtime_releases_resources(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    descriptors = open_descriptors()
    threads = runtime_threads()

    await LEGS[leg](state_root(tmp_path), kora_capture)

    assert open_descriptors() == descriptors
    assert runtime_threads() == threads == 0
