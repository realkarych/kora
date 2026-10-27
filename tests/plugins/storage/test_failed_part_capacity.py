import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import BytesSource, digest, operation, stage_spec
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    ContentPreparation,
    ManifestPrefix,
    PartSpec,
    PrefixCapture,
    PreparationArguments,
    StageManifest,
    StageWrite,
    StorageLimits,
)
from tests.plugins.storage.inspection import InspectedStorage


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
        spec = stage_spec(provider.identity(), "failed-part", payload)
        part = PartSpec(
            version=1,
            index=0,
            byte_offset=0,
            size=len(payload),
            digest=digest(payload),
            final=True,
        )
        try:
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

        assert failed_write.value.code == "storage_conflict"
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
        spec = stage_spec(provider.identity(), "failed-part", payload)
        part = PartSpec(
            version=1,
            index=0,
            byte_offset=0,
            size=len(payload),
            digest=digest(payload),
            final=True,
        )
        try:
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
            capture = PrefixCapture(
                version=1, stage=spec.stage, source_spec=part, count=1
            )
            prefix = await provider.capture_prefix(
                operation(provider.identity(), "prefix", "capture_prefix", capture),
                spec.stage,
                part,
                1,
                BytesSource(payload),
                context,
            )
            manifest = StageManifest(
                version=1,
                stage=spec.stage,
                parts=(),
                prefix=ManifestPrefix(
                    version=1,
                    source_spec=part,
                    count=1,
                    digest=prefix.digest,
                    private_identity=prefix.private_identity,
                ),
            )
            sealed = await provider.seal_stage(
                operation(provider.identity(), "seal", "seal_stage", manifest),
                spec.stage,
                manifest,
                context,
            )
            arguments = PreparationArguments(
                version=1,
                source=sealed,
                maximum_output_size=1,
                expected_output_digest=digest(b"p"),
            )

            prepared = await provider.prepare_content(
                ContentPreparation(
                    version=1,
                    operation=operation(
                        provider.identity(), "prepare", "prepare_content", arguments
                    ),
                    arguments=arguments,
                ),
                BytesSource(b"p"),
                context,
            )
            counts = provider.resource_counts()
            held = sum(
                path.stat().st_size
                for path in (tmp_path / "store" / "stages" / "0").iterdir()
            )
        finally:
            await provider.aclose()

        assert failed_write.value.code == "storage_conflict"
        assert prepared.total_size == 1
        assert held == 524290
        assert counts["reserved_bytes"] == 1048578
        provider.assert_closed()

    asyncio.run(scenario())
