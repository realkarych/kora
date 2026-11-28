import asyncio
import os
import secrets
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from kora.db import CUSTODY_UNFINISHED, custody_log
from kora.db.completion import complete
from kora.db.connection import (
    Database,
    DatabaseSettings,
    IsolatedMetadataMain,
    LocalSQLiteWriterEntry,
)
from kora.db.custody_journal import (
    CustodyJournalLease,
    NativeRow,
    complete_native_journal_job,
    open_isolated_journal_lease,
)
from kora.db.custody_models import (
    CustodyFileAnchor,
    CustodyNativeOutcome,
    CustodyParticipantRegistration,
    CustodyProcessIdentity,
    CustodyUnavailable,
)
from kora.db.custody_native import (
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    custody_native_job,
    file_anchor,
    register_custody_fork,
)
from kora.db.custody_schema import (
    ISOLATED_AUTHORITY_VERSION,
    ISOLATED_FRONTIER_REVISION,
)
from kora.db.custody_sqlite import CustodySQLiteConnection
from kora.db.custody_writer import (
    LOCAL_SQLITE_NATIVE_SEQUENCE,
    CustodyDriverConnection,
    LocalSQLiteWriterScope,
)
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.plugins.storage.native import filesystem, synchronize_file, validate_acl
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.sqlite.driver import Connection, Error, NativeRetirement


def local_sqlite_writer(
    settings: DatabaseSettings,
    root: CustodyNativeDirectory,
    lifetime: CustodyLifetimeLease,
    participant: CustodyParticipantRegistration,
) -> tuple[Database, LocalSQLiteWriterEntry]:
    register_custody_fork()
    scope = LocalSQLiteWriterScope(root, lifetime, participant)
    return Database(
        settings, require_sqlite_full_sync=True, _local_sqlite_writer=scope
    ), scope.entry


_isolated_incomplete: AsyncExitStack | None = None


def isolated_marker_owner_retained() -> bool:
    return _isolated_incomplete is not None


def _anchor_values(prefix: str, anchor: CustodyFileAnchor) -> NativeRow:
    return {
        f"{prefix}_device": anchor.device,
        f"{prefix}_inode": anchor.inode,
        f"{prefix}_path": anchor.canonical_path,
    }


def _isolated_initial_rows(
    root: CustodyNativeDirectory,
    main: IsolatedMetadataMain,
    provider: LocalArtifactStorage,
    provider_root: CustodyFileAnchor,
    revision: str,
) -> tuple[NativeRow, NativeRow]:
    metadata: NativeRow = {
        "singleton": 1,
        "metadata_id": secrets.token_bytes(16),
        "metadata_backend": "sqlite",
        "qualification_digest": None,
        **_anchor_values("metadata_sqlite", main.anchor),
        "system_identifier": None,
        "database_oid": None,
        "connection_authority_digest": None,
    }
    authority: NativeRow = {
        **metadata,
        "format_version": ISOLATED_AUTHORITY_VERSION,
        "journal_id": secrets.token_bytes(16),
        "host_id": root.process.host_id,
        **_anchor_values("parent", root.parent_anchor),
        **_anchor_values("root", root.root_anchor),
        **_anchor_values("journal", root.file_anchors["authority.sqlite3"]),
        **_anchor_values("provider_root", provider_root),
        **_anchor_values("recovery", root.file_anchors["recovery.lock"]),
        **_anchor_values("lifetime", root.file_anchors["lifetime.lock"]),
        **_anchor_values("commit", root.file_anchors["commit.lock"]),
        **_anchor_values("journal_order", root.file_anchors["journal-order.lock"]),
        "provider_identity": provider.identity().model_dump_json().encode(),
        "mode": "provisioning",
        "generation": 1,
        "allocated_commit": 0,
        "allocated_issuer": 0,
        "confirmed_generation": 0,
        "confirmed_commit": 0,
        "confirmed_token": bytes(16),
        "recovery_priority_incarnation": None,
    }
    provisioning: NativeRow = {
        **metadata,
        "format_version": 1,
        "recovery_incarnation": 1,
        "host_id": root.process.host_id,
        "boot_id": root.process.boot_id,
        "pid_namespace": root.process.pid_namespace,
        "pid": root.process.pid,
        "start_time": root.process.start_time,
        "alembic_revision": revision,
        "phase": "schema_running",
        "migration_connection_sequence": None,
        "backend_pid": None,
        "backend_start_us": None,
        "server_start_us": None,
    }
    return authority, provisioning


@dataclass(frozen=True)
class IsolatedSchema:
    scripts: Path
    checkpoint: Callable[[], None]
    metadata_retired: Callable[[], None]
    journal_dispatch: Callable[[], None]
    journal_committed: Callable[[], None]
    journal_retired: Callable[[], None]


async def isolated_marker_checkpoints(
    main: IsolatedMetadataMain,
    revision: str,
    journal_checkpoint: Callable[[], None],
    metadata_checkpoint: Callable[[], None],
    *,
    schema: IsolatedSchema | None = None,
) -> int:
    global _isolated_incomplete
    if _isolated_incomplete is not None or revision != ISOLATED_FRONTIER_REVISION:
        raise CustodyUnavailable
    main.require_live()
    provider_path = main.path.parent / "provider"
    provider_stat = provider_path.stat(follow_symlinks=False)
    with os.scandir(provider_path) as entries:
        if next(entries, None) is not None:
            raise CustodyUnavailable
    provider_root = file_anchor(provider_stat, provider_path)
    scopes = AsyncExitStack()
    provider = LocalArtifactStorage(
        LocalStorageConfiguration(
            root=str(provider_path), limits=StorageLimits(version=1)
        )
    )
    scopes.push_async_callback(provider.aclose)
    driver: CustodyDriverConnection | None = None
    journal: CustodyJournalLease | None = None
    schema_dispatched = False
    _isolated_incomplete = scopes

    async def finish_owner() -> None:
        global _isolated_incomplete
        if journal is not None:
            if journal.last_outcome is not CustodyNativeOutcome.UNKNOWN:
                released = asyncio.Event()
                if journal.defer_sqlite_checkout(released.set):
                    await released.wait()
            await journal.close()
        await scopes.aclose()
        _isolated_incomplete = None

    try:
        root = CustodyNativeDirectory(
            main.path.parent / "custody-parent" / "custody",
            NativeCustodyPlatform(),
            provision=True,
            _isolated_launch=main.require_live,
        )
        scopes.callback(root.close)
        scopes.callback(root.lock("recovery.lock").close)
        scopes.callback(root.lock("lifetime.lock").close)
        authority, provisioning = _isolated_initial_rows(
            root, main, provider, provider_root, revision
        )
        lease = journal = open_isolated_journal_lease(root)
        await lease.initialize_isolated(authority, provisioning, journal_checkpoint)

        def retain(candidate: Connection) -> None:
            NativeRetirement.retain(
                main.path, candidate, lambda: driver_owner.observe_retirement(candidate)
            )

        async def require_metadata_retired() -> None:
            if not driver_owner.retired:
                raise CustodyUnavailable

        attempt = CustodySQLiteConnection(retain)
        driver_owner = CustodyDriverConnection(lambda: main.open(attempt))
        driver = driver_owner
        driver_owner.native_candidate = attempt
        scopes.push_async_callback(require_metadata_retired)

        async def finish_schema(selected: IsolatedSchema, sequence: int) -> None:
            root.require_isolated()
            selected.metadata_retired()
            try:
                await lease.complete_isolated_schema(
                    authority,
                    provisioning,
                    sequence,
                    selected.journal_dispatch,
                    selected.journal_committed,
                )
            except (OSError, Error, CustodyUnavailable):
                if lease.last_outcome is not CustodyNativeOutcome.UNKNOWN:
                    await finish_owner()
                raise
            selected.journal_retired()
            await finish_owner()

        async def run_schema(selected: IsolatedSchema, sequence: int) -> None:
            migrated = False

            def retire_names() -> bool:
                with custody_native_job():
                    return attempt.retire_isolated_migration_names()

            try:
                await main.migrate(
                    driver_owner, attempt, selected.scripts, selected.checkpoint
                )
                migrated = True
            finally:
                try:
                    if not driver_owner.joined:
                        await driver_owner.close()
                finally:
                    if not driver_owner.retired:
                        custody_log(CUSTODY_UNFINISHED, "native_turn")
                        retired = asyncio.Event()
                        driver_owner.after_retirement(retired.set)
                        await retired.wait()
                    if not await asyncio.to_thread(retire_names):
                        raise CustodyUnavailable
                    if migrated:
                        await finish_schema(selected, sequence)
                    else:
                        await finish_owner()

        try:
            await complete(driver_owner)
            sequence = next(LOCAL_SQLITE_NATIVE_SEQUENCE)
            await lease.bind_isolated(authority, provisioning, sequence)
            await complete(driver_owner.custody_worker(lambda: main.mark(attempt)))
            metadata_checkpoint()
            if schema is not None:
                caller = asyncio.current_task()
                if caller is None or caller.cancelling():
                    raise asyncio.CancelledError
                schema_dispatched = True
                try:
                    await complete_native_journal_job(run_schema(schema, sequence))
                except (OSError, Error, CustodyUnavailable):
                    if isolated_marker_owner_retained():
                        custody_log(CUSTODY_UNFINISHED, "native_turn")
                    raise
        finally:

            async def retire_after_j() -> None:
                try:
                    await driver_owner.close()
                    if driver_owner.retired:
                        await finish_owner()
                except (OSError, Error, CustodyUnavailable):
                    custody_log(CUSTODY_UNFINISHED, "native_turn")

            def resume_retirement() -> None:
                asyncio.create_task(retire_after_j())

            if not schema_dispatched and not lease.defer_sqlite_checkout(
                resume_retirement
            ):
                await driver_owner.close()
        if not driver_owner.retired:
            raise CustodyUnavailable
        if not schema_dispatched:
            root.require_isolated()
        return sequence
    finally:
        if not schema_dispatched and (driver is None or driver.retired):
            await complete_native_journal_job(finish_owner())


class NativeCustodyPlatform:
    _identity: NativeIdentityReader

    def __init__(self) -> None:
        register_custody_fork()
        self._identity = NativeIdentityReader()

    def current_process(self) -> CustodyProcessIdentity:
        observation = self._identity.read(os.getpid())
        if (
            observation.state is not IdentityState.PRESENT
            or observation.process is None
        ):
            raise CustodyUnavailable
        process = observation.process
        identity = process.identity
        if (
            process.dead
            or identity.host_id is None
            or identity.boot_id is None
            or identity.pid_namespace is None
        ):
            raise CustodyUnavailable
        return CustodyProcessIdentity(
            identity.host_id,
            identity.boot_id,
            identity.pid_namespace,
            identity.pid,
            identity.start_time,
        )

    def require_process_absent(self, previous: CustodyProcessIdentity) -> None:
        current = self.current_process()
        if (
            previous.host_id != current.host_id
            or previous.boot_id != current.boot_id
            or previous.pid_namespace != current.pid_namespace
            or self._identity.read(previous.pid).state is not IdentityState.ABSENT
        ):
            raise CustodyUnavailable

    def inspect_filesystem(self, descriptor: int) -> tuple[str, str]:
        try:
            return filesystem(descriptor)
        except KoraProtocolError:
            raise CustodyUnavailable from None

    def require_private_acl(self, descriptor: int) -> None:
        try:
            validate_acl(descriptor)
        except KoraProtocolError:
            raise CustodyUnavailable from None

    def persist_file(self, descriptor: int) -> None:
        synchronize_file(descriptor)

    def persist_directory(self, descriptor: int) -> None:
        os.fsync(descriptor)
