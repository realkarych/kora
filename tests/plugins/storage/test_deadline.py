import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import acknowledge_stage, seal_repeated
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.storage_models import StorageLimits
from tests.plugins.storage.clock import StorageClock
from tests.plugins.storage.support import operation, stage_spec


def test_expired_storage_call_preserves_replay_and_cleanup(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
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
            with pytest.raises(KoraProtocolError, match="storage_deadline"):
                await provider.begin_stage(begin, spec, expired)
            with pytest.raises(KoraProtocolError, match="storage_deadline"):
                await provider.read_stage(upload.content, expired)
            saved = await provider.observe(upload.operations[0], expired)
            release = await acknowledge_stage(
                provider, upload.content.stage, upload.operations, expired
            )
            released = await provider.release_stage(release, expired)
            inventory = await provider.scan("stages", None, 128, expired)
        finally:
            await provider.aclose()

        assert saved.state == "settled"
        assert released.state == "released"
        assert inventory.entries == ()

    asyncio.run(scenario())
