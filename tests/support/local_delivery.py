import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from kora.conformance.capture import Capture
from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ENVELOPE_BYTES,
    AttachRequest,
    FrameType,
    read_delivery,
    send_message,
    workload_digest,
)
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import (
    COMPONENT,
    ControlServer,
    decoded_workload,
    rebound,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.runtime import Workload
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import IdentityReader, NativeIdentityReader
from kora.runner.peers import kernel_source
from kora.runner.registry import Registry, Running, Submitter
from tests.support.local_peers import FAR_NS, client, identity_of
from tests.support.local_runners import (
    open_spawning,
    short_directory,
    submitted,
    written,
)
from tests.support.local_runtime import runtime_context, state_root


@dataclass(frozen=True)
class DeliveryServer:
    runtime: LocalRuntime
    server: ControlServer
    work: Workload
    entry: Running
    ctx: ProtocolContext

    async def attach(self, dispatcher: str | None = None) -> BoundedChannel:
        clock = self.ctx.clock
        channel = await client(self.entry.socket_path, clock)
        request = AttachRequest(
            op="attach",
            request_id="attach-1",
            dispatcher_id=dispatcher or self.work.dispatcher_id,
        )
        try:
            await channel.write_typed_frame(
                FrameType.JSON,
                canonical_bytes(request.model_dump(mode="json")),
                clock.monotonic_ns() + FAR_NS,
            )
            _, payload = await channel.read_typed_frame(
                clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            read_delivery(payload, "attach-1", "attached")
        except BaseException:
            channel.close()
            raise
        return channel

    async def deliver(
        self, channel: BoundedChannel, work: Workload | None = None
    ) -> None:
        clock = self.ctx.clock
        encoded = canonical_bytes((work or self.work).model_dump(mode="json"))
        await send_message(channel, ACCEPT_WORKLOAD, "delivery-1", (encoded,), clock)
        _, payload = await channel.read_typed_frame(
            clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
        )
        read_delivery(payload, "delivery-1", "accepted")

    async def write(self, entry: Running) -> None:
        await written(self.runtime, self.work.attempt_id, entry, self.ctx)


@asynccontextmanager
async def delivery_server(
    tmp_path: Path,
    capture: Capture,
    *,
    clock: Clock | None = None,
    decoder: Callable[[bytearray], Workload] = decoded_workload,
    identities: IdentityReader | None = None,
) -> AsyncGenerator[DeliveryServer]:
    ctx = runtime_context(capture, clock or SystemClock())
    with short_directory() as base:
        sockets = base / "sockets"
        root = state_root(tmp_path)
        runtime = await open_spawning(root, ctx, sockets)
        registry = Registry(
            open_local_directory(root / "registry"),
            runtime.execution_domain_id,
            lambda: "f" * 32,
        )
        reader = NativeIdentityReader()
        own = identity_of(os.getpid())
        work = submitted(runtime, "attempt-1")
        key = runtime.registry.key(work.attempt_id)
        bound = rebound(sockets, "delivery")
        entry = Running(
            state="running",
            deadline=None,
            gate_closed=False,
            request_digest=work.execution_request_digest,
            workload_digest=workload_digest(work),
            submitter=Submitter(dispatcher_id=work.dispatcher_id, process=own),
            runner=own,
            socket_path=str(sockets / "delivery"),
        )
        await written(runtime, work.attempt_id, entry, ctx)
        server = ControlServer(
            bound,
            sockets,
            "delivery",
            ctx.clock,
            kernel_source(reader),
            capture.diagnostics.logger(COMPONENT),
            registry,
            key,
            own,
            identities or reader,
            decoder=decoder,
        )
        server.start()
        try:
            yield DeliveryServer(runtime, server, work, entry, ctx)
        finally:
            await server.close()
            registry.directory.close()
            await runtime.close()
