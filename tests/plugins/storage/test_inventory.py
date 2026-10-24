import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits
from tests.plugins.storage.support import operation, stage_spec


def test_local_storage_inventory_page_fits_metadata_envelope(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"),
                limits=StorageLimits(version=1, stage_slots=32),
            )
        )
        context = context_for(kora_capture)
        keys = tuple(f"{index:03d}-" + "x" * 252 for index in range(32))
        for index, key in enumerate(keys):
            spec = stage_spec(provider.identity(), key, b"")
            await provider.begin_stage(
                operation(provider.identity(), f"begin-{index}", "begin_stage", spec),
                spec,
                context,
            )

        try:
            first = await provider.scan("stages", None, 7, context)
            second = await provider.scan("stages", first.next_cursor, 7, context)
            all_entries = await provider.scan("stages", None, 128, context)
            assert first.next_cursor is not None
            wrong = first.next_cursor.model_copy(update={"kind": "content"})
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.scan("stages", wrong, 7, context)
            with pytest.raises(KoraProtocolError, match="storage_range"):
                await provider.scan("stages", None, True, context)
        finally:
            await provider.aclose()

        assert tuple(entry.key for entry in first.entries) == keys[:7]
        assert tuple(entry.key for entry in second.entries) == keys[7:14]
        assert tuple(entry.key for entry in all_entries.entries) == keys
        assert first.next_cursor.last_key == keys[6]
        assert len(all_entries.canonical_bytes()) <= 16384

    asyncio.run(scenario())


def test_inventory_reports_actual_bounded_query_work(tmp_path: Path) -> None:
    from kora.conformance.capture import captured
    from kora.observability import Level, LoggingSettings
    from kora.plugins.storage.local import COMPONENT
    from tests.plugins.storage.clock import StorageClock

    clock = StorageClock()
    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "query-work", clock.now
    ) as capture:

        async def scenario() -> None:
            provider = LocalArtifactStorage(
                LocalStorageConfiguration(
                    root=str(tmp_path / "store"), limits=StorageLimits(version=1)
                )
            )
            context = context_for(capture)
            spec = stage_spec(provider.identity(), "private-key-marker", b"")
            await provider.begin_stage(
                operation(provider.identity(), "begin", "begin_stage", spec),
                spec,
                context,
            )

            try:
                page = await provider.scan("stages", None, 128, context)
            finally:
                await provider.aclose()

            assert len(page.entries) == 1

        asyncio.run(scenario())
        records = capture.finish()

    work = [
        record for record in records if record["event"] == "storage.local.scan_work"
    ]
    assert len(work) == 1
    assert work[0]["queried_rows"] == 1
    assert isinstance(work[0]["query_steps"], int)
    assert work[0]["query_steps"] > 0
    assert "private-key-marker" not in str(work)
