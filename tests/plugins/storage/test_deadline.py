import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    acknowledge_stage,
    operation,
    seal_repeated,
    stage_spec,
)
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from tests.plugins.storage.clock import StorageClock
from tests.plugins.storage.configuration import local_configuration


def test_expired_storage_call_preserves_replay_and_cleanup(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = replace(context_for(kora_capture), clock=StorageClock())
        expired = replace(
            context,
            deadline=ExecutionDeadline(
                at=datetime(2000, 1, 1, tzinfo=UTC),
                scope_kind="run",
                scope_id="expired-run",
                clock_uncertainty=timedelta(0),
                grace=DEADLINE_GRACE,
            ),
        )
        upload = await seal_repeated(provider, "saved", 1, context)
        spec = stage_spec(provider.identity(), "never-admitted", b"")
        begin = operation(provider.identity(), "expired", "begin_stage", spec)

        try:
            with pytest.raises(KoraProtocolError) as expired_begin:
                await provider.begin_stage(begin, spec, expired)
            with pytest.raises(KoraProtocolError) as expired_read:
                await provider.read_stage(upload.content, expired)
            saved = await provider.observe(upload.operations[0], expired)
            release = await acknowledge_stage(
                provider, upload.content.stage, upload.operations, expired
            )
            released = await provider.release_stage(release, expired)
            inventory = await provider.scan("stages", None, 128, expired)
        finally:
            await provider.aclose()

        assert expired_begin.value.code == "storage_deadline"
        assert expired_read.value.code == "storage_deadline"
        assert saved.state == "settled"
        assert released.state == "released"
        assert inventory.entries == ()

    asyncio.run(scenario())
