import asyncio
import os
from pathlib import Path

import pytest

from kora.conformance import harness as checks
from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.harness import kora_capture as kora_capture
from kora.conformance.harness import kora_harness_host as kora_harness_host
from kora.conformance.harness import (
    test_harness_events_are_normalised,
    test_harness_max_events_denies_at_exact_count,
    test_harness_policy_precedes_every_tool_call,
    test_harness_structured_output_and_fallbacks,
    test_harness_usage_completeness,
)
from kora.runner.filesystem import DirectoryAnchor, create_directory, remove_directory
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.launching_context import (
    CaseLaunches,
    ContextOwner,
)
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.launching_harness import (
    Adapter,
    Exercise,
    LaunchingHarness,
)

__all__ = [
    "test_harness_events_are_normalised",
    "test_harness_max_events_denies_at_exact_count",
    "test_harness_policy_precedes_every_tool_call",
    "test_harness_structured_output_and_fallbacks",
    "test_harness_usage_completeness",
]


@pytest.fixture
def kora_harness_exercise(tmp_path: Path) -> Exercise:
    return Exercise(ContextOwner(tmp_path))


@pytest.mark.asyncio
async def test_launching_case_tree_supports_parent_cleanup(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    try:
        tree = create_directory(parent, "case")
        try:
            case = CaseLaunches(tmp_path / "case", "success", asyncio.Event())
            for directory in (case.workspace, *case.placements.values()):
                (directory / "effect").write_text("owned")

            remove_directory(parent, tree.identity)

            assert not case.root.exists()
        finally:
            tree.close()
    finally:
        parent.close()


def assert_released(owner: ContextOwner) -> None:
    assert owner.active is None
    assert owner.cases
    for case in owner.cases:
        assert case.closed
        assert case.loop is case.cleanup_loop
        assert not case.root.exists()
        assert not case.workspace.exists()
        assert all(not directory.exists() for directory in case.placements.values())
        launch = case.launch
        if launch is not None:
            assert launch.closed
            if launch.process is not None:
                assert launch.process.returncode is not None
                assert launch.process.stdin is not None
                assert launch.process.stdin.is_closing()
                assert launch.process.stdout is not None
                assert launch.process.stdout.at_eof()
                with pytest.raises(ProcessLookupError):
                    os.kill(launch.process.pid, 0)


def test_launching_harness_pytest_uses_case_context(
    tmp_path: Path, kora_capture: Capture
) -> None:
    exercise = Exercise(ContextOwner(tmp_path))

    first = checks.hosted(
        checks.HarnessHost(), exercise, kora_capture, checks.HarnessCase.TOOL_CALLS
    )
    second = checks.hosted(
        checks.HarnessHost(), exercise, kora_capture, checks.HarnessCase.NO_USAGE
    )

    checks.check_harness_policy_precedes_every_tool_call(first)
    checks.check_harness_events_are_normalised(second)
    assert len(exercise.owner.cases) == 2
    assert exercise.owner.cases[0].root != exercise.owner.cases[1].root
    for case in exercise.owner.cases:
        assert case.launch is not None
        assert case.launch.handshake is not None
        assert case.launch.handshake.cwd == str(case.workspace)
        assert len(set(case.placements.values())) == 4
        assert all(
            not path.is_relative_to(case.workspace) for path in case.placements.values()
        )
    assert_released(exercise.owner)


async def exercise_ending(exercise: Exercise, capture: Capture) -> None:
    context = context_for(capture)
    provider = LaunchingHarness(exercise.owner)
    operation = asyncio.create_task(
        checks.run_harness_case(
            checks.HarnessHost(),
            provider,
            {"prefix": "retained"},
            context,
            exercise.inputs,
            checks.HarnessCase.TOOL_CALLS,
        )
    )
    try:
        async with asyncio.timeout(10):
            if exercise.owner.mode == "cancel":
                await provider.started.wait()
                operation.cancel()
            elif exercise.owner.mode == "startup_cancel":
                await exercise.owner.startup_pause.wait()
                operation.cancel()
            elif exercise.owner.mode in {"acquisition_cancel", "prepare_cancel"}:
                await exercise.owner.entered.wait()
                operation.cancel()
            elif exercise.owner.mode == "cleanup_cancel":
                await exercise.owner.cleanup_entered.wait()
                operation.cancel()
                barrier = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(barrier.set_result, None)
                await barrier
                operation.cancel()
                exercise.owner.cleanup_release.set()
            await operation
    finally:
        if not operation.done():
            operation.cancel()
            exercise.owner.cleanup_release.set()
            await asyncio.gather(operation, return_exceptions=True)
        await Adapter(exercise.owner).cleanup(provider)


@pytest.mark.parametrize(
    "ending",
    [
        "success",
        "nonzero",
        "failure",
        "cancel",
        "kill",
        "prepare_failure",
        "spec_failure",
        "allocation_failure",
        "acquisition_failure",
        "acquisition_cancel",
        "startup_cancel",
        "prepare_cancel",
        "cleanup_cancel",
    ],
)
def test_launching_harness_releases_every_ending(
    tmp_path: Path, kora_capture: Capture, ending: str
) -> None:
    exercise = Exercise(ContextOwner(tmp_path, ending))
    with asyncio.Runner() as runner:
        operation = exercise_ending(exercise, kora_capture)

        if "cancel" in ending:
            with pytest.raises(asyncio.CancelledError):
                runner.run(operation)
        elif "failure" in ending:
            with pytest.raises(RuntimeError, match="failed"):
                runner.run(operation)
        else:
            runner.run(operation)

        assert not asyncio.all_tasks(runner.get_loop())
        assert_released(exercise.owner)
        for case in exercise.owner.cases:
            if case.launch is not None:
                assert runner.run(case.launch.ended()) == runner.run(
                    case.launch.ended()
                )
                runner.run(case.launch.close())
                assert not case.root.exists()


@pytest.mark.parametrize("defective", [False, True])
def test_launching_harness_denial_prevents_execution(
    tmp_path: Path, kora_capture: Capture, defective: bool
) -> None:
    exercise = Exercise(
        ContextOwner(tmp_path, "denied_execution" if defective else "success")
    )

    if defective:
        with pytest.raises(AssertionError, match="denied operation executed"):
            checks.hosted(
                checks.HarnessHost(),
                exercise,
                kora_capture,
                checks.HarnessCase.TOOL_CALLS,
            )
    else:
        result = checks.hosted(
            checks.HarnessHost(), exercise, kora_capture, checks.HarnessCase.TOOL_CALLS
        )
        checks.check_harness_policy_precedes_every_tool_call(result)

    assert_released(exercise.owner)


@pytest.mark.parametrize("configuration", [{}, {"prefix": "retained-"}])
def test_launching_harness_preserves_configured_provider(
    tmp_path: Path, kora_capture: Capture, configuration: dict[str, str]
) -> None:
    exercise = Exercise(ContextOwner(tmp_path))
    provider = LaunchingHarness(exercise.owner)
    context = context_for(kora_capture)

    with asyncio.Runner() as runner:
        run = runner.run(
            checks.run_harness_case(
                checks.HarnessHost(),
                provider,
                dict(configuration),
                context,
                exercise.inputs,
                checks.HarnessCase.NO_USAGE,
            )
        )

        assert not asyncio.all_tasks(runner.get_loop())
    assert any(
        event.kind == "turn"
        and event.data.message_id == configuration.get("prefix", "") + "m1"
        for event in run.events
    )
    assert provider.owner is exercise.owner
    assert_released(exercise.owner)
