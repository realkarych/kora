import asyncio
import threading
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Checkout, run
from kora.executor.failures import ExecutorError, Phase
from kora.executor.protected import (
    Finding,
    FuelBounds,
    check_paths,
    checked_off_loop,
    compiled_globs,
)
from kora.ir import VcsRef
from kora.ir.node import ErrorClass
from kora.protocols.vcs import CheckoutRequest
from tests.plugins.vcs.git_support import REPO, World, git, make_world

PROTECTED: tuple[str, ...] = ("keep", "run.sh")


def deleted(world: World, checkout: Checkout) -> None:
    world.commit(checkout, {"keep/old.txt": None}, "delete a protected file")


def restored(world: World, checkout: Checkout) -> None:
    world.commit(checkout, {"run.sh": "#!/bin/sh\necho changed\n"}, "change")
    world.commit(checkout, {"run.sh": "#!/bin/sh\n"}, "restore")


def merged(world: World, checkout: Checkout) -> None:
    candidate = world.candidate(checkout)
    git(world.home, "checkout", "-q", "-b", "side", cwd=candidate)
    world.commit(checkout, {"side.txt": "side\n"}, "side")
    git(world.home, "checkout", "-q", "conformance-work", cwd=candidate)
    world.commit(checkout, {"main.txt": "main\n"}, "main")
    git(world.home, "merge", "-q", "--no-commit", "side", cwd=candidate)
    (candidate / "keep" / "old.txt").write_text("changed only in the merge\n")
    git(world.home, "add", "-A", cwd=candidate)
    git(world.home, "commit", "-q", "-m", "merge", cwd=candidate)
    world.commit(checkout, {"keep/old.txt": "old\n"}, "restore after the merge")


@pytest.mark.parametrize("history", [deleted, restored, merged])
def test_protected_paths_detect_deletion_and_restore(
    tmp_path: Path, kora_capture: Capture, history: object
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    assert callable(history)
    history(world, checkout)

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout, protected=PROTECTED)

    assert raised.value.failure_class is ErrorClass.PROTECTED_PATH_MODIFIED
    assert world.remote_head() is None


def test_unprotected_changes_publish(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    world.commit(checkout, {"free.txt": "free\n"}, "free")

    prepared = world.prepare(checkout, protected=PROTECTED)

    assert prepared.result.publication.changed_paths == ("free.txt",)


def test_gate_checkout_of_a_changed_vcs_ref_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    world.commit(checkout, {"run.sh": "#!/bin/sh\necho gate\n"}, "touch protected")
    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)
    assert outcome.ref is not None
    gate = world.bench().areas()
    request = CheckoutRequest(
        repo=REPO,
        rev=outcome.ref,
        root=str(gate.candidate),
        attempt_id="gate-attempt",
        branch="gate",
    )
    plan = run(world.provider.checkout_plan(request, world.context()))
    assert isinstance(request.rev, VcsRef)

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_workspace(
                plan, request, gate, PROTECTED, False, world.context()
            )
        )
    allowed = run(
        world.executor.prepare_workspace(
            plan,
            request,
            world.bench().areas(),
            ("unrelated",),
            False,
            world.context(),
        )
    )

    assert raised.value.failure_class is ErrorClass.PROTECTED_PATH_MODIFIED
    assert raised.value.phase is Phase.CHECKOUT
    assert allowed.base_commit == world.base
    assert allowed.changed_paths == ("run.sh",)


def test_fuel_exhaustion_and_the_path_cap_fail_closed() -> None:
    globs = compiled_globs(("*a*b*c*d*",), Phase.INSPECT)
    long_path = "/".join(["a" * 90] * 40)
    unlimited = FuelBounds(budget=10**12, per_path=10**12, slice_units=10**12)
    long_cost = check_paths(globs, [long_path], unlimited, threading.Event()).spent
    short_cost = check_paths(globs, ["short.txt"], unlimited, threading.Event()).spent
    small = FuelBounds(budget=long_cost * 5, per_path=long_cost, slice_units=1_000)
    capped = FuelBounds(
        budget=10**9, per_path=(long_cost + short_cost) // 2, slice_units=10**6
    )

    exhausted = check_paths(globs, [long_path] * 20, small, threading.Event())
    over_cap = check_paths(globs, [long_path], capped, threading.Event())
    clear = check_paths(globs, ["short.txt"], capped, threading.Event())

    assert exhausted.finding is Finding.FUEL
    assert over_cap.finding is Finding.FUEL
    assert clear.finding is Finding.CLEAR


def test_an_invalid_union_is_config_error() -> None:
    with pytest.raises(ExecutorError) as raised:
        compiled_globs(("valid", "in[valid"), Phase.INSPECT)

    assert raised.value.failure_class is ErrorClass.CONFIG_ERROR


def test_cancellation_takes_effect_within_one_slice() -> None:
    globs = compiled_globs(("*a*b*c*d*",), Phase.INSPECT)
    paths = ["/".join(["a" * 90] * 40)] * 1000
    bounds = FuelBounds(budget=10**12, per_path=10**7, slice_units=50_000)
    cancelled = threading.Event()
    cancelled.set()

    result = check_paths(globs, paths, bounds, cancelled)

    assert result.finding is Finding.CANCELLED
    assert result.spent < bounds.slice_units + bounds.per_path


def test_cancelled_check_does_not_wait_for_its_thread() -> None:
    globs = compiled_globs(("*a*b*c*d*",), Phase.INSPECT)
    paths = ["/".join(["a" * 90] * 40)] * 100_000
    bounds = FuelBounds(budget=10**13, per_path=10**7, slice_units=10**6)

    async def cancel_soon() -> bool:
        task = asyncio.create_task(checked_off_loop(globs, paths, bounds))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(cancel_soon())
