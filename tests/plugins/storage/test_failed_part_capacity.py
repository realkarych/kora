import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    BytesSource,
    digest,
    operation,
    preparation_request,
    seal_prefix,
    single_part,
    stage_spec,
)
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    PartSpec,
    PrefixCapture,
    StageSpec,
    StageWrite,
    StorageLimits,
)
from tests.plugins.storage.inspection import InspectedStorage


async def write_failed_part(
    provider: InspectedStorage, payload: bytes, context: ProtocolContext
) -> tuple[StageSpec, PartSpec, KoraProtocolError]:
    spec = stage_spec(provider.identity(), "failed-part", payload)
    part = single_part(payload)
    await provider.begin_stage(
        operation(provider.identity(), "begin", "begin_stage", spec),
        spec,
        context,
    )
    with pytest.raises(KoraProtocolError) as failed_write:
        await provider.write_part(
            operation(
                provider.identity(),
                "failed",
                "write_part",
                StageWrite(version=1, stage=spec.stage, spec=part),
            ),
            spec.stage,
            part,
            BytesSource(payload + b"!"),
            context,
        )
    return spec, part, failed_write.value


def test_prefix_admission_charges_the_failed_part(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = InspectedStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"),
                limits=StorageLimits(version=1, private_bytes=1048576),
            )
        )
        context = context_for(kora_capture)
        payload = b"k" * 524288
        try:
            spec, part, failed_write = await write_failed_part(
                provider, payload, context
            )
            capture = PrefixCapture(
                version=1, stage=spec.stage, source_spec=part, count=1
            )

            with pytest.raises(KoraProtocolError) as refused_prefix:
                await provider.capture_prefix(
                    operation(provider.identity(), "prefix", "capture_prefix", capture),
                    spec.stage,
                    part,
                    1,
                    BytesSource(payload),
                    context,
                )
            reserved = provider.resource_counts()["reserved_bytes"]
        finally:
            await provider.aclose()

        assert failed_write.code == "storage_conflict"
        assert refused_prefix.value.code == "storage_capacity"
        assert reserved == 1048576
        provider.assert_closed()

    asyncio.run(scenario())


def test_preparation_preserves_the_retained_failed_part_reservation(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = InspectedStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"),
                limits=StorageLimits(version=1, private_bytes=2097152),
            )
        )
        context = context_for(kora_capture)
        payload = b"k" * 524288
        try:
            spec, part, failed_write = await write_failed_part(
                provider, payload, context
            )
            sealed = await seal_prefix(
                provider,
                spec.stage,
                part,
                1,
                BytesSource(payload),
                context,
                capture_id="prefix",
                seal_id="seal",
                manifest_count="requested",
            )
            request = preparation_request(
                provider.identity(), "prepare", sealed, 1, digest(b"p")
            )

            prepared = await provider.prepare_content(
                request, BytesSource(b"p"), context
            )
            counts = provider.resource_counts()
            held = sum(
                path.stat().st_size
                for path in (tmp_path / "store" / "stages" / "0").iterdir()
            )
        finally:
            await provider.aclose()

        assert failed_write.code == "storage_conflict"
        assert prepared.total_size == 1
        assert held == 524290
        assert counts["reserved_bytes"] == 1048578
        provider.assert_closed()

    asyncio.run(scenario())
