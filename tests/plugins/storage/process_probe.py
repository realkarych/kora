import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from multiprocessing.connection import Connection

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.storage_support import digest, operation, seal_bytes
from kora.observability import LoggingSettings
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.storage_models import (
    ContentAddress,
    PromotionArguments,
    PromotionRequest,
    StorageLimits,
)


class ProcessBarrierActivity:
    def __init__(self, connection: Connection, boundary: str, request: bytes) -> None:
        self.connection = connection
        self.boundary = boundary
        self.request = request

    def pause(self) -> None:
        self.connection.send_bytes(self.request)
        if not self.connection.poll(15):
            raise TimeoutError("the process barrier was not released")
        if self.connection.recv_bytes(16) != b"continue":
            raise ValueError("invalid process barrier release")

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        return self.record(name)

    @asynccontextmanager
    async def record(self, name: str) -> AsyncGenerator[None]:
        if name == self.boundary and name != "storage.promote.admitted":
            self.pause()
        yield
        if name == self.boundary and name == "storage.promote.admitted":
            self.pause()


def promotion_probe(root: str, boundary: str, connection: Connection) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=root, limits=StorageLimits(version=1))
        )
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "storage-crash-probe", utc_now
        ) as capture:
            context = context_for(capture)
            sealed, _, _ = await seal_bytes(
                provider, "crashed", b"crash-content", context
            )
            address = ContentAddress(
                version=1,
                storage_identity=provider.identity(),
                payload_digest=digest(b"crash-content"),
            )
            arguments = PromotionArguments(
                version=1, content=sealed, address=address, content_generation=1
            )
            request = PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "crashed-promote", "promote", arguments
                ),
                arguments=arguments,
            )
            activity = ProcessBarrierActivity(
                connection, boundary, request.canonical_bytes()
            )
            try:
                await provider.promote(request, replace(context, activity=activity))
            finally:
                await provider.aclose()

    try:
        asyncio.run(scenario())
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise ValueError("the probe requires a root, boundary and control descriptor")
    promotion_probe(sys.argv[1], sys.argv[2], Connection(int(sys.argv[3])))
