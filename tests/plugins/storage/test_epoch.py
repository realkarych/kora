import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    acknowledge_stage,
    content_address,
    delete_release,
    delete_request,
    operation,
    promotion_request,
    seal_repeated,
    stage_release,
    stage_spec,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import MAX_COUNT
from tests.plugins.storage.configuration import local_configuration
from tests.plugins.storage.inspection import InspectedStorage


def test_local_storage_epoch_exhaustion_preserves_cleanup(
    tmp_path: Path, kora_capture: Capture
) -> None:
    configured = tmp_path / "store"
    configuration = local_configuration(configured)
    provider = InspectedStorage(configuration)
    context = context_for(kora_capture)
    spec = stage_spec(provider.identity(), "tracked", b"")
    begin = operation(provider.identity(), "begin", "begin_stage", spec)

    async def arrange() -> None:
        await provider.begin_stage(begin, spec, context)
        await provider.aclose()

    asyncio.run(arrange())
    with closing(sqlite3.connect(configured / "control.sqlite")) as connection:
        with connection:
            connection.execute(
                "UPDATE root SET epoch=? WHERE singleton=1", (MAX_COUNT,)
            )

    async def cleanup() -> None:
        terminal = InspectedStorage(configuration)
        absent = stage_spec(terminal.identity(), "absent", b"")
        absent_retire = operation(
            terminal.identity(), "absent-retire", "retire_stage", absent.stage
        )
        try:
            with pytest.raises(KoraProtocolError) as exhausted:
                await terminal.begin_stage(
                    operation(
                        terminal.identity(), "absent-begin", "begin_stage", absent
                    ),
                    absent,
                    context,
                )
            closed = await terminal.retire_stage(absent_retire, absent.stage, context)
            unavailable = await terminal.observe(absent_retire, context)
            retire = operation(
                terminal.identity(), "retire", "retire_stage", spec.stage
            )
            retired = await terminal.retire_stage(retire, spec.stage, context)
            released = await terminal.release_stage(
                stage_release(
                    spec.stage,
                    ((begin, spec.stage), (retire, retired)),
                    persisted_outcome_revision="terminal-outcome",
                    issuance_closed="terminal-closed",
                ),
                context,
            )
            inventory = await terminal.scan("stages", None, 128, context)
        finally:
            await terminal.aclose()

        assert exhausted.value.code == "storage_issuer_closed"
        assert closed.completed
        assert unavailable.state == "evidence_unavailable"
        assert released.state == "released"
        assert not inventory.entries

    asyncio.run(cleanup())

    with closing(sqlite3.connect(configured / "control.sqlite")) as connection:
        assert connection.execute("SELECT epoch FROM root").fetchone() == (MAX_COUNT,)


def test_reconstructed_receiver_acknowledges_compacted_ownership(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configuration = local_configuration(tmp_path / "store")
        provider = InspectedStorage(configuration)
        context = context_for(kora_capture)
        upload = await seal_repeated(provider, "released", 1, context)
        promotion = promotion_request(
            provider.identity(),
            "promote",
            upload.content,
            content_address(provider.identity(), upload.content.payload_digest),
        )
        published = await provider.promote(promotion, context)
        release = await acknowledge_stage(
            provider,
            upload.content.stage,
            (*upload.operations, promotion.operation),
            context,
        )
        await provider.release_stage(release, context)
        await provider.aclose()
        provider = InspectedStorage(configuration)
        deletion = delete_request(
            provider.identity(), "delete", published.content.address
        )
        await provider.delete_content(deletion, context)
        deletion_release = delete_release(
            deletion.operation,
            persisted_outcome_revision="saved-delete",
            issuance_closed="closed-delete",
        )
        await provider.release_delete(deletion_release, context)
        await provider.aclose()
        provider = InspectedStorage(configuration)
        replacement = await seal_repeated(provider, "replacement", 1, context)
        recreated = await provider.promote(
            promotion_request(
                provider.identity(),
                "recreate",
                replacement.content,
                promotion.arguments.address,
                content_generation=2,
            ),
            context,
        )
        before = provider.epoch()

        try:
            stage_ack = await provider.release_stage(release, context)
            delete_ack = await provider.release_delete(deletion_release, context)
            observation = await provider.observe(deletion.operation, context)
            existing = await provider.exists(recreated.content.address, context)
            after = provider.epoch()
        finally:
            await provider.aclose()

        assert stage_ack.state == delete_ack.state == "released"
        assert before == after
        assert observation.state == "evidence_unavailable"
        assert existing == recreated.content
        assert (
            recreated.content.object_incarnation != published.content.object_incarnation
        )

    asyncio.run(scenario())
