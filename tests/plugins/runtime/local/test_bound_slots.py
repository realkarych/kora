import asyncio
from pathlib import Path
from typing import NoReturn

import pytest

from kora.conformance.capture import Capture
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots, RoleClaim
from kora.plugins.runtime.local.environment import (
    BoundCandidateEnvironments,
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused
from tests.plugins.runtime.local.frames_support import (
    ORIGIN_NS,
    StepClock,
    TickingClock,
)


def binding_for(workspace: Path, capture: Capture) -> BoundCandidateEnvironments:
    return bind_candidate_environments(
        cwd=workspace,
        attempt_token=capture.scope.admit("synthetic-attempt"),
        workload={},
        secrets={},
        harness_credentials={"CODEX_API_KEY": capture.scope.admit("synthetic-key")},
        roles={
            "app": CandidateRoleEnvironment({}, ("CODEX_API_KEY",)),
            "exec": CandidateRoleEnvironment({}, ()),
        },
    )


def assert_slot_refusal(error: KoraProtocolError) -> None:
    assert error.code == "launch_rejected"
    assert error.message == "the bound native role is unavailable"
    assert not error.retryable
    assert error.details == {}
    assert error.__context__ is None
    assert error.__cause__ is None


def unexpected_acquisition(environment: dict[str, str]) -> NoReturn:
    raise AssertionError("a refused role reached resource acquisition")


@pytest.mark.asyncio
async def test_concurrent_duplicate_claim_has_one_winner(
    tmp_path: Path, kora_capture: Capture
) -> None:
    binding = binding_for(tmp_path, kora_capture)
    slots = BoundRoleSlots(binding, StepClock(), ORIGIN_NS + 10)
    start = asyncio.Event()

    async def claim() -> RoleClaim | KoraProtocolError:
        await start.wait()
        try:
            return slots.claim("exec")
        except KoraProtocolError as error:
            return error

    requests = [asyncio.create_task(claim()) for _ in range(2)]

    start.set()
    results = await asyncio.gather(*requests)

    assert sum(isinstance(result, RoleClaim) for result in results) == 1
    refusals = [result for result in results if isinstance(result, KoraProtocolError)]
    assert len(refusals) == 1
    assert_slot_refusal(refusals[0])
    assert binding.roles == {"app", "exec"}


def test_slot_handoff_rejects_replay(tmp_path: Path, kora_capture: Capture) -> None:
    slots = BoundRoleSlots(
        binding_for(tmp_path, kora_capture), StepClock(), ORIGIN_NS + 10
    )
    app = slots.claim("app")

    key = slots.start("app", app, lambda environment: environment["CODEX_API_KEY"])
    kora_capture.scope.release()
    with pytest.raises(KoraProtocolError) as replay:
        slots.start("app", app, unexpected_acquisition)
    with pytest.raises(KoraProtocolError) as duplicate:
        slots.claim("app")

    assert key == "synthetic-key"
    assert_slot_refusal(replay.value)
    assert_slot_refusal(duplicate.value)


def test_slot_handoff_rejects_pending_wrong_role_and_foreign_owner(
    tmp_path: Path, kora_capture: Capture
) -> None:
    binding = binding_for(tmp_path, kora_capture)
    clock = StepClock()
    slots = BoundRoleSlots(binding, clock, ORIGIN_NS + 10)
    foreign = BoundRoleSlots(binding, clock, ORIGIN_NS + 10)
    app = slots.claim("app")
    slots.claim("exec")
    foreign_executor = foreign.claim("exec")
    kora_capture.scope.release()

    with pytest.raises(KoraProtocolError) as cross_role:
        slots.start("exec", app, unexpected_acquisition)
    with pytest.raises(KoraProtocolError) as cross_owner:
        slots.start("exec", foreign_executor, unexpected_acquisition)

    assert_slot_refusal(cross_role.value)
    assert_slot_refusal(cross_owner.value)


def test_claim_rejects_unknown_role_and_exact_cutoff(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    slots = BoundRoleSlots(binding_for(tmp_path, kora_capture), clock, ORIGIN_NS + 10)

    with pytest.raises(KoraProtocolError) as unknown:
        slots.claim("unknown-role")
    clock.advance(10)
    with pytest.raises(KoraProtocolError) as expired:
        slots.claim("exec")

    assert_slot_refusal(unknown.value)
    assert_slot_refusal(expired.value)


def test_slots_admit_only_roles_captured_in_the_binding(
    tmp_path: Path, kora_capture: Capture
) -> None:
    roles = {"inspection": CandidateRoleEnvironment({}, ())}
    binding = bind_candidate_environments(
        cwd=tmp_path,
        attempt_token=kora_capture.scope.admit("synthetic-attempt"),
        workload={},
        secrets={},
        harness_credentials={},
        roles=roles,
    )
    roles["exec"] = roles.pop("inspection")
    slots = BoundRoleSlots(binding, StepClock(), ORIGIN_NS + 10)

    claim = slots.claim("inspection")
    attempt_value = slots.start(
        "inspection", claim, lambda environment: environment["KORA_ATTEMPT_TOKEN"]
    )
    with pytest.raises(KoraProtocolError) as refusal:
        slots.claim("exec")

    assert attempt_value == "synthetic-attempt"
    assert_slot_refusal(refusal.value)


@pytest.mark.parametrize("stop", [True, False], ids=("stopped", "expired"))
def test_fenced_claim_refuses_before_revealing(
    tmp_path: Path, kora_capture: Capture, stop: bool
) -> None:
    clock = StepClock()
    slots = BoundRoleSlots(binding_for(tmp_path, kora_capture), clock, ORIGIN_NS + 10)
    claim = slots.claim("exec")
    kora_capture.scope.release()
    if stop:
        slots.stop()
    else:
        clock.advance(10)

    with pytest.raises(KoraProtocolError) as refusal:
        slots.start("exec", claim, unexpected_acquisition)

    assert_slot_refusal(refusal.value)


def test_cutoff_crossed_during_preparation_refuses_acquisition(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = TickingClock(1)
    slots = BoundRoleSlots(binding_for(tmp_path, kora_capture), clock, ORIGIN_NS + 3)
    claim = slots.claim("app")

    with pytest.raises(KoraProtocolError) as refusal:
        slots.start("app", claim, unexpected_acquisition)

    assert_slot_refusal(refusal.value)
    assert clock.offset_ns == 3


def test_materialization_runs_before_the_final_cutoff_check(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = TickingClock(1)
    slots = BoundRoleSlots(binding_for(tmp_path, kora_capture), clock, ORIGIN_NS + 3)
    claim = slots.claim("app")
    kora_capture.scope.release()

    with pytest.raises(SecretRevealRefused):
        slots.start("app", claim, unexpected_acquisition)

    assert clock.offset_ns == 2


def test_failed_acquisition_keeps_slot_consumed_and_fences_other_roles(
    tmp_path: Path, kora_capture: Capture
) -> None:
    slots = BoundRoleSlots(
        binding_for(tmp_path, kora_capture), StepClock(), ORIGIN_NS + 10
    )
    claim = slots.claim("exec")

    def acquire(environment: dict[str, str]) -> NoReturn:
        assert "CODEX_API_KEY" not in environment
        raise OSError("native acquisition failed")

    with pytest.raises(OSError, match="^native acquisition failed$"):
        slots.start("exec", claim, acquire)
    with pytest.raises(KoraProtocolError) as replay:
        slots.start("exec", claim, unexpected_acquisition)
    with pytest.raises(KoraProtocolError) as other_role:
        slots.claim("app")

    assert_slot_refusal(replay.value)
    assert_slot_refusal(other_role.value)


def test_admitted_preflight_rechecks_cutoff_before_reveal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    slots = BoundRoleSlots(binding_for(tmp_path, kora_capture), clock, ORIGIN_NS + 10)
    app = slots.claim("app")
    kora_capture.scope.release()

    with pytest.raises(KoraProtocolError) as expired:
        slots.start(
            "app", app, unexpected_acquisition, preflight=lambda: clock.advance(10)
        )

    assert_slot_refusal(expired.value)
    assert slots.stopped


def test_foreign_claim_cannot_run_preflight_or_stop_the_owner(
    tmp_path: Path, kora_capture: Capture
) -> None:
    binding = binding_for(tmp_path, kora_capture)
    clock = StepClock()
    slots = BoundRoleSlots(binding, clock, ORIGIN_NS + 10)
    foreign = BoundRoleSlots(binding, clock, ORIGIN_NS + 10)
    app = slots.claim("app")
    invalid = foreign.claim("app")

    def unexpected_preflight() -> NoReturn:
        raise AssertionError("an invalid claim reached preflight")

    with pytest.raises(KoraProtocolError) as refused:
        slots.start(
            "app", invalid, unexpected_acquisition, preflight=unexpected_preflight
        )
    key = slots.start("app", app, lambda environment: environment["CODEX_API_KEY"])

    assert_slot_refusal(refused.value)
    assert not slots.stopped
    assert key == "synthetic-key"
