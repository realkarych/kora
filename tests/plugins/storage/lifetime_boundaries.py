import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path

from kora.conformance.storage_support import (
    RepeatedSource,
    acknowledge_stage,
    content_address,
    delete_release,
    delete_request,
    digest,
    operation,
    promotion_request,
    repeated_digest,
    seal_bytes,
    seal_repeated,
    single_part,
    stage_spec,
)
from kora.plugins.storage.content_files import content_path
from kora.plugins.storage.files import RootAnchor
from kora.plugins.storage.inventory import scan
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.plugins.storage.state import LocalState
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    ContentRef,
    DeleteRequest,
    ManifestPart,
    MutationObservation,
    PartRef,
    PartSpec,
    StageManifest,
    StageRef,
    StageSpec,
    StageWrite,
    StorageOperationRef,
    StoragePage,
    StorageScanCursor,
)
from tests.measurements.harness import Report, Sample
from tests.measurements.storage_budgets import read_budgets, require_budget
from tests.measurements.storage_lifetime_resources import close_provider
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.inspection import InspectedStorage


async def assert_refusal(awaitable: Awaitable[object], code: str) -> None:
    try:
        await awaitable
    except KoraProtocolError as error:
        if error.code != code:
            raise
    else:
        raise AssertionError("the required admission refusal was missing")


def seed_inventory(configuration: LocalStorageConfiguration, count: int) -> float:
    started = time.perf_counter()
    with RootAnchor.open(Path(configuration.root), configuration.limits) as root:
        state = LocalState(root)
        records: list[tuple[str, str, int, int]] = []
        for index in range(count):
            payload = index.to_bytes(16, "big")
            address = content_address(root.identity, digest(payload))
            reference = ContentRef(
                version=1,
                address=address,
                object_incarnation=f"fixture-{index}",
                total_size=16,
            )
            path = content_path(address)
            directory = root.directory(path[:-1], create=True)
            try:
                descriptor = root.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    if os.write(descriptor, payload) != len(payload):
                        raise AssertionError(
                            "the offline inventory fixture write was short"
                        )
                    os.fsync(descriptor)
                    info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                root.synchronize_directory(directory)
            finally:
                os.close(directory)
            records.append(
                (
                    address.payload_digest,
                    reference.model_dump_json(),
                    info.st_dev,
                    info.st_ino,
                )
            )
        with state.transaction(write=True) as connection:
            connection.executemany(
                "INSERT INTO contents(digest, reference, device, inode) "
                "VALUES(?, ?, ?, ?)",
                records,
            )
    return time.perf_counter() - started


async def measured_page(
    provider: InspectedStorage,
    cursor: StorageScanCursor | None,
    context: ProtocolContext,
    report: Report,
    count: int,
    phase: str,
) -> StoragePage:
    started = time.perf_counter()
    page = await provider.scan("content", cursor, 128, context)
    elapsed = time.perf_counter() - started
    measured = scan(provider.control_state(), "content", cursor, 128, measure=True)
    if measured.page != page or measured.query_steps is None:
        raise AssertionError("the instrumented actual query changed its projection")
    if measured.query_steps <= 0 or len(page.entries) > measured.queried_rows:
        raise AssertionError("actual inventory query work is absent")
    if measured.queried_rows > 128 or len(page.canonical_bytes()) > 16384:
        raise AssertionError("the indexed inventory exceeded a declared bound")
    require_budget(
        measured.query_steps,
        read_budgets().platforms[sys.platform].inventory_query_steps,
        "inventory.query_steps",
    )
    report.samples.append(
        Sample(
            f"storage.inventory.{phase}",
            count,
            1,
            elapsed,
            {
                "query_steps": measured.query_steps,
                "queried_rows": measured.queried_rows,
                "entries": len(page.entries),
                "metadata_bytes": len(page.canonical_bytes()),
                "keys_json": json.dumps([entry.key for entry in page.entries]),
                "cursor_key": None if cursor is None else cursor.last_key,
                "next_key": None
                if page.next_cursor is None
                else page.next_cursor.last_key,
            },
        )
    )
    return page


def check_inventory_position(
    page: StoragePage, original_keys: list[str], cursor: StorageScanCursor | None
) -> None:
    keys = tuple(entry.key for entry in page.entries)
    remaining = tuple(
        key for key in original_keys if cursor is None or key > cursor.last_key
    )
    if (
        not keys
        or keys != remaining[: len(keys)]
        or (page.next_cursor is None and len(keys) != len(remaining))
        or (page.next_cursor is not None and page.next_cursor.last_key != keys[-1])
    ):
        raise AssertionError("an inventory probe differs from its keyset position")


async def inventory_workload(
    configuration: LocalStorageConfiguration,
    count: int,
    context: ProtocolContext,
    report: Report,
) -> None:
    seeded = seed_inventory(configuration, count)
    report.samples.append(Sample("storage.inventory.seed", count, count, seeded))
    provider = InspectedStorage(configuration)
    cursor: StorageScanCursor | None = None
    emitted: list[str] = []
    try:
        while True:
            page = await measured_page(provider, cursor, context, report, count, "page")
            emitted.extend(entry.key for entry in page.entries)
            if page.next_cursor is None:
                break
            if not page.entries or page.next_cursor.last_key != page.entries[-1].key:
                raise AssertionError(
                    "inventory continuation skipped the emitted prefix"
                )
            cursor = page.next_cursor
        if len(emitted) != count or emitted != sorted(set(emitted)):
            raise AssertionError(
                "inventory omitted or duplicated intentionally retained content"
            )
        first = await measured_page(provider, None, context, report, count, "first")
        check_inventory_position(first, emitted, None)
        middle_cursor = StorageScanCursor(
            version=1,
            storage_identity=provider.identity(),
            kind="content",
            last_key=emitted[count // 2 - 1],
        )
        middle = await measured_page(
            provider, middle_cursor, context, report, count, "middle"
        )
        check_inventory_position(middle, emitted, middle_cursor)
        final_cursor = StorageScanCursor(
            version=1,
            storage_identity=provider.identity(),
            kind="content",
            last_key=emitted[-2],
        )
        final = await measured_page(
            provider, final_cursor, context, report, count, "final"
        )
        check_inventory_position(final, emitted, final_cursor)
        await close_provider(provider)
        provider = InspectedStorage(configuration)
        restarted = await measured_page(
            provider, final_cursor, context, report, count, "restart"
        )
        check_inventory_position(restarted, emitted, final_cursor)
        if restarted.entries != final.entries:
            raise AssertionError("restart changed the same keyset page")
        address = content_address(provider.identity(), emitted[count // 2])
        content = await provider.exists(address, context)
        if content is None:
            raise AssertionError("the offline valid content fixture is missing")
        reader = await provider.get(content, context)
        observed = hashlib.sha256()
        async with reader:
            async for chunk in reader:
                observed.update(chunk)
        if "sha256:" + observed.hexdigest() != address.payload_digest:
            raise AssertionError("the offline inventory fixture was not valid content")
        peer = InspectedStorage(configuration)
        barrier = StorageBarrierActivity("storage.scan.admitted")
        held = asyncio.create_task(
            provider.scan("content", None, 128, replace(context, activity=barrier))
        )
        try:
            await asyncio.wait_for(barrier.entered.wait(), 5)
            new_payload = (count + 1).to_bytes(16, "big")
            created, created_manifest, created_seal = await seal_bytes(
                peer, "concurrent-created", new_payload, context
            )
            publication = promotion_request(
                peer.identity(),
                "concurrent-publish",
                created,
                content_address(peer.identity(), created.payload_digest),
            )
            await peer.promote(publication, context)
            deleted_key = emitted[count // 2 + 1]
            deletion = delete_request(
                peer.identity(),
                "concurrent-delete",
                content_address(peer.identity(), deleted_key),
            )
            removed = await peer.delete_content(deletion, context)
            if not removed.deleted_object:
                raise AssertionError(
                    "concurrent inventory deletion did not remove the fixture"
                )
            concurrent = await measured_page(
                peer, None, context, report, count, "concurrent"
            )
            barrier.released.set()
            original = await held
            if concurrent != original:
                raise AssertionError(
                    "concurrent receivers disagreed on the committed keyset"
                )
            changed_keys = [entry.key for entry in original.entries]
            continuation = original.next_cursor
            while continuation is not None:
                page = await measured_page(
                    peer, continuation, context, report, count, "concurrent_page"
                )
                if page.next_cursor is not None and (
                    not page.entries
                    or page.next_cursor.last_key != page.entries[-1].key
                ):
                    raise AssertionError(
                        "concurrent inventory skipped its emitted prefix"
                    )
                changed_keys.extend(entry.key for entry in page.entries)
                continuation = page.next_cursor
            expected_keys = sorted(
                (set(emitted) - {deleted_key}) | {created.payload_digest}
            )
            if changed_keys != expected_keys:
                raise AssertionError(
                    "concurrent create/delete omitted or duplicated content"
                )
            created_spec = stage_spec(
                peer.identity(), "concurrent-created", new_payload
            )
            issued = (
                operation(
                    peer.identity(),
                    "concurrent-created-begin",
                    "begin_stage",
                    created_spec,
                ),
                operation(
                    peer.identity(),
                    "concurrent-created-part",
                    "write_part",
                    StageWrite(
                        version=1,
                        stage=created.stage,
                        spec=created_manifest.parts[0].spec,
                    ),
                ),
                created_seal,
                publication.operation,
            )
            acknowledgment = await acknowledge_stage(
                peer, created.stage, issued, context
            )
            await peer.release_stage(acknowledgment, context)
            await peer.release_delete(
                delete_release(
                    deletion.operation,
                    persisted_outcome_revision="saved-concurrent-delete",
                    issuance_closed="closed-concurrent-delete",
                ),
                context,
            )
        finally:
            barrier.released.set()
            try:
                await held
            finally:
                await close_provider(peer)
        report.gates[f"storage.inventory.{count}"] = True
    finally:
        await close_provider(provider)


async def join_boundary_job(job: Awaitable[object]) -> None:
    await job


def check_open_issuance(stage_id: str, open_issuance: frozenset[str]) -> None:
    if stage_id not in open_issuance:
        raise AssertionError("the modeled caller cannot reconstruct closed issuance")


async def receiver_churn(
    configuration: LocalStorageConfiguration, context: ProtocolContext, report: Report
) -> None:
    async with AsyncExitStack() as receiver_owner:
        provider = InspectedStorage(configuration)
        receiver_owner.push_async_callback(close_provider, provider)
        spec = StageSpec(
            version=1,
            stage=StageRef(
                version=1, storage_identity=provider.identity(), stage_id="continuing"
            ),
            payload_digest=repeated_digest(268435456),
            total_size=268435456,
            content_type="application/octet-stream",
        )
        begin = operation(provider.identity(), "continuing-begin", "begin_stage", spec)
        await provider.begin_stage(begin, spec, context)
        open_issuance = frozenset({spec.stage.stage_id})
        useful_calls = 1
        issued = [begin]
        parts: list[ManifestPart] = []
        reconstructions = 0
        authority_checks = 0
        refusals = 0
        started = time.perf_counter()
        try:
            for index in range(100):
                async with AsyncExitStack() as peer_owner:
                    peer = InspectedStorage(configuration)
                    peer_owner.push_async_callback(close_provider, peer)
                    dummy = stage_spec(peer.identity(), f"churn-{index}", b"")
                    dummy_begin = operation(
                        peer.identity(), f"churn-{index}-begin", "begin_stage", dummy
                    )
                    await peer.begin_stage(dummy_begin, dummy, context)
                    pending: asyncio.Task[PartRef] | None = None
                    part: PartSpec | None = None
                    write: StorageOperationRef | None = None
                    barrier: StorageBarrierActivity | None = None
                    if index % 6 == 0 and len(parts) < 16:
                        await receiver_owner.aclose()
                        check_open_issuance(spec.stage.stage_id, open_issuance)
                        authority_checks += 1
                        provider = InspectedStorage(configuration)
                        receiver_owner.push_async_callback(close_provider, provider)
                        reconstructions += 1
                        part = PartSpec(
                            version=1,
                            index=len(parts),
                            byte_offset=len(parts) * 16777216,
                            size=16777216,
                            digest=repeated_digest(16777216),
                            final=len(parts) == 15,
                        )
                        write = operation(
                            provider.identity(),
                            f"continuing-part-{len(parts)}",
                            "write_part",
                            StageWrite(version=1, stage=spec.stage, spec=part),
                        )
                        barrier = StorageBarrierActivity(
                            "storage.write_part.registered"
                        )
                        pending = asyncio.create_task(
                            provider.write_part(
                                write,
                                spec.stage,
                                part,
                                RepeatedSource(part.size),
                                replace(context, activity=barrier),
                            )
                        )
                        peer_owner.push_async_callback(join_boundary_job, pending)
                        peer_owner.callback(barrier.released.set)
                        await asyncio.wait_for(barrier.entered.wait(), 5)
                    try:
                        release = await acknowledge_stage(
                            peer, dummy.stage, (dummy_begin,), context
                        )
                        await peer.release_stage(release, context)
                        delayed = stage_spec(peer.identity(), f"delayed-{index}", b"")
                        await assert_refusal(
                            provider.begin_stage(
                                operation(
                                    provider.identity(),
                                    f"delayed-{index}-begin",
                                    "begin_stage",
                                    delayed,
                                ),
                                delayed,
                                context,
                            ),
                            "storage_issuer_closed",
                        )
                        refusals += 1
                        if (
                            barrier is not None
                            and pending is not None
                            and part is not None
                            and write is not None
                        ):
                            barrier.released.set()
                            result = await pending
                            useful_calls += 1
                            if result.spec != part:
                                raise AssertionError(
                                    "registered work changed across the epoch change"
                                )
                            observed = await provider.observe(write, context)
                            if observed.result is None:
                                raise AssertionError(
                                    "the completed continuing part is missing"
                                )
                            if not isinstance(observed.result, PartRef):
                                raise AssertionError(
                                    "the continuing write result changed type"
                                )
                            parts.append(
                                ManifestPart(
                                    version=1,
                                    spec=part,
                                    part_identity=observed.result.part_identity,
                                )
                            )
                            issued.append(write)
                    finally:
                        if barrier is not None:
                            barrier.released.set()
                        await close_provider(peer)
            await receiver_owner.aclose()
            check_open_issuance(spec.stage.stage_id, open_issuance)
            authority_checks += 1
            provider = InspectedStorage(configuration)
            receiver_owner.push_async_callback(close_provider, provider)
            reconstructions += 1
            manifest = StageManifest(
                version=1, stage=spec.stage, parts=tuple(parts), prefix=None
            )
            seal = operation(
                provider.identity(), "continuing-seal", "seal_stage", manifest
            )
            sealed = await provider.seal_stage(seal, spec.stage, manifest, context)
            useful_calls += 1
            issued.append(seal)
            acknowledgment = await acknowledge_stage(
                provider, spec.stage, tuple(issued), context
            )
            await provider.release_stage(acknowledgment, context)
            if sealed.total_size != 268435456 or len(parts) != 16:
                raise AssertionError(
                    "receiver churn prevented complete bounded progress"
                )
            if any(provider.resource_counts().values()):
                raise AssertionError("receiver churn retained disposable state")
            report.samples.append(
                Sample(
                    "storage.receiver_churn",
                    0,
                    100,
                    time.perf_counter() - started,
                    {
                        "parts": len(parts),
                        "refusals": refusals,
                        "reconstructions": reconstructions,
                        "authority_checks": authority_checks,
                        "continuing_useful_calls": useful_calls,
                    },
                )
            )
            report.gates["storage.receiver_churn"] = True
        finally:
            await receiver_owner.aclose()


async def maximum_slots(
    configuration: LocalStorageConfiguration, context: ProtocolContext, report: Report
) -> None:
    async with AsyncExitStack() as ownership:
        provider = InspectedStorage(configuration)
        ownership.push_async_callback(close_provider, provider)
        peer = InspectedStorage(configuration)
        ownership.push_async_callback(close_provider, peer)
        uploads: list[tuple[StageRef, tuple[StorageOperationRef, ...]]] = []
        first = await seal_repeated(provider, "slot-0", 0, context)
        uploads.append((first.content.stage, first.operations))
        for index in range(1, 32):
            spec = stage_spec(provider.identity(), f"slot-{index}", b"")
            begin = operation(
                provider.identity(), f"slot-{index}-begin", "begin_stage", spec
            )
            await provider.begin_stage(begin, spec, context)
            uploads.append((spec.stage, (begin,)))
        overflow = stage_spec(provider.identity(), "slot-overflow", b"")
        await assert_refusal(
            provider.begin_stage(
                operation(
                    provider.identity(), "slot-overflow-begin", "begin_stage", overflow
                ),
                overflow,
                context,
            ),
            "storage_capacity",
        )
        deletions: list[DeleteRequest] = []
        for index in range(33):
            request = delete_request(
                provider.identity(),
                f"delete-{index}",
                content_address(provider.identity(), f"sha256:{index:064x}"),
            )
            if index == 32:
                await assert_refusal(
                    provider.delete_content(request, context), "storage_capacity"
                )
            else:
                deleted = await provider.delete_content(request, context)
                if deleted.deleted_object:
                    raise AssertionError("an absent object was attributed as deleted")
                deletions.append(request)
        readers = [
            await owner.read_stage(first.content, context)
            for owner in (provider, peer)
            for _ in range(8)
        ]
        await assert_refusal(
            provider.read_stage(first.content, context), "storage_busy"
        )
        barriers: list[StorageBarrierActivity] = []
        writers: list[asyncio.Task[PartRef]] = []
        controls: list[asyncio.Task[MutationObservation]] = []
        control_barriers: list[StorageBarrierActivity] = []
        started = time.perf_counter()
        try:
            for index in range(1, 9):
                owner = provider if index <= 4 else peer
                stage, issued = uploads[index]
                part = single_part(b"")
                write = operation(
                    owner.identity(),
                    f"slot-{index}-part",
                    "write_part",
                    StageWrite(version=1, stage=stage, spec=part),
                )
                barrier = StorageBarrierActivity("storage.write_part.registered")
                task = asyncio.create_task(
                    owner.write_part(
                        write,
                        stage,
                        part,
                        RepeatedSource(0),
                        replace(context, activity=barrier),
                    )
                )
                barriers.append(barrier)
                writers.append(task)
                await asyncio.wait_for(barrier.entered.wait(), 5)
                uploads[index] = stage, (*issued, write)
            await assert_refusal(
                provider.exists(
                    content_address(provider.identity(), digest(b"")), context
                ),
                "storage_busy",
            )
            for owner in (provider, peer):
                barrier = StorageBarrierActivity("storage.observe.admitted")
                task = asyncio.create_task(
                    owner.observe(
                        first.operations[0], replace(context, activity=barrier)
                    )
                )
                controls.append(task)
                control_barriers.append(barrier)
                await asyncio.wait_for(barrier.entered.wait(), 5)
            await assert_refusal(
                provider.scan("stages", None, 128, context), "storage_busy"
            )
            left = provider.resource_counts()
            right = peer.resource_counts()
            if (
                left["owned_jobs"] + right["owned_jobs"] != 10
                or left["readers"] + right["readers"] != 16
                or left["stage_rows"] != 32
                or left["delete_rows"] != 32
            ):
                raise AssertionError(
                    "maximum aggregate data/control/reader ownership was not exercised"
                )
            control_barriers[0].released.set()
            await controls[0]
            retire = operation(
                provider.identity(),
                "slot-0-retire",
                "retire_stage",
                first.content.stage,
            )
            waiting = await provider.retire_stage(retire, first.content.stage, context)
            if waiting.completed or waiting.remaining_effects != 1:
                raise AssertionError(
                    "retirement failed to preserve actual reader ownership"
                )
            control_barriers[1].released.set()
            await controls[1]
            for reader in readers:
                await reader.aclose()
            for barrier, task in zip(barriers, writers, strict=True):
                barrier.released.set()
                await task
            await close_provider(peer)
            for stage, issued in uploads:
                acknowledgment = await acknowledge_stage(
                    provider, stage, issued, context
                )
                await provider.release_stage(acknowledgment, context)
            for request in deletions:
                await provider.release_delete(
                    delete_release(
                        request.operation,
                        persisted_outcome_revision="saved-delete",
                        issuance_closed="closed-delete",
                    ),
                    context,
                )
            if any(provider.resource_counts().values()):
                raise AssertionError("maximum-slot cleanup retained disposable state")
            report.samples.append(
                Sample(
                    "storage.maximum_slots",
                    0,
                    1,
                    time.perf_counter() - started,
                    {
                        "stages": 32,
                        "deletions": 32,
                        "data_jobs": 8,
                        "control_jobs": 2,
                        "readers": left["readers"] + right["readers"],
                        "composition_owners": 2,
                        "peak_owned_jobs": left["owned_jobs"] + right["owned_jobs"],
                    },
                )
            )
            report.gates["storage.maximum_slots"] = True
        finally:
            for barrier in (*barriers, *control_barriers):
                barrier.released.set()
            try:
                owned: tuple[Awaitable[PartRef | MutationObservation], ...] = (
                    *writers,
                    *controls,
                )
                results: list[
                    PartRef | MutationObservation | BaseException
                ] = await asyncio.gather(*owned, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                for reader in readers:
                    await reader.aclose()
            finally:
                try:
                    await close_provider(peer)
                finally:
                    await close_provider(provider)


async def bounded_reconstruction(
    configuration: LocalStorageConfiguration, context: ProtocolContext, report: Report
) -> None:
    started = time.perf_counter()
    failures = 0
    authority_checks = 0
    reconstructions = 0
    open_issuance = frozenset({"bounded"})
    saved_spec: StageSpec | None = None
    saved_operation: StorageOperationRef | None = None
    for index in range(3):
        check_open_issuance("bounded", open_issuance)
        authority_checks += 1
        async with AsyncExitStack() as ownership:
            provider = InspectedStorage(configuration)
            ownership.push_async_callback(close_provider, provider)
            reconstructions += 1
            async with context.activity.span(
                "storage.boundary.reconstruction.peer_acquisition"
            ):
                peer = InspectedStorage(configuration)
                ownership.push_async_callback(close_provider, peer)
            spec = stage_spec(provider.identity(), "bounded", b"")
            begin = operation(provider.identity(), "bounded-begin", "begin_stage", spec)
            saved_spec, saved_operation = spec, begin
            barrier = StorageBarrierActivity("storage.begin_stage.admitted")
            pending = asyncio.create_task(
                assert_refusal(
                    provider.begin_stage(
                        begin, spec, replace(context, activity=barrier)
                    ),
                    "storage_issuer_closed",
                )
            )
            ownership.push_async_callback(join_boundary_job, pending)
            ownership.callback(pending.cancel)
            ownership.callback(barrier.released.set)
            await asyncio.wait_for(barrier.entered.wait(), 5)
            dummy = stage_spec(peer.identity(), f"advance-{index}", b"")
            dummy_begin = operation(
                peer.identity(), f"advance-{index}-begin", "begin_stage", dummy
            )
            await peer.begin_stage(dummy_begin, dummy, context)
            release = await acknowledge_stage(
                peer, dummy.stage, (dummy_begin,), context
            )
            await peer.release_stage(release, context)
            barrier.released.set()
            await pending
            failures += 1
    if failures != 3 or saved_spec is None or saved_operation is None:
        raise AssertionError(
            "the request did not return bounded pending after three changes"
        )
    check_open_issuance("bounded", open_issuance)
    next_authority_checks = 1
    current = InspectedStorage(configuration)
    next_reconstructions = 1
    try:
        stage = await current.begin_stage(saved_operation, saved_spec, context)
        if stage != saved_spec.stage:
            raise AssertionError("the next authorized request returned another stage")
        release = await acknowledge_stage(current, stage, (saved_operation,), context)
        await current.release_stage(release, context)
        if any(current.resource_counts().values()):
            raise AssertionError(
                "the next authorized request retained old receivers or state"
            )
    finally:
        await close_provider(current)
    report.samples.append(
        Sample(
            "storage.bounded_reconstruction",
            0,
            1,
            time.perf_counter() - started,
            {
                "request_refusals": failures,
                "request_reconstructions": reconstructions,
                "request_authority_checks": authority_checks,
                "next_request_reconstructions": next_reconstructions,
                "next_request_authority_checks": next_authority_checks,
                "next_request_completed": stage == saved_spec.stage,
            },
        )
    )
    report.gates["storage.bounded_reconstruction"] = True
