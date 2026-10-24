import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import acknowledge_stage, seal_repeated
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import MAX_COUNT
from kora.protocols.storage_models import (
    ContentAddress,
    DeleteArguments,
    DeleteReleaseRequest,
    DeleteRequest,
    MutationReceipt,
    PromotionArguments,
    PromotionRequest,
    StageReleaseRequest,
    StorageLimits,
    StorageMutationSet,
)
from tests.plugins.storage.inspection import InspectedStorage
from tests.plugins.storage.support import operation, stage_spec


def test_local_storage_epoch_exhaustion_preserves_cleanup(
    tmp_path: Path, kora_capture: Capture
) -> None:
    configured = tmp_path / "store"
    configuration = LocalStorageConfiguration(
        root=str(configured), limits=StorageLimits(version=1)
    )
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
            with pytest.raises(KoraProtocolError, match="storage_issuer_closed"):
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
            receipts = tuple(
                sorted(
                    (
                        MutationReceipt(
                            version=1,
                            operation_id=op.operation_id,
                            kind=op.kind,
                            request_digest=op.request_digest,
                            outcome_digest=result.metadata_digest(),
                        )
                        for op, result in ((begin, spec.stage), (retire, retired))
                    ),
                    key=lambda item: item.operation_id,
                )
            )
            acknowledged = StorageMutationSet(
                version=1, stage=spec.stage, receipts=receipts
            )
            released = await terminal.release_stage(
                StageReleaseRequest(
                    version=1,
                    stage=spec.stage,
                    persisted_outcome_revision="terminal-outcome",
                    mutation_set_digest=acknowledged.metadata_digest(),
                    issuance_closed="terminal-closed",
                ),
                context,
            )
            inventory = await terminal.scan("stages", None, 128, context)
        finally:
            await terminal.aclose()

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
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1)
        )
        provider = InspectedStorage(configuration)
        context = context_for(kora_capture)
        upload = await seal_repeated(provider, "released", 1, context)
        arguments = PromotionArguments(
            version=1,
            content=upload.content,
            address=ContentAddress(
                version=1,
                storage_identity=provider.identity(),
                payload_digest=upload.content.payload_digest,
            ),
            content_generation=1,
        )
        promotion = PromotionRequest(
            version=1,
            operation=operation(provider.identity(), "promote", "promote", arguments),
            arguments=arguments,
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
        delete_arguments = DeleteArguments(
            version=1, address=published.content.address, deletion_generation=1
        )
        deletion = DeleteRequest(
            version=1,
            operation=operation(
                provider.identity(), "delete", "delete_content", delete_arguments
            ),
            arguments=delete_arguments,
        )
        await provider.delete_content(deletion, context)
        delete_release = DeleteReleaseRequest(
            version=1,
            operation=deletion.operation,
            persisted_outcome_revision="saved-delete",
            issuance_closed="closed-delete",
        )
        await provider.release_delete(delete_release, context)
        await provider.aclose()
        provider = InspectedStorage(configuration)
        replacement = await seal_repeated(provider, "replacement", 1, context)
        replacement_arguments = arguments.model_copy(
            update={"content": replacement.content, "content_generation": 2}
        )
        recreated = await provider.promote(
            PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "recreate", "promote", replacement_arguments
                ),
                arguments=replacement_arguments,
            ),
            context,
        )
        before = provider.epoch()

        try:
            stage_ack = await provider.release_stage(release, context)
            delete_ack = await provider.release_delete(delete_release, context)
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
