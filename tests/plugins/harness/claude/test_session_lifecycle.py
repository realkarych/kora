import asyncio
import json
import os
import socket
from pathlib import Path
from tempfile import mkdtemp

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import (
    CONFORMANCE_PRICE_TABLE,
    ConformancePolicy,
    ConformanceSink,
    Trace,
)
from kora.ir import ErrorClass
from kora.ir.config import NodeLimits
from kora.ir.json import JsonObject
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.adapter import ClaudeHarness, ClaudeSummary
from kora.plugins.harness.claude.stop import ClaudeObservation
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    Enforcement,
    EventSummary,
    Launch,
    LaunchInputs,
    RunnerCause,
    RunnerOutcome,
    SessionInputs,
    SessionResult,
    SessionSpec,
    prompt_digest,
)
from kora.protocols.policy import (
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.time import Clock, SystemClock
from kora.protocols.vcs import WorkspaceTarget
from tests.plugins.harness.claude.conformance_launch import (
    RECORDINGS,
    ClaudeLaunch,
    ClaudeLaunches,
)
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_replay_events import (
    context_for,
    init_fields,
    projector,
)


class ObservedHarness(ClaudeHarness):
    outcome: RunnerOutcome | None = None
    observation: ClaudeObservation | None = None
    decisions: tuple[str, ...] = ()

    def finalise(self, summary: EventSummary, outcome: RunnerOutcome) -> SessionResult:
        assert isinstance(summary, ClaudeSummary)
        self.observation = summary.events.observation()
        self.outcome = outcome
        return super().finalise(summary, outcome)


class RefusingLaunches(ClaudeLaunches):
    async def allocate(self, inputs: LaunchInputs) -> Launch:
        raise KoraProtocolError("launch_rejected", "launch refused", False, {})


class CleaningPolicy:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await self.release.wait()
            self.closed = True
        return PolicyDecision(True, "allowed")


class TeardownClock(SystemClock):
    def __init__(self) -> None:
        self.instant = super().monotonic_ns()
        self.deadline: int | None = None
        self.wait_count = 0
        self.waiting = asyncio.Event()
        self.expired = asyncio.Event()

    def monotonic_ns(self) -> int:
        return self.instant

    async def sleep_until(self, deadline_ns: int) -> None:
        self.wait_count += 1
        self.deadline = deadline_ns
        self.expired = asyncio.Event()
        self.waiting.set()
        await self.expired.wait()

    def advance(self) -> None:
        assert self.deadline is not None
        self.instant = self.deadline
        self.waiting.clear()
        self.expired.set()


class EscalationClock(TeardownClock):
    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.cancelling = asyncio.Event()
        self.release = asyncio.Event()

    async def sleep_until(self, deadline_ns: int) -> None:
        if self.started:
            await super().sleep_until(deadline_ns)
            return
        self.started = True
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelling.set()
            await self.release.wait()


@pytest.mark.asyncio
async def test_launch_report_handles_coalesced_notifications() -> None:
    before = asyncio.all_tasks()
    root = Path(mkdtemp(prefix="kc", dir="/tmp"))
    endpoint = root / "s"
    loop = asyncio.get_running_loop()
    launch: ClaudeLaunch | None = None
    try:
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender,
        ):
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.setblocking(False)
            sender.setblocking(False)
            await loop.sock_connect(sender, str(endpoint))
            await loop.sock_sendall(sender, b"1\ninitializing\ninterrupt\n")

            launch = ClaudeLaunch(root / "unused", listener)
            try:
                async with asyncio.timeout(5):
                    await launch.initializing.wait()
                    await launch.interrupted.wait()
            finally:
                sender.close()
                await launch.ended()

            assert launch.pid == 1
            assert launch.eof
            assert launch.report.done()
    finally:
        endpoint.unlink(missing_ok=True)
        root.rmdir()
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_teardown", [False, True])
async def test_run_cancellation_settles_policy_callbacks(
    claude_capture: Capture,
    expire_teardown: bool,
) -> None:
    before = asyncio.all_tasks()
    root = Path(mkdtemp(prefix="kc", dir="/tmp"))
    launches = ClaudeLaunches(
        root, RECORDINGS / "conformance-tool_calls-2.1.301.ndjson"
    )
    clock = TeardownClock()
    adapter = make_adapter(launches.workspace, context_for(claude_capture, clock))
    policy = CleaningPolicy()
    sink = ConformanceSink(None, Trace())
    task = asyncio.create_task(
        adapter.run(session_spec(adapter), policy, sink, launches)
    )

    try:
        async with asyncio.timeout(10):
            await policy.entered.wait()
            task.cancel()
            await policy.cleaning.wait()
            assert not task.done()
            if expire_teardown:
                await clock.waiting.wait()
                assert clock.deadline == clock.monotonic_ns() + 30_000_000_000
                clock.advance()
                assert launches.launch is not None
                await launches.launch.kill_requested.wait()
                assert not task.done()
            policy.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert policy.closed
        assert clock.wait_count == 1
        assert launches.launch is not None
        assert launches.launch.eof
        assert launches.launch.pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(launches.launch.pid, 0)
        assert asyncio.all_tasks() == before
    finally:
        policy.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launches.close()
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_run_cancellation_during_escalation_cleanup_reaps_launch(
    claude_capture: Capture,
) -> None:
    before = asyncio.all_tasks()
    root = Path(mkdtemp(prefix="kc", dir="/tmp"))
    launches = ClaudeLaunches(root, RECORDINGS / "conformance-bypass-2.1.301.ndjson")
    clock = EscalationClock()
    adapter = make_adapter(launches.workspace, context_for(claude_capture, clock))
    sink = ConformanceSink(None, Trace())
    policy = ConformancePolicy("Forbidden", sink.trace)
    task = asyncio.create_task(
        adapter.run(session_spec(adapter), policy, sink, launches)
    )

    try:
        async with asyncio.timeout(10):
            await clock.cancelling.wait()
            task.cancel()
            clock.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert launches.launch is not None
        assert launches.launch.interrupted.is_set()
        assert launches.launch.eof
        assert launches.launch.report.done()
        assert launches.launch.pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(launches.launch.pid, 0)
        assert asyncio.all_tasks() == before
    finally:
        clock.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launches.close()


class StoppingSink(ConformanceSink):
    def __init__(
        self, cause: RunnerCause | None = None, max_events: int | None = None
    ) -> None:
        super().__init__(max_events, Trace())
        self.cause: RunnerCause | None = cause

    def emit(self, kind: str, data: JsonObject) -> None:
        super().emit(kind, data)
        if kind == "session_started" and self.cause is not None:
            self.stop(self.cause)


class HeldStartupStopSink(ConformanceSink):
    def __init__(self, cause: RunnerCause | None) -> None:
        super().__init__(None, Trace())
        self.cause: RunnerCause | None = cause
        self.observing = asyncio.Event()
        self.observed = False

    def enforcement(self) -> Enforcement:
        if self.observed and self.cause is not None:
            return Enforcement("terminate", self.cause)
        return super().enforcement()

    async def termination(self) -> RunnerCause:
        self.observing.set()
        return await super().termination()


def session_spec(
    adapter: ClaudeHarness,
    limits: NodeLimits | None = None,
    config: JsonObject | None = None,
) -> SessionSpec:
    return adapter.build_session_spec(
        {} if config is None else config,
        WorkspaceTarget(root=adapter.scope.workspace.root, host_path=None),
        None,
        inputs=SessionInputs(
            "conformance",
            prompt_digest("conformance"),
            CONFORMANCE_PRICE_TABLE.id,
            CONFORMANCE_PRICE_TABLE,
            NodeLimits() if limits is None else limits,
        ),
    )


async def run_lifecycle(
    capture: Capture,
    case: str,
    *,
    cause: RunnerCause | None = None,
    kill: bool = False,
    version: str = "2.1.301",
    refused: bool = False,
    recording: Path | None = None,
    clock: Clock | None = None,
    output_format: bool = False,
    sink: StoppingSink | None = None,
    policy: PolicyClient | None = None,
    registry: SecretRegistry | None = None,
) -> tuple[SessionResult, RunnerOutcome, ObservedHarness]:
    before = asyncio.all_tasks()
    root = Path(mkdtemp(prefix="kc", dir="/tmp"))
    factory = RefusingLaunches if refused else ClaudeLaunches
    launches = factory(
        root,
        RECORDINGS / f"conformance-{case}-2.1.301.ndjson"
        if recording is None
        else recording,
        version,
    )
    base = make_adapter(
        launches.workspace,
        context_for(capture, SystemClock() if clock is None else clock),
        output_format,
    )
    adapter = ObservedHarness(
        base.scope,
        base.ctx,
        base.registry if registry is None else registry,
        base.environment,
    )
    sink = StoppingSink(cause) if sink is None else sink
    policy = ConformancePolicy("Forbidden", sink.trace) if policy is None else policy
    operation = asyncio.create_task(
        adapter.run(session_spec(adapter), policy, sink, launches)
    )
    try:
        async with asyncio.timeout(10):
            if kill and version == "ignore_initialize":
                await launches.allocated.wait()
                assert launches.launch is not None
                await launches.launch.initializing.wait()
                adapter.kill()
            elif kill:
                await sink.termination()
                assert launches.launch is not None
                await launches.launch.interrupted.wait()
                adapter.kill()
                adapter.kill()
            if isinstance(clock, TeardownClock):
                await launches.allocated.wait()
                assert launches.launch is not None
                await launches.launch.interrupted.wait()
                await clock.waiting.wait()
                assert clock.deadline == clock.monotonic_ns() + 30_000_000_000
                clock.advance()
            result = await operation
        assert adapter.outcome is not None
        if launches.launch is not None:
            launch = launches.launch
            assert launch.eof
            assert launch.report.done()
            assert launch.pid is not None
            with pytest.raises(ProcessLookupError):
                os.kill(launch.pid, 0)
            assert await launch.ended() == await launch.ended()
        if version != "2.1.301":
            assert not launches.recording.with_suffix(".prompt").exists()
        if refused:
            assert launches.launch is None
        decisions = launches.recording.with_suffix(".decisions")
        if decisions.exists():
            adapter.decisions = tuple(decisions.read_text().splitlines())
        return result, adapter.outcome, adapter
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await launches.close()
        assert not root.exists()
        assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_result_tears_down_after_result(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(claude_capture, "no_usage")

    assert result.error_class is None
    assert outcome.termination == "teardown_after_result"


@pytest.mark.asyncio
async def test_interrupt_acknowledged(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "interrupt", cause="cancel"
    )

    assert result.error_class is ErrorClass.CANCELLED
    assert outcome.termination == "interrupted"


@pytest.mark.asyncio
async def test_unanswered_interrupt_does_not_block_kill(
    claude_capture: Capture,
) -> None:
    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "ignore_interrupt", cause="deadline", kill=True
    )

    assert result.error_class is ErrorClass.TIMEOUT
    assert outcome.termination == "killed_after_grace"
    assert outcome.signal == 9


def oversized_recording(root: Path) -> Path:
    lines = (RECORDINGS / "conformance-usage-2.1.301.ndjson").read_text().splitlines()
    path = root / "oversized.ndjson"
    path.write_text(
        "\n".join(lines[:4])
        + '\n{"type":"system","subtype":"oversized","value":"'
        + "x" * 1_100_000
        + '"}\n'
    )
    return path


@pytest.mark.asyncio
async def test_oversized_message_kills_at_once(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recording = oversized_recording(tmp_path)

    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "usage", recording=recording
    )

    assert result.stop_reason == "oversized_message"
    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert result.usage_coverage.completeness == "partial"
    assert result.usage[0].input_tokens == 10
    assert outcome.termination == "killed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "expected_error"),
    [
        (None, ErrorClass.HARNESS_ERROR),
        ("deadline", ErrorClass.TIMEOUT),
        ("cancel", ErrorClass.CANCELLED),
    ],
)
async def test_oversized_initialize_preserves_observed_stop(
    claude_capture: Capture, stop: RunnerCause | None, expected_error: ErrorClass
) -> None:
    before = asyncio.all_tasks()
    root = Path(mkdtemp(prefix="kc", dir="/tmp"))
    launches = ClaudeLaunches(
        root, RECORDINGS / "conformance-no_usage-2.1.301.ndjson", "oversized_initialize"
    )
    base = make_adapter(launches.workspace, context_for(claude_capture, SystemClock()))
    adapter = ObservedHarness(base.scope, base.ctx, base.registry, base.environment)
    sink = HeldStartupStopSink(stop)
    policy = ConformancePolicy("Forbidden", sink.trace)
    spec = session_spec(adapter)

    operation = asyncio.create_task(adapter.run(spec, policy, sink, launches))
    try:
        async with asyncio.timeout(10):
            await launches.allocated.wait()
            launch = launches.launch
            assert launch is not None
            await launch.initializing.wait()
            await sink.observing.wait()
            sink.observed = True
            launches.release_pause()
            result = await operation
            prompt_received = launches.recording.with_suffix(".prompt").exists()
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await launches.close()

    assert result.stop_reason == ("oversized_message" if stop is None else stop)
    assert result.error_class is expected_error
    assert not sink.future().done()
    assert adapter.outcome is not None
    assert adapter.outcome.termination == "killed"
    assert not prompt_received
    assert launch.eof
    assert launch.report.done()
    assert not root.exists()
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_cli_exit_without_result(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(claude_capture, "exit")

    assert result.stop_reason == "cli_exit_without_result"
    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.exit_code == 0


@pytest.mark.asyncio
async def test_allocation_refused(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "no_usage", refused=True
    )

    assert result.stop_reason == "launch_rejected"
    assert result.error_class is ErrorClass.CONFIG_ERROR
    assert outcome.termination == "none"


@pytest.mark.asyncio
async def test_version_mismatch_never_sends_prompt(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "no_usage", version="2.1.300"
    )

    assert result.stop_reason == "version_mismatch"
    assert result.error_class is ErrorClass.CONFIG_ERROR
    assert outcome.termination == "none"


def test_finalise_refuses_foreign_summary(claude_capture: Capture) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))

    with pytest.raises(KoraProtocolError, match="another adapter") as error:
        adapter.finalise(object(), RunnerOutcome("completed", "none", None, None))

    assert error.value.code == "summary_foreign"


@pytest.mark.asyncio
async def test_kill_during_initialize(claude_capture: Capture) -> None:
    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "no_usage", version="ignore_initialize", kill=True
    )

    assert result.error_class is ErrorClass.CANCELLED
    assert outcome.termination == "killed_after_grace"
    assert outcome.signal == 9


@pytest.mark.asyncio
async def test_sdk_parse_error_starts_bounded_teardown(
    claude_capture: Capture, tmp_path: Path
) -> None:
    init = (
        (RECORDINGS / "conformance-no_usage-2.1.301.ndjson").read_text().splitlines()[0]
    )
    recording = tmp_path / "malformed.ndjson"
    recording.write_text(
        init
        + '\n{"type":"assistant","message":{}}\n{"kora_replay":"await_interrupt"}\n'
    )

    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "no_usage", recording=recording
    )

    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.cause == "cli_exit_without_result"


@pytest.mark.asyncio
async def test_internal_bypass_escalates_without_host_kill(
    claude_capture: Capture, tmp_path: Path
) -> None:
    source = (RECORDINGS / "conformance-bypass-2.1.301.ndjson").read_text()
    recording = tmp_path / "ignored-bypass.ndjson"
    recording.write_text(source.replace('"await_interrupt"', '"ignore_interrupt"'))

    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "bypass", recording=recording, clock=TeardownClock()
    )

    assert result.policy_bypass
    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.cause == "policy_bypass"
    assert outcome.termination == "killed_after_grace"
    assert outcome.signal == 9


@pytest.mark.asyncio
async def test_successful_denied_result_interrupts_session(
    claude_capture: Capture, tmp_path: Path
) -> None:
    lines = (
        (RECORDINGS / "conformance-tool_calls-2.1.301.ndjson").read_text().splitlines()
    )
    recording = tmp_path / "denied-success.ndjson"
    lines[6] = lines[6].replace('"is_error":true', '"is_error":false')
    lines.insert(7, '{"kora_replay":"await_interrupt"}')
    recording.write_text("\n".join(lines) + "\n")

    result, outcome, _adapter = await run_lifecycle(
        claude_capture, "tool_calls", recording=recording
    )

    assert result.policy_bypass
    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.cause == "policy_bypass"
    assert outcome.termination == "interrupted"


@pytest.mark.asyncio
async def test_invalid_structured_output_returns_diagnostic(
    claude_capture: Capture, tmp_path: Path
) -> None:
    lines = (
        (RECORDINGS / "conformance-no_usage-2.1.301.ndjson").read_text().splitlines()
    )
    terminal: dict[str, object] = json.loads(lines[-1])
    terminal["structured_output"] = json.loads("[" * 17 + "0" + "]" * 17)
    terminal["result"] = '{"fallback":true}'
    lines[-1] = json.dumps(terminal)
    recording = tmp_path / "invalid-output.ndjson"
    recording.write_text("\n".join(lines) + "\n")

    result, _, _adapter = await run_lifecycle(
        claude_capture, "no_usage", recording=recording
    )

    assert result.error_class is None
    assert result.output is None
    assert result.output_diagnostic == "structured_output_invalid"


@pytest.mark.parametrize(
    ("cause", "diagnostic"),
    [("completed", "structured_output_invalid"), ("deadline", None)],
)
def test_finalise_refused_output_preserves_classification(
    claude_capture: Capture, cause: RunnerCause, diagnostic: str | None
) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    _, events = projector()
    events.observe_init(init_fields())
    events.observe_result(
        "success",
        False,
        None,
        {},
        structured_output=json.loads("[" * 17 + "0" + "]" * 17),
        result='{"fallback":true}',
    )

    result = adapter.finalise(
        ClaudeSummary("claude", events),
        RunnerOutcome(
            cause,
            "interrupted" if cause == "deadline" else "teardown_after_result",
            0,
            None,
        ),
    )

    assert result.output is None
    assert result.output_diagnostic == diagnostic
    assert result.error_class is (ErrorClass.TIMEOUT if cause == "deadline" else None)
