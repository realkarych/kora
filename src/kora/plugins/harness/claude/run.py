import asyncio
from pathlib import Path
from typing import Literal, TypeVar, cast

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIJSONDecodeError,
    ResultMessage,
    SystemMessage,
)

from kora.plugins.harness.claude.configuration import (
    CLI_VERSION,
    ClaudeEnvironmentConfiguration,
)
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.plugins.harness.claude.options import ClaudeSessionOptions, agent_options
from kora.plugins.harness.claude.policy import PolicyBridge
from kora.plugins.harness.claude.sdk import feed
from kora.plugins.harness.claude.trust import surface_cause
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EventSink,
    Launch,
    LaunchContext,
    LaunchEnd,
    RunnerCause,
    RunnerOutcome,
    SessionSpec,
)
from kora.protocols.policy import PolicyClient
from kora.runner.calls import CallScope

T = TypeVar("T")
INTERRUPT_GRACE_NS = 30_000_000_000
TEARDOWN_NS = INTERRUPT_GRACE_NS
Termination = Literal[
    "none", "interrupted", "killed", "killed_after_grace", "teardown_after_result"
]


async def settled(task: asyncio.Task[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def cancel_tasks(tasks: tuple[asyncio.Task[object], ...]) -> None:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class ClaudeSession:
    def __init__(
        self,
        scope: CallScope,
        ctx: ProtocolContext,
        events: ClaudeEvents,
        sink: EventSink,
        policy: PolicyClient,
        killed: asyncio.Event,
    ) -> None:
        self.scope = scope
        self.ctx = ctx
        self.events = events
        self.sink = sink
        self.killed = killed
        self.stopped = asyncio.Event()
        self.cause: RunnerCause | None = None
        self.connecting = False
        self.surface_checked = False
        self.surface_refused = False
        self.bridge = PolicyBridge(
            scope, policy, sink, events, self.stop, self.refusing, self.checked
        )
        self.reader: asyncio.Task[bool] | None = None
        self.interrupt: asyncio.Task[None] | None = None
        self.escalation: asyncio.Task[None] | None = None

    def refusing(self) -> bool:
        return self.surface_refused

    def checked(self) -> bool:
        return self.surface_checked

    def observe_surface(self, data: object) -> None:
        surface = surface_cause(data, output_format=self.scope.output_format)
        if surface is None:
            self.surface_checked = True
        else:
            self.stop(surface)

    def stop(self, cause: RunnerCause, *, external: bool = False) -> None:
        self.surface_refused |= cause in {
            "version_mismatch",
            "plugin_mismatch",
            "config_check_failed",
        }
        if self.cause is None:
            self.cause = cause
            self.stopped.set()
            if not external:
                self.escalation = asyncio.create_task(self.escalate())

    def startup_stop(self) -> RunnerCause | None:
        if self.cause is None:
            cause = self.sink.enforcement().cause
            if cause is not None:
                self.stop(cause, external=True)
            elif self.killed.is_set():
                self.stop("cancel", external=True)
        return self.cause

    async def escalate(self) -> None:
        await self.ctx.clock.sleep_until(
            self.ctx.clock.monotonic_ns() + INTERRUPT_GRACE_NS
        )
        self.killed.set()

    async def read(self, client: ClaudeSDKClient) -> bool:
        async for message in client.receive_messages():
            feed(self.events, message)
            if isinstance(message, SystemMessage) and message.subtype == "init":
                self.observe_surface(message.data)
            cause = self.sink.enforcement().cause
            if cause is not None:
                self.stop(cause)
            if self.events.bypass_confirmed:
                self.stop("policy_bypass")
            if isinstance(message, ResultMessage):
                return True
        return False

    async def run(
        self,
        spec: SessionSpec,
        launches: LaunchContext,
        environment: ClaudeEnvironmentConfiguration,
    ) -> RunnerOutcome:
        options = ClaudeSessionOptions.model_validate(spec.options)
        if (cause := self.startup_stop()) is not None:
            return RunnerOutcome(cause, "none", None, None)
        try:
            inputs = environment.launch_inputs(
                cwd=Path(self.scope.workspace.root), placements=launches.placements
            )
            launch = await launches.allocate(inputs)
        except KoraProtocolError as error:
            return RunnerOutcome(
                self.startup_stop()
                or (
                    "launch_rejected"
                    if error.code == "launch_rejected"
                    else "launch_failed"
                ),
                "none",
                None,
                None,
            )
        client = ClaudeSDKClient(
            options=agent_options(
                options,
                cli_path=launch.cli_path,
                inputs=inputs,
                hook=self.bridge.hook,
            )
        )
        exchange = asyncio.create_task(self.exchange(client, launch, spec.prompt))
        external = asyncio.create_task(self.external_stop(client))
        killed = asyncio.create_task(self.killed.wait())
        try:
            await asyncio.wait({exchange, killed}, return_when=asyncio.FIRST_COMPLETED)
            if self.killed.is_set():
                await launch.kill()
                cause = self.startup_stop() or "cancel"
                termination: Termination = "killed_after_grace"
            elif (
                self.connecting
                and type(exchange.exception()) is Exception
                and (cause := self.startup_stop()) is not None
            ):
                termination = "interrupted"
            else:
                cause, termination = exchange.result()
        finally:
            cleanup = asyncio.create_task(
                self.teardown(client, launch, (exchange, external, killed))
            )
            end = await settled(cleanup)
        if cause is None:
            cause, termination = self.ending(end)
        self.events.finish_session()
        return RunnerOutcome(cause, termination, end.exit_code, end.signal)

    async def external_stop(self, client: ClaudeSDKClient) -> None:
        self.stop(await self.sink.termination(), external=True)
        self.start_interrupt(client)

    def start_interrupt(self, client: ClaudeSDKClient) -> None:
        if self.interrupt is None:
            self.interrupt = asyncio.create_task(client.interrupt())

    async def exchange(
        self, client: ClaudeSDKClient, launch: Launch, prompt: str
    ) -> tuple[RunnerCause | None, Termination]:
        try:
            if (cause := self.startup_stop()) is not None:
                await launch.kill()
                return cause, "interrupted"
            self.connecting = True
            await client.connect()
            self.connecting = False
            info = cast("object", await client.get_server_info())
            if (cause := self.startup_stop()) is not None:
                return cause, "interrupted"
            if (
                not isinstance(info, dict)
                or cast("dict[object, object]", info).get("claude_code_version")
                != CLI_VERSION
            ):
                return "version_mismatch", "none"
            await client.query(prompt)
            return await self.wait(client, launch)
        except CLIJSONDecodeError:
            was_connecting = self.connecting
            self.connecting = False
            if not was_connecting or self.startup_stop() is None:
                self.stop("oversized_message")
            await launch.kill()
            return self.cause or "oversized_message", "killed"
        except ClaudeSDKError:
            self.connecting = False
            cause = self.startup_stop()
            return cause, "interrupted" if cause is not None else "none"

    def ending(self, end: LaunchEnd) -> tuple[RunnerCause, Termination]:
        return (
            self.cause
            or end.cause
            or (
                "cli_exit_without_result"
                if end.exit_code is not None
                else "launcher_died"
            ),
            "interrupted" if self.cause is not None else "none",
        )

    async def wait(
        self, client: ClaudeSDKClient, launch: Launch
    ) -> tuple[RunnerCause | None, Termination]:
        self.reader = asyncio.create_task(self.read(client))
        internal = asyncio.create_task(self.stopped.wait())
        waiting: set[asyncio.Task[object]] = {self.reader, internal}
        try:
            while True:
                done, _ = await asyncio.wait(
                    waiting, return_when=asyncio.FIRST_COMPLETED
                )
                if self.cause is not None:
                    self.start_interrupt(client)
                    waiting.discard(internal)
                if self.reader in done:
                    if self.reader.result():
                        return (
                            (self.cause, "interrupted")
                            if self.cause is not None
                            else ("completed", "teardown_after_result")
                        )
                    return None, "none"
        finally:
            await cancel_tasks((internal,))

    async def release(
        self,
        client: ClaudeSDKClient,
        launch: Launch,
        owned: tuple[asyncio.Task[object], ...],
    ) -> LaunchEnd:
        tasks = tuple(
            task
            for task in (self.escalation, self.interrupt, self.reader)
            if task is not None
        )
        await cancel_tasks((*owned, *tasks))
        try:
            await client.disconnect()
        except ClaudeSDKError:
            await launch.kill()
        finally:
            await cancel_tasks(tuple(self.bridge.active))
        return await launch.ended()

    async def teardown(
        self,
        client: ClaudeSDKClient,
        launch: Launch,
        owned: tuple[asyncio.Task[object], ...],
    ) -> LaunchEnd:
        cleanup = asyncio.create_task(self.release(client, launch, owned))
        timer = asyncio.create_task(
            self.ctx.clock.sleep_until(self.ctx.clock.monotonic_ns() + TEARDOWN_NS)
        )
        killed = asyncio.create_task(self.killed.wait())
        try:
            done, _ = await asyncio.wait(
                {cleanup, timer, killed}, return_when=asyncio.FIRST_COMPLETED
            )
            if cleanup not in done:
                await launch.kill()
            return await cleanup
        finally:
            await cancel_tasks((timer, killed))
