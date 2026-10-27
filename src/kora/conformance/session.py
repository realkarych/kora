import asyncio
import socket
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from functools import partial

import pytest

from kora.conformance.bridge import ConformanceSelection, PreparedInvocation
from kora.conformance.common import fail
from kora.conformance.executor import Case, Executor
from kora.conformance.result import ChildResult
from kora.conformance.storage_cases import StorageCaseRunner
from kora.conformance.transport import LifetimeClient
from kora.protocols.metadata import ProtocolName
from kora.protocols.storage import ArtifactStorage
from kora.runner.filesystem import DirectoryAnchor


class Session:
    def __init__(
        self,
        protocol: ProtocolName,
        selection: ConformanceSelection,
        executor: Executor,
        lifetime_endpoint: socket.socket,
        root: DirectoryAnchor,
        startup: DirectoryAnchor,
        report: Callable[[ChildResult], Coroutine[object, object, None]],
    ) -> None:
        self.protocol = protocol
        self.selection = selection
        self.executor = executor
        self.lifetime_endpoint = lifetime_endpoint
        self.root = root
        self.startup = startup
        self.report = report
        self.invocation: PreparedInvocation | None = None
        self.storage_factory = self.reconstruct
        self._checked_fixtures = False
        self._started = False
        self.closed = False

    def provider(self) -> object:
        if self.invocation is None:
            fail("conformance provider is not prepared")
        return self.invocation.instance

    def reconstruct(self) -> ArtifactStorage:
        if self.invocation is None:
            fail("conformance provider is not prepared")
        return self.invocation.reconstruct()

    async def exercise(
        self, invocation: PreparedInvocation, case: Case, request: pytest.FixtureRequest
    ) -> None:
        self.invocation = invocation
        try:
            if not self._checked_fixtures:
                if request.getfixturevalue("kora_provider") is not invocation.instance:
                    fail("conformance provider fixture was replaced")
                storage = self.protocol is ProtocolName.ARTIFACT_STORAGE
                name = (
                    "kora_artifact_storage_factory"
                    if storage
                    else f"kora_{self.protocol}_provider"
                )
                expected = self.storage_factory if storage else invocation.instance
                if request.getfixturevalue(name) is not expected:
                    fail("conformance selected fixture was replaced")
                self._checked_fixtures = True
            await self.executor.run(case, invocation)
        finally:
            self.invocation = None

    async def connected(self) -> LifetimeClient:
        return LifetimeClient(self.lifetime_endpoint)

    async def run_shared(
        self, adapter: object, client: LifetimeClient, request: pytest.FixtureRequest
    ) -> None:
        async with self.selection.prepare(adapter, client) as invocation:
            for case in self.executor.cases:
                await self.exercise(invocation, case, request)

    async def parked(self) -> None:
        while True:
            try:
                await asyncio.get_running_loop().create_future()
            except asyncio.CancelledError:
                continue

    def run(self, adapter: object, request: pytest.FixtureRequest) -> None:
        if self._started:
            fail("conformance wrapper was repeated")
        self._started = True
        with asyncio.Runner() as runner:
            client = runner.run(self.connected())
            try:
                if self.protocol is ProtocolName.ARTIFACT_STORAGE:
                    cases = StorageCaseRunner(
                        runner, self.selection, adapter, client, self.root, self.startup
                    )
                    settled = False
                    try:
                        for case in self.executor.cases:
                            cases.run(
                                partial(self.exercise, case=case, request=request)
                            )
                        settled = True
                    finally:
                        if not settled:
                            try:
                                runner.run(
                                    self.report(
                                        ChildResult(
                                            phase="storage",
                                            expected=len(self.executor.cases),
                                            completed=self.executor.completed,
                                        )
                                    )
                                )
                            finally:
                                runner.run(self.parked())
                else:
                    runner.run(self.run_shared(adapter, client, request))
            finally:
                client.disconnect()
        self.startup.close()
        self.root.close()
        self.closed = True


CURRENT_SESSION: ContextVar[Session] = ContextVar("kora_conformance_session")
