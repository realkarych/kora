import asyncio
import cProfile
import hashlib
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    RepeatedSource,
    preparation_request,
    repeated_digest,
    seal_repeated,
)
from kora.plugins.storage.payload import FileSegment
from kora.plugins.storage.state import LocalState, row_values, text
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import PreparedContentRef, SealedStage
from tests.plugins.storage.configuration import local_configuration
from tests.plugins.storage.inspection import InspectedStorage


class ResolutionStorage(InspectedStorage):
    def resolve_stage(
        self, content: SealedStage | PreparedContentRef
    ) -> tuple[FileSegment, ...]:
        with self._state.transaction(synchronize=False) as connection:
            return self._staging.resolve(connection, content)[1]


@pytest.mark.parametrize("size", [65536, 16777217])
@pytest.mark.parametrize("kind", ["sealed", "alias", "prepared"])
def test_resolution_decodes_one_complete_receipt_view_per_stage(
    tmp_path: Path, kora_capture: Capture, size: int, kind: str
) -> None:
    async def scenario() -> None:
        provider = ResolutionStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        try:
            upload = await seal_repeated(provider, "resolve", size, context)
            content: SealedStage | PreparedContentRef = upload.content
            output_size = size + (kind == "prepared")
            if kind != "sealed":
                request = preparation_request(
                    provider.identity(),
                    "prepare",
                    upload.content,
                    output_size,
                    repeated_digest(output_size),
                )
                content = await provider.prepare_content(
                    request, RepeatedSource(output_size), context
                )

            with cProfile.Profile() as profile:
                segments = provider.resolve_stage(content)
            reader = await provider.read_stage(content, context)
            digest = hashlib.sha256()
            async with reader:
                async for chunk in reader:
                    digest.update(chunk)

            decoded_views = sum(
                entry.callcount
                for entry in profile.getstats()
                if entry.code is LocalState.stage_observations.__code__
            )
            assert decoded_views == 1
            assert sum(segment.size for segment in segments) == output_size
            assert len(segments) == (
                1 if kind == "prepared" else (size - 1) // 16777216 + 1
            )
            assert "sha256:" + digest.hexdigest() == repeated_digest(output_size)
        finally:
            await provider.aclose()
        provider.assert_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("semantic", ["begin", "part-0"])
def test_resolution_revalidates_receipts_in_each_transaction(
    tmp_path: Path, kora_capture: Capture, semantic: str
) -> None:
    async def scenario() -> None:
        provider = ResolutionStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        try:
            upload = await seal_repeated(provider, "fresh", 65536, context)
            provider.resolve_stage(upload.content)
            state = provider.control_state()
            with state.transaction(write=True) as connection:
                row = row_values(
                    connection.execute(
                        "SELECT details FROM receipts WHERE owner_key=? AND semantic=?",
                        (upload.content.stage.stage_id, semantic),
                    ).fetchone()
                )
                assert row is not None
                original = None if row[0] is None else text(row[0])
                connection.execute(
                    "UPDATE receipts SET details='{}' WHERE owner_key=? AND semantic=?",
                    (upload.content.stage.stage_id, semantic),
                )

            try:
                with pytest.raises(KoraProtocolError) as refused:
                    await provider.read_stage(upload.content, context)
            finally:
                with state.transaction(write=True) as connection:
                    connection.execute(
                        "UPDATE receipts SET details=? WHERE owner_key=? "
                        "AND semantic=?",
                        (original, upload.content.stage.stage_id, semantic),
                    )

            assert refused.value.code == "storage_integrity"
            assert not refused.value.retryable
        finally:
            await provider.aclose()
        provider.assert_closed()

    asyncio.run(scenario())
