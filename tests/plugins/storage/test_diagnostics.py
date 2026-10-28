import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import captured
from kora.conformance.common import context_for, utc_now
from kora.conformance.storage_support import (
    BytesSource,
    operation,
    single_part,
    stage_spec,
)
from kora.observability import Level, LoggingSettings
from kora.plugins.storage.local import (
    COMPONENT,
    LocalArtifactStorage,
)
from kora.protocols.storage_models import StageWrite
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.configuration import local_configuration


class FailingSource(BytesSource):
    async def read(self, maximum: int) -> bytes:
        raise RuntimeError("private-payload-and-backend-path")


@pytest.mark.parametrize("level", [Level.INFO, Level.DEBUG])
def test_storage_unexpected_failure_has_one_safe_reporting_owner(
    tmp_path: Path, level: Level
) -> None:
    with captured(
        COMPONENT, LoggingSettings(level=level), "storage-error", utc_now
    ) as capture:
        context = context_for(capture)
        context = replace(
            context,
            logger=capture.diagnostics.logger(COMPONENT).bind(
                request_id="safe-request"
            ),
        )
        provider = LocalArtifactStorage(
            local_configuration(tmp_path / "private-root-marker")
        )
        source = FailingSource(b"k")

        async def scenario() -> None:
            spec = stage_spec(provider.identity(), "private-stage-marker", b"k")
            stage = await provider.begin_stage(
                operation(provider.identity(), "begin", "begin_stage", spec),
                spec,
                context,
            )
            part = single_part(b"k")
            request = operation(
                provider.identity(),
                "write",
                "write_part",
                StageWrite(version=1, stage=stage, spec=part),
            )
            try:
                with pytest.raises(
                    RuntimeError, match="private-payload-and-backend-path"
                ):
                    await provider.write_part(request, stage, part, source, context)
            finally:
                await provider.aclose()

        asyncio.run(scenario())
        records = capture.finish()

    errors = [record for record in records if record["level"] == "ERROR"]
    assert len(errors) == 1
    assert all(record["event"] == "storage.local.failed" for record in errors)
    assert all(record["request_id"] == "safe-request" for record in errors)
    assert source.closed
    encoded = json.dumps(records)
    assert "private-payload-and-backend-path" not in encoded
    assert "private-root-marker" not in encoded
    assert "private-stage-marker" not in encoded
    assert "sha256:" not in encoded


def test_storage_concurrent_contexts_do_not_cross_requests(tmp_path: Path) -> None:
    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "storage-context", utc_now
    ) as capture:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(capture)
        left = replace(
            context,
            logger=capture.diagnostics.logger(COMPONENT).bind(
                request_id="left-request"
            ),
        )
        right = replace(
            context,
            logger=capture.diagnostics.logger(COMPONENT).bind(
                request_id="right-request"
            ),
        )
        barrier = StorageBarrierActivity("storage.observe.admitted")

        async def scenario() -> None:
            spec = stage_spec(provider.identity(), "stage", b"")
            begin = operation(provider.identity(), "begin", "begin_stage", spec)
            await provider.begin_stage(begin, spec, left)
            pending = asyncio.create_task(
                provider.observe(begin, replace(left, activity=barrier))
            )
            try:
                await asyncio.wait_for(barrier.entered.wait(), 5)
                await provider.scan("stages", None, 128, right)
                barrier.released.set()
                await pending
            finally:
                barrier.released.set()
                await provider.aclose()

        asyncio.run(scenario())
        records = capture.finish()

    completed = [
        record for record in records if record["event"] == "storage.local.completed"
    ]
    assert {record["operation"]: record["request_id"] for record in completed} == {
        "begin_stage": "left-request",
        "observe": "left-request",
        "scan": "right-request",
    }
