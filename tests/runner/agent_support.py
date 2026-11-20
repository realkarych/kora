import asyncio
import errno
import json
import os
import threading
from collections.abc import AsyncGenerator, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, TypeVar

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.fake import CallScope, FakeHarness, ScriptHeader
from kora.plugins.harness.fake.script import PauseStep, Step
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ClockSample, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution
from kora.protocols.policy import (
    Policy,
    PolicyCapabilities,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.runtime import Workload
from kora.protocols.time import Clock, SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import (
    AgentHost,
    HostedHarness,
    HostServices,
    Released,
)
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.journal import BatchAppend, JournalAppendOutcome, RunningJournal
from kora.runner.sink import BATCH_WINDOW_NS, JournalWriter, RunnerSink
from tests.plugins.harness.fake.fake_support import step
from tests.protocols.runtime_documents import ATTEMPT, workload

FIXED_NOW: Final[datetime] = datetime(2026, 11, 12, tzinfo=UTC)
TEST_DEADLINE_SECONDS: Final[float] = 60.0
T = TypeVar("T")


def run(work: Coroutine[object, object, T]) -> T:
    return asyncio.run(asyncio.wait_for(work, TEST_DEADLINE_SECONDS))


class GatedClock:
    def __init__(self) -> None:
        self.current = 0
        self.deadlines: list[int] = []
        self.released: set[int] = set()
        self.changed = asyncio.Condition()

    def now(self) -> datetime:
        return FIXED_NOW + timedelta(microseconds=self.current // 1000)

    def monotonic_ns(self) -> int:
        return self.current

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.current)

    async def advance_to(self, instant_ns: int) -> None:
        async with self.changed:
            if instant_ns < self.current:
                raise ValueError("time cannot move backwards")
            self.current = instant_ns
            self.changed.notify_all()

    async def sleep_until(self, deadline_ns: int) -> None:
        async with self.changed:
            self.deadlines.append(deadline_ns)
            self.changed.notify_all()
            await self.changed.wait_for(lambda: deadline_ns in self.released)

    async def requested(self, deadline_ns: int, count: int = 1) -> None:
        async with self.changed:
            await self.changed.wait_for(
                lambda: self.deadlines.count(deadline_ns) >= count
            )

    async def release(self, deadline_ns: int) -> None:
        async with self.changed:
            self.released.add(deadline_ns)
            self.changed.notify_all()


class SteppedClock:
    def __init__(self) -> None:
        self.current = 0
        self.deadlines: list[int] = []
        self.changed = asyncio.Condition()

    def now(self) -> datetime:
        return FIXED_NOW + timedelta(microseconds=self.current // 1000)

    def monotonic_ns(self) -> int:
        return self.current

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.current)

    async def sleep_until(self, deadline_ns: int) -> None:
        async with self.changed:
            self.deadlines.append(deadline_ns)
            self.changed.notify_all()
            await self.changed.wait_for(lambda: self.current >= deadline_ns)

    async def requested(self, deadline_ns: int, count: int = 1) -> None:
        async with self.changed:
            await self.changed.wait_for(
                lambda: self.deadlines.count(deadline_ns) >= count
            )

    async def advance_to(self, instant_ns: int) -> None:
        async with self.changed:
            if instant_ns < self.current:
                raise ValueError("time cannot move backwards")
            self.current = instant_ns
            self.changed.notify_all()


async def advance_batch(clock: GatedClock | SteppedClock) -> None:
    await clock.advance_to(clock.current + BATCH_WINDOW_NS)


class HeldWriter:
    def __init__(
        self,
        outcome: JournalAppendOutcome = JournalAppendOutcome.DURABLE,
        journal: RunningJournal | None = None,
    ) -> None:
        self.journal = journal
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.outcome = outcome
        self.appended: list[tuple[str, JsonObject]] = []
        self.batches: list[tuple[tuple[str, JsonObject], ...]] = []

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        if self.journal is not None and kind.startswith("runner."):
            return self.journal.append_outcome(kind, data)
        self.entered.set()
        self.gate.wait()
        self.appended.append((kind, data))
        if self.journal is not None:
            return self.journal.append_outcome(kind, data)
        return self.outcome

    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend:
        self.entered.set()
        self.gate.wait()
        self.batches.append(tuple(records))
        self.appended.extend(records)
        if self.journal is not None:
            return self.journal.append_batch(records, cap_bytes=cap_bytes)
        return BatchAppend(len(records), self.outcome)


class FailingWriter:
    def __init__(self) -> None:
        self.attempts = 0

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        self.attempts += 1
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend:
        self.attempts += 1
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class RaisingPolicy:
    name: str = "raising"
    config_schema: JsonObject = {"type": "object"}

    def capabilities(self) -> PolicyCapabilities:
        return PolicyCapabilities()

    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return []

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        raise KoraProtocolError("policy_failed", "the policy failed", False, {})


class SignalledScript:
    def __init__(self, header: ScriptHeader, steps: Sequence[Step]) -> None:
        self.header = header
        self.listed = tuple(steps)
        self.paused = asyncio.Event()

    async def steps(self) -> AsyncGenerator[Step]:
        for item in self.listed:
            if isinstance(item, PauseStep):
                self.paused.set()
            yield item


def script(
    documents: Sequence[JsonObject],
    end: Literal["completed", "cli_exit_without_result", "oversized_message"] = (
        "completed"
    ),
    on_interrupt: Literal["acknowledge", "ignore"] = "acknowledge",
) -> SignalledScript:
    header = ScriptHeader(end=end, on_interrupt=on_interrupt)
    return SignalledScript(header, tuple(step(document) for document in documents))


PAUSE: Final[JsonObject] = {"kind": "pause"}
FAKE_SPEC: Final[JsonObject] = {
    "harness": "fake",
    "adapter_version": "fake-1",
    "options": {},
    "files": [],
    "prompt": "",
    "resume_handle": None,
    "price_table_id": "fake",
    "price_table": {},
}


class DeafHarness(FakeHarness):
    def kill(self) -> None:
        return None


@dataclass
class FakeResolver:
    source: SignalledScript
    ctx: ProtocolContext
    deaf: bool = False
    adapters: list[FakeHarness] = field(default_factory=lambda: [])

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        scope = CallScope(
            harness=execution.harness,
            workspace=workspace,
            node_path=execution.node_path,
            layers=execution.layers(),
            protected_paths=(),
            output_format=execution.output_format,
        )
        harness = DeafHarness if self.deaf else FakeHarness
        adapter = harness(self.source, scope, self.ctx)
        self.adapters.append(adapter)
        return HostedHarness(adapter, adapter.kill)


def execution(deny: tuple[str, ...] = (), **changes: JsonValue) -> JsonObject:
    document: JsonObject = {
        "harness": "claude",
        "node_path": "graph.node",
        "runtime_attempt": 1,
        "policy_layers": {
            "namespace": {"allow": [], "deny": []},
            "node": {"allow": [], "deny": list(deny)},
        },
        "output_format": True,
    }
    return {**document, **changes}


def private_directory(path: Path) -> DirectoryAnchor:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return DirectoryAnchor.open_path(path)


@dataclass
class World:
    root: Path
    capture: Capture
    clock: GatedClock
    attempt: DirectoryAnchor
    control: DirectoryAnchor
    journal: RunningJournal

    @property
    def candidate(self) -> str:
        return str(self.root / "candidate")

    def workspace(self, root: str | None = None) -> WorkspaceRef:
        return WorkspaceRef(
            attempt_id=ATTEMPT,
            root=self.candidate if root is None else root,
            repo="example/repo",
            commit="c0ffee",
            base_commit="c0ffee",
            branch=None,
        )

    def workload(
        self, *, deadline: ExecutionDeadline | None = None, **changes: object
    ) -> Workload:
        document = workload(
            execution_request=execution(),
            limits={"stall_after_us": "900000000"},
            session_spec=FAKE_SPEC,
        )
        parsed = Workload.model_validate_json(json.dumps({**document, **changes}))
        return parsed.model_copy(update={"deadline": deadline})

    def host(
        self,
        resolver: FakeResolver,
        policy: Policy | None = None,
        journal: JournalWriter | None = None,
        clock: Clock | None = None,
    ) -> AgentHost:
        services = HostServices(
            resolver=resolver,
            policy=AllowDenyPolicy() if policy is None else policy,
            ctx=context_for(self.capture),
            registry=self.capture.diagnostics.registry,
            clock=self.clock if clock is None else clock,
            log=self.capture.diagnostics.logger("runner.agent"),
        )
        writer = self.journal if journal is None else journal
        return AgentHost(services, writer, self.control, self.candidate)

    def resolver(self, source: SignalledScript, deaf: bool = False) -> FakeResolver:
        return FakeResolver(source, context_for(self.capture), deaf)

    def records(self) -> list[JsonObject]:
        path = self.root / "attempt" / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def kinds(self) -> list[str]:
        return [str(record["kind"]) for record in self.records()]

    def close(self) -> None:
        self.journal.close()
        self.attempt.close()
        self.control.close()


def make_world(root: Path, capture: Capture) -> World:
    root.mkdir(parents=True, exist_ok=True)
    attempt = private_directory(root / "attempt")
    control = private_directory(root / "control")
    (root / "candidate").mkdir()
    journal = RunningJournal(attempt, SystemClock())
    return World(root, capture, GatedClock(), attempt, control, journal)


@dataclass
class JournalProxy:
    journal: RunningJournal
    on: str
    action: Literal["fence", "close", "fail", "hold"]
    host: AgentHost | None = None
    gate: threading.Event = field(default_factory=threading.Event)

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        if kind == self.on and self.action == "fail":
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        if kind == self.on and self.action == "hold":
            self.gate.wait()
        if kind == self.on and self.action == "fence" and self.host is not None:
            self.host.fence()
        if kind == self.on and self.action in {"fence", "close"}:
            self.journal.close()
        return self.journal.append_outcome(kind, data)

    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend:
        if any(kind == self.on for kind, _ in records):
            if self.action == "fail":
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            if self.action == "hold":
                self.gate.wait()
            if self.action == "fence" and self.host is not None:
                self.host.fence()
            if self.action in {"fence", "close"}:
                self.journal.close()
        return self.journal.append_batch(records, cap_bytes=cap_bytes)


def sink_for(
    writer: HeldWriter | FailingWriter | RunningJournal,
    registry: SecretRegistry | None = None,
    max_events: int | None = None,
) -> RunnerSink:
    return RunnerSink(
        writer,
        SecretRegistry() if registry is None else registry,
        SystemClock(),
        max_events,
        900_000_000_000,
    )


async def drained(sink: RunnerSink) -> tuple[bool, bool]:
    clock = SystemClock()
    complete = await sink.settle(clock.monotonic_ns() + 10_000_000_000)
    sink.close()
    return complete, await sink.joined(clock.monotonic_ns() + 10_000_000_000)


def journal_threads_exited() -> bool:
    for thread in threading.enumerate():
        if thread.name == "kora-agent-journal":
            thread.join(TEST_DEADLINE_SECONDS)
    return all(thread.name != "kora-agent-journal" for thread in threading.enumerate())


async def hosted(
    world: World,
    source: SignalledScript,
    journal: JournalWriter | None = None,
    policy: Policy | None = None,
    deadline: ExecutionDeadline | None = None,
    **changes: object,
) -> tuple[AgentHost, Released]:
    host = world.host(world.resolver(source), policy=policy, journal=journal)
    host.start(world.workload(deadline=deadline, **changes), world.workspace())
    await host.finished()
    return host, await host.stop()
