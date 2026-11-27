import asyncio
import secrets
import sqlite3
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, replace
from typing import Final, TypeAlias, TypeVar, cast

from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    and_,
    bindparam,
    case,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.sql import ClauseElement

from kora.db import CUSTODY_REFUSED, CUSTODY_UNFINISHED, custody_log
from kora.db.completion import complete
from kora.db.custody_models import (
    MAX_CUSTODY_PARTICIPANTS,
    MAX_CUSTODY_ROW_BYTES,
    CustodyCommitPhase,
    CustodyFileAnchor,
    CustodyFrontier,
    CustodyGate,
    CustodyIssuerKind,
    CustodyMode,
    CustodyNativeOutcome,
    CustodyParticipantRegistration,
    CustodyPendingCommit,
    CustodyProcessIdentity,
    CustodyRecoveryRegistration,
    CustodyTransactionBinding,
    CustodyUnavailable,
    custody_ascii,
    custody_counter,
    custody_identity,
    native_identifier,
)
from kora.db.custody_native import (
    CustodyDescriptor,
    CustodyLatch,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    CustodyNativeWait,
    CustodyRecoveryLease,
    custody_native_job,
)
from kora.db.custody_schema import (
    AUTHORITY,
    IMAGE,
    INVOCATION_COLUMNS,
    PARTICIPANT,
    PENDING,
    PROVISIONING,
    RECOVERY,
    RELATIONS,
    CustodyColumn,
    CustodyRelation,
)
from kora.db.custody_transaction import (
    AppliedCustodyFrontier,
    OriginalCustodyTransaction,
    ReleasedCustodyCommit,
)
from kora.sqlite.driver import Connection, NativeRetirement

NativeValue: TypeAlias = int | str | bytes | None
NativeRow: TypeAlias = dict[str, NativeValue]
Result = TypeVar("Result")
ACTIVE_LEASES: Final[set["CustodyJournalLease"]] = set()
SQLITE_DIALECT: Final = sqlite_dialect(paramstyle="named")


async def complete_native_journal_job(pending: Awaitable[Result]) -> Result:
    task = asyncio.ensure_future(pending)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def settled(_: asyncio.Future[Result]) -> None:
            if not waiter.done():
                waiter.set_result(None)

        task.add_done_callback(settled)
        try:
            while not task.done():
                try:
                    await waiter
                except asyncio.CancelledError:
                    waiter = asyncio.get_running_loop().create_future()
        finally:
            task.remove_done_callback(settled)
        if not task.cancelled():
            task.exception()
        raise


def native_column(column: CustodyColumn) -> Column[int] | Column[bytes] | Column[str]:
    if column.kind == "integer":
        return Column(column.name, Integer())
    if column.kind == "blob":
        return Column(column.name, LargeBinary())
    if column.kind == "text":
        return Column(column.name, Text())
    raise CustodyUnavailable


TABLES: Final[dict[str, Table]] = {
    relation.name: Table(
        relation.name,
        MetaData(),
        *(native_column(column) for column in relation.columns),
    )
    for relation in RELATIONS
}


def native_statement(
    statement: ClauseElement, arguments: Mapping[str, NativeValue] | None = None
) -> tuple[str, dict[str, NativeValue]]:
    compiled = statement.compile(dialect=SQLITE_DIALECT)
    values: dict[str, NativeValue] = {}
    for name, value in cast("Mapping[str, object]", compiled.params).items():
        if value is not None and type(value) not in {int, str, bytes}:
            raise CustodyUnavailable
        values[name] = cast("NativeValue", value)
    if arguments is not None:
        values.update(arguments)
    return str(compiled), values


def require_relation(relation: CustodyRelation) -> None:
    if relation not in RELATIONS:
        raise CustodyUnavailable


def checked_value(column: CustodyColumn, value: object) -> NativeValue:
    if value is None and column.nullable:
        return None
    if type(value) is int and column.kind == "integer" and -(2**63) <= value < 2**63:
        return value
    if (
        type(value) is str
        and column.kind == "text"
        and value.isascii()
        and len(value) <= MAX_CUSTODY_ROW_BYTES
    ):
        return value
    if (
        type(value) is bytes
        and column.kind == "blob"
        and len(value) <= MAX_CUSTODY_ROW_BYTES
    ):
        return value
    raise CustodyUnavailable


def scalar(connection: Connection, statement: str) -> object:
    with closing(connection.execute(statement)) as cursor:
        row: object = cursor.fetchone()
    if type(row) is not tuple:
        raise CustodyUnavailable
    values = cast("tuple[object, ...]", row)
    if len(values) != 1:
        raise CustodyUnavailable
    return values[0]


def open_journal(
    root: CustodyNativeDirectory, connection: Connection, *, initialize: bool
) -> None:
    root.require_current()
    connection.open(
        root.path / "authority.sqlite3",
        False,
        0,
        lambda fd, empty: root.admit_journal_main(fd, empty, initialize=initialize),
        journal=root.admit_rollback_journal,
        directory=root.journal_directory(),
        custody=True,
        authority_journal=True,
    )
    connection.set_length_limit(MAX_CUSTODY_ROW_BYTES)
    connection.execute("PRAGMA foreign_keys=ON").close()
    if initialize:
        connection.execute("PRAGMA page_size=4096").close()
        scalar(connection, "PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=EXTRA").close()
    if sys.platform == "darwin":
        connection.execute("PRAGMA fullfsync=ON").close()
        if scalar(connection, "PRAGMA fullfsync") != 1:
            raise CustodyUnavailable
    if (
        scalar(connection, "PRAGMA journal_mode") != "delete"
        or scalar(connection, "PRAGMA synchronous") != 3
        or scalar(connection, "PRAGMA page_size") != 4096
        or scalar(connection, "PRAGMA max_page_count=4096") != 4096
        or scalar(connection, "PRAGMA auto_vacuum") != 0
    ):
        raise CustodyUnavailable


class CustodyJournalPhase:
    def __init__(self, root: CustodyNativeDirectory) -> None:
        self._root = root
        self._loop = asyncio.get_running_loop()
        self._order: CustodyNativeWait | None = None
        self._attempt: Connection | None = None
        self._running = False
        self._retired = False
        self._closing_order = False
        self._resume: Callable[[], None] | None = None

    @property
    def released(self) -> bool:
        return self._order is None

    def start_job(self) -> None:
        self._running = True

    def finish_job(self) -> None:
        self._running = False
        self._finish_retirement()

    def after_release(self, resume: Callable[[], None]) -> None:
        if self._order is None:
            resume()
        elif self._resume is None:
            self._resume = resume

    @contextmanager
    def connection(self, *, initialize: bool = False) -> Generator[Connection]:
        if self._order is not None:
            raise CustodyUnavailable
        order = self._root.open_wait("journal-order.lock")
        self._order = order
        self._retired = False
        self._closing_order = False
        try:
            order.acquire()
            with custody_native_job():
                attempt = Connection(self._retain)
                self._attempt = attempt
                try:
                    open_journal(self._root, attempt, initialize=initialize)
                    yield attempt
                finally:
                    attempt.close()
            self._root.require_current()
        finally:
            attempt = self._attempt
            if attempt is None or attempt.settled():
                self._close_order(order)

    def _retain(self, attempt: Connection) -> None:
        def notify() -> None:
            try:
                self._loop.call_soon_threadsafe(self._observe_retired, attempt)
            except RuntimeError:
                if not self._loop.is_closed():
                    raise

        if not attempt.settled():
            NativeRetirement.retain(
                self._root.path / "authority.sqlite3",
                attempt,
                notify,
            )

    def _observe_retired(self, attempt: Connection) -> None:
        if self._attempt is attempt:
            self._retired = True
            self._finish_retirement()

    def _finish_retirement(self) -> None:
        order = self._order
        if self._running or not self._retired or self._closing_order or order is None:
            return
        try:
            self._close_order(order)
        except (OSError, CustodyUnavailable):
            custody_log(CUSTODY_UNFINISHED, "ordinary_admission")
            return
        resume, self._resume = self._resume, None
        if resume is not None:
            resume()

    def _close_order(self, order: CustodyNativeWait) -> None:
        self._closing_order = True
        order.close()
        self._attempt = None
        self._order = None


class CustodyJournalTransaction:
    _connection: Connection
    _completion_owner: object | None
    _open: bool

    def __init__(
        self, connection: Connection, *, completion_owner: object | None
    ) -> None:
        self._connection = connection
        self._completion_owner = completion_owner
        self._open = True

    def close(self) -> None:
        self._open = False

    def _require_current(self, relation: CustodyRelation) -> None:
        if not self._open or not self._connection.in_transaction:
            raise CustodyUnavailable
        require_relation(relation)

    def read(self, relation: CustodyRelation, key: int = 1) -> NativeRow | None:
        self._require_current(relation)
        key_column = next(
            column for column in relation.columns if column.name == relation.key
        )
        checked_value(key_column, key)
        predicates = (
            *relation.constraints,
            *(column.predicate for column in relation.columns),
        )
        table = TABLES[relation.name]
        admitted = and_(
            *(text(f"({condition})") for condition in predicates),
            text(f"({relation.width}) <= {MAX_CUSTODY_ROW_BYTES}"),
        )
        admission = (
            select(case((admitted, 1), else_=0))
            .select_from(table)
            .where(table.c[relation.key] == bindparam("bound_key"))
        )
        statement, parameters = native_statement(admission, {"bound_key": key})
        try:
            with closing(self._connection.execute(statement, parameters)) as cursor:
                decision: object = cursor.fetchone()
        except sqlite3.DataError:
            raise CustodyUnavailable from None
        if decision is None:
            return None
        if decision != (1,):
            raise CustodyUnavailable
        selection = select(table).where(table.c[relation.key] == bindparam("bound_key"))
        statement, parameters = native_statement(selection, {"bound_key": key})
        with closing(self._connection.execute(statement, parameters)) as cursor:
            row: object = cursor.fetchone()
        if type(row) is not tuple:
            raise CustodyUnavailable
        values = cast("tuple[object, ...]", row)
        if len(values) != len(relation.columns):
            raise CustodyUnavailable
        parsed = {
            column.name: checked_value(column, value)
            for column, value in zip(relation.columns, values, strict=True)
        }
        if parsed["format_version"] != (2 if relation is AUTHORITY else 1):
            raise CustodyUnavailable
        return parsed

    def insert(self, relation: CustodyRelation, values: Mapping[str, object]) -> None:
        if relation == PENDING:
            raise CustodyUnavailable
        self._insert(relation, values)

    def _insert(self, relation: CustodyRelation, values: Mapping[str, object]) -> None:
        self._require_current(relation)
        if set(values) != {column.name for column in relation.columns}:
            raise CustodyUnavailable
        admitted = {
            column.name: checked_value(column, values[column.name])
            for column in relation.columns
        }
        statement, parameters = native_statement(
            insert(TABLES[relation.name]).values(admitted)
        )
        with closing(self._connection.execute(statement, parameters)):
            pass

    def update(
        self, relation: CustodyRelation, values: Mapping[str, object], *, key: int = 1
    ) -> None:
        if relation in (PENDING, AUTHORITY):
            raise CustodyUnavailable
        self._update(relation, values, key=key)

    def _update(
        self, relation: CustodyRelation, values: Mapping[str, object], *, key: int = 1
    ) -> None:
        self._require_current(relation)
        selected = tuple(column for column in relation.columns if column.name in values)
        if not selected or set(values) != {column.name for column in selected}:
            raise CustodyUnavailable
        admitted = {
            column.name: checked_value(column, values[column.name])
            for column in selected
        }
        table = TABLES[relation.name]
        mutation = (
            update(table)
            .values(admitted)
            .where(table.c[relation.key] == bindparam("bound_key"))
        )
        statement, parameters = native_statement(mutation, {"bound_key": key})
        with closing(self._connection.execute(statement, parameters)) as cursor:
            if cursor.rowcount != 1:
                raise CustodyUnavailable

    def delete(self, relation: CustodyRelation, *, key: int = 1) -> None:
        if relation in (PENDING, AUTHORITY):
            raise CustodyUnavailable
        self._delete(relation, key=key)

    def _delete(self, relation: CustodyRelation, *, key: int = 1) -> None:
        self._require_current(relation)
        table = TABLES[relation.name]
        mutation = delete(table).where(table.c[relation.key] == bindparam("bound_key"))
        statement, parameters = native_statement(mutation, {"bound_key": key})
        with closing(self._connection.execute(statement, parameters)) as cursor:
            if cursor.rowcount != 1:
                raise CustodyUnavailable

    def reserve_ordinary_participant(
        self, lifetime: CustodyLifetimeLease, *, journal_id: bytes, generation: int
    ) -> CustodyParticipantRegistration:
        root = lifetime.require_current()
        authority = self._ordinary_authority(root, journal_id, generation)
        sequence = custody_counter(
            custody_counter(authority["allocated_issuer"], allow_zero=True) + 1
        )
        for slot in range(MAX_CUSTODY_PARTICIPANTS):
            if self.read(PARTICIPANT, slot) is None:
                participant = CustodyParticipantRegistration(
                    slot, journal_id, generation, sequence, root.process
                )
                self._update(AUTHORITY, {"allocated_issuer": sequence})
                self._insert(PARTICIPANT, participant_registration_values(participant))
                return participant
        raise CustodyUnavailable

    def require_ordinary_checkout(
        self,
        lifetime: CustodyLifetimeLease,
        participant: CustodyParticipantRegistration,
    ) -> NativeRow:
        root = lifetime.require_current()
        authority = self._ordinary_authority(
            root, participant.journal_id, participant.generation
        )
        saved = self.read(PARTICIPANT, participant.slot)
        if (
            saved is None
            or root.process != participant.process
            or saved["gate"] != CustodyGate.OPEN
            or saved["journal_id"] != participant.journal_id
            or saved["generation"] != participant.generation
            or saved["issuer_sequence"] != participant.issuer_sequence
            or participant.issuer_sequence
            > custody_counter(authority["allocated_issuer"])
            or not matches_process(saved, participant.process)
        ):
            raise CustodyUnavailable
        return authority

    def _ordinary_authority(
        self, root: CustodyNativeDirectory, journal_id: bytes, generation: int
    ) -> NativeRow:
        root.require_open_latch()
        authority = self._anchored_authority(root, journal_id, generation)
        recovery = self.read(RECOVERY)
        if (
            authority["mode"] != CustodyMode.ACTIVE
            or recovery is None
            or recovery["host_id"] != root.process.host_id
            or recovery["boot_id"] != root.process.boot_id
            or recovery["gate"] != CustodyGate.OPEN
            or custody_counter(recovery["incarnation"])
            > custody_counter(authority["allocated_issuer"])
            or self.read(PENDING) is not None
            or authority["recovery_priority_incarnation"] is not None
        ):
            raise CustodyUnavailable
        return authority

    def reserve_recovery_coordinator(
        self,
        coordinator: CustodyRecoveryLease,
        lifetime: CustodyLifetimeLease,
        *,
        journal_id: bytes,
        generation: int,
    ) -> CustodyRecoveryRegistration:
        root = coordinator.require_current()
        if lifetime.require_current() is not root:
            raise CustodyUnavailable
        authority = self._anchored_authority(root, journal_id, generation)
        if self.read(RECOVERY) is not None:
            raise CustodyUnavailable
        incarnation = custody_counter(
            custody_counter(authority["allocated_issuer"], allow_zero=True) + 1
        )
        recovery = CustodyRecoveryRegistration(
            journal_id, generation, incarnation, root.process
        )
        self._update(AUTHORITY, {"allocated_issuer": incarnation})
        self._insert(RECOVERY, recovery_registration_values(recovery))
        return recovery

    def reconcile_native_only_successor(
        self,
        coordinator: CustodyRecoveryLease,
        lifetime: CustodyLifetimeLease,
        turn: CustodyNativeWait,
        *,
        journal_id: bytes,
        generation: int,
    ) -> CustodyRecoveryRegistration:
        root = coordinator.require_current()
        if lifetime.require_current() is not root:
            raise CustodyUnavailable
        turn.require_commit(root)
        authority = self._anchored_authority(root, journal_id, generation)
        previous = self.read(RECOVERY)
        allocation = custody_counter(authority["allocated_issuer"], allow_zero=True)
        journal = root.file_anchors["authority.sqlite3"]
        if (
            root.read_latch() is not CustodyLatch.BOOTSTRAP_FROM_OPEN
            or authority["mode"] != CustodyMode.ACTIVE
            or authority["metadata_backend"] != "sqlite"
            or authority["metadata_sqlite_device"] != journal.device
            or authority["metadata_sqlite_inode"] != journal.inode
            or authority["metadata_sqlite_path"] != journal.canonical_path
            or authority["recovery_priority_incarnation"] is not None
            or self.read(PENDING) is not None
            or self.read(PROVISIONING) is not None
            or self.read(IMAGE) is not None
            or previous is None
            or custody_counter(previous["incarnation"]) > allocation
            or previous["gate"] != CustodyGate.OPEN
            or previous["allocated_receiver"] != 0
            or previous["allocated_invocation"] != 0
            or previous["receiver_sequence"] is not None
            or previous["owner_kind"] is not None
        ):
            raise CustodyUnavailable

        root.require_process_absent(
            CustodyProcessIdentity(
                custody_ascii(previous["host_id"]),
                custody_ascii(previous["boot_id"]),
                custody_ascii(previous["pid_namespace"]),
                custody_counter(previous["pid"]),
                custody_counter(previous["start_time"], allow_zero=True),
            )
        )
        incarnation = custody_counter(allocation + 1)
        recovery = CustodyRecoveryRegistration(
            journal_id, generation, incarnation, root.process
        )
        values = recovery_registration_values(recovery)
        del values[RECOVERY.key]
        self._update(RECOVERY, values)
        self._update(AUTHORITY, {"allocated_issuer": incarnation})
        return recovery

    def current_recovery_priority(
        self,
        coordinator: CustodyRecoveryLease,
        registration: CustodyRecoveryRegistration,
        *,
        request: bool,
        verify: bool = False,
    ) -> None:
        root = coordinator.require_current()
        authority = self._anchored_authority(
            root, registration.journal_id, registration.generation
        )
        saved = self.read(RECOVERY)
        if (
            authority["mode"] != CustodyMode.ACTIVE
            or saved is None
            or saved["incarnation"] != registration.incarnation
            or registration.incarnation > custody_counter(authority["allocated_issuer"])
            or saved["gate"] != CustodyGate.OPEN
            or root.process != registration.process
            or not matches_process(saved, registration.process)
            or authority["recovery_priority_incarnation"]
            not in (None, registration.incarnation)
        ):
            raise CustodyUnavailable
        incarnation = registration.incarnation if request else None
        if authority["recovery_priority_incarnation"] != incarnation:
            if verify:
                raise CustodyUnavailable
            self._update(AUTHORITY, {"recovery_priority_incarnation": incarnation})

    def require_live_priority(
        self,
        coordinator: CustodyRecoveryLease,
        registration: CustodyRecoveryRegistration,
    ) -> None:
        self.current_recovery_priority(
            coordinator, registration, request=True, verify=True
        )
        if self.read(PENDING) is not None:
            raise CustodyUnavailable

    def require_fresh_recovery(
        self, coordinator: CustodyRecoveryLease, journal_id: bytes, generation: int
    ) -> None:
        authority = self._anchored_authority(
            coordinator.require_current(), journal_id, generation
        )
        if (
            authority["mode"] != CustodyMode.ACTIVE
            or authority["recovery_priority_incarnation"] is not None
            or self.read(PENDING) is not None
        ):
            raise CustodyUnavailable

    def _anchored_authority(
        self, root: CustodyNativeDirectory, journal_id: bytes, generation: int
    ) -> NativeRow:
        authority = self.read(AUTHORITY)
        if (
            authority is None
            or authority["format_version"] != 2
            or authority["journal_id"] != journal_id
            or authority["generation"] != generation
            or authority["host_id"] != root.process.host_id
            or not matches_journal_anchors(authority, root)
        ):
            raise CustodyUnavailable
        return authority

    def prepare_commit(
        self,
        *,
        journal_id: bytes,
        generation: int,
        previous: CustodyFrontier,
        issuer_kind: CustodyIssuerKind,
        original: CustodyTransactionBinding,
    ) -> CustodyPendingCommit:
        authority = self.read(AUTHORITY)
        if (
            authority is None
            or self.read(PENDING) is not None
            or authority["journal_id"] != journal_id
            or authority["generation"] != generation
            or authority["metadata_id"] != original.metadata_id
            or (
                authority["confirmed_generation"],
                authority["confirmed_commit"],
                authority["confirmed_token"],
            )
            != (previous.generation, previous.sequence, previous.token)
            or original.issuer_sequence
            > custody_counter(authority["allocated_issuer"], allow_zero=True)
        ):
            raise CustodyUnavailable
        postgres = original.postgres
        if (authority["system_identifier"], authority["database_oid"]) != (
            (None, None)
            if postgres is None
            else (postgres.system_identifier, postgres.database_oid)
        ):
            raise CustodyUnavailable
        sequence = custody_counter(
            custody_counter(authority["allocated_commit"], allow_zero=True) + 1
        )
        frontier = CustodyFrontier(
            generation, sequence, custody_identity(secrets.token_bytes(16))
        )
        pending = CustodyPendingCommit(
            journal_id,
            issuer_kind,
            frontier,
            previous,
            original,
            CustodyCommitPhase.PREPARED,
        )
        self._update(AUTHORITY, {"allocated_commit": sequence})
        self._insert(PENDING, pending_commit_values(pending))
        return pending

    def permit_original_dispatch(self, applied: AppliedCustodyFrontier) -> None:
        if type(applied) is not AppliedCustodyFrontier:
            raise CustodyUnavailable
        applied.require_owner(self._require_completion_owner())
        pending = applied.pending
        if pending.phase is not CustodyCommitPhase.PREPARED:
            raise CustodyUnavailable
        self._require_exact_pending(pending, (CustodyCommitPhase.PREPARED,))
        self._update(PENDING, {"phase": CustodyCommitPhase.DISPATCHING.value})

    def record_original_outcome(
        self, released: ReleasedCustodyCommit
    ) -> CustodyPendingCommit:
        released.require_owner(self._require_completion_owner())
        pending, outcome = released.pending, released.outcome
        if outcome is CustodyNativeOutcome.COMMITTED:
            phase = CustodyCommitPhase.COMMITTED
            allowed = (CustodyCommitPhase.DISPATCHING, phase)
        elif outcome is CustodyNativeOutcome.ABORTED:
            phase = CustodyCommitPhase.ABORTED
            allowed = (
                CustodyCommitPhase.PREPARED,
                CustodyCommitPhase.DISPATCHING,
                phase,
            )
        else:
            raise CustodyUnavailable
        self._require_exact_pending(pending, allowed)
        self._update(PENDING, {"phase": phase.value})
        return replace(pending, phase=phase)

    def compact_original_commit(self, pending: CustodyPendingCommit) -> None:
        self._require_completion_owner()
        if pending.phase not in (
            CustodyCommitPhase.COMMITTED,
            CustodyCommitPhase.ABORTED,
        ):
            raise CustodyUnavailable
        self._require_exact_pending(pending, (pending.phase,))
        authority = self.read(AUTHORITY)
        if (
            authority is None
            or authority["journal_id"] != pending.journal_id
            or authority["generation"] != pending.frontier.generation
            or authority["confirmed_generation"] != pending.previous.generation
            or authority["confirmed_commit"] != pending.previous.sequence
            or authority["confirmed_token"] != pending.previous.token
            or authority["metadata_id"] != pending.transaction.metadata_id
            or pending.frontier.sequence
            > custody_counter(authority["allocated_commit"])
        ):
            raise CustodyUnavailable
        if pending.phase is CustodyCommitPhase.COMMITTED:
            self._update(
                AUTHORITY,
                {
                    "confirmed_generation": pending.frontier.generation,
                    "confirmed_commit": pending.frontier.sequence,
                    "confirmed_token": pending.frontier.token,
                },
            )
        self._delete(PENDING)

    def _require_completion_owner(self) -> object:
        owner = self._completion_owner
        if owner is None:
            raise CustodyUnavailable
        return owner

    def _require_exact_pending(
        self, pending: CustodyPendingCommit, phases: tuple[CustodyCommitPhase, ...]
    ) -> None:
        saved = self.read(PENDING)
        if saved is None or saved["phase"] not in phases:
            raise CustodyUnavailable
        expected = pending_commit_values(pending)
        expected["phase"] = saved["phase"]
        if saved != expected:
            raise CustodyUnavailable


def matches_journal_anchors(authority: NativeRow, root: CustodyNativeDirectory) -> bool:
    anchors: tuple[tuple[str, CustodyFileAnchor], ...] = (
        ("parent", root.parent_anchor),
        ("root", root.root_anchor),
        ("journal", root.file_anchors["authority.sqlite3"]),
        ("lifetime", root.file_anchors["lifetime.lock"]),
        ("commit", root.file_anchors["commit.lock"]),
        ("recovery", root.file_anchors["recovery.lock"]),
        ("journal_order", root.file_anchors["journal-order.lock"]),
    )
    return all(
        (
            authority[f"{name}_device"],
            authority[f"{name}_inode"],
            authority[f"{name}_path"],
        )
        == (anchor.device, anchor.inode, anchor.canonical_path)
        for name, anchor in anchors
    )


def matches_process(saved: NativeRow, process: CustodyProcessIdentity) -> bool:
    return (
        saved["host_id"],
        saved["boot_id"],
        saved["pid_namespace"],
        saved["pid"],
        saved["start_time"],
    ) == (
        process.host_id,
        process.boot_id,
        process.pid_namespace,
        process.pid,
        process.start_time,
    )


def participant_registration_values(
    participant: CustodyParticipantRegistration,
) -> NativeRow:
    process = participant.process
    return {
        "slot": participant.slot,
        "format_version": 1,
        "journal_id": participant.journal_id,
        "generation": participant.generation,
        "issuer_sequence": participant.issuer_sequence,
        "host_id": process.host_id,
        "boot_id": process.boot_id,
        "pid_namespace": process.pid_namespace,
        "pid": process.pid,
        "start_time": process.start_time,
        "gate": CustodyGate.OPEN.value,
        "allocated_receiver": 0,
        "allocated_invocation": 0,
        "receiver_sequence": None,
        "receiver_state": None,
        "closure_revision": None,
    }


def recovery_registration_values(recovery: CustodyRecoveryRegistration) -> NativeRow:
    process = recovery.process
    return {
        "singleton": 1,
        "format_version": 1,
        "incarnation": recovery.incarnation,
        "host_id": process.host_id,
        "boot_id": process.boot_id,
        "pid_namespace": process.pid_namespace,
        "pid": process.pid,
        "start_time": process.start_time,
        "gate": CustodyGate.OPEN.value,
        "allocated_receiver": 0,
        "allocated_invocation": 0,
        "receiver_sequence": None,
        "receiver_state": None,
        **{column.name: None for column in INVOCATION_COLUMNS},
    }


def pending_commit_values(pending: CustodyPendingCommit) -> NativeRow:
    original = pending.transaction
    postgres = original.postgres
    return {
        "singleton": 1,
        "format_version": 1,
        "journal_id": pending.journal_id,
        "generation": pending.frontier.generation,
        "commit_sequence": pending.frontier.sequence,
        "token": pending.frontier.token,
        "previous_generation": pending.previous.generation,
        "previous_commit": pending.previous.sequence,
        "previous_token": pending.previous.token,
        "issuer_sequence": original.issuer_sequence,
        "issuer_kind": pending.issuer_kind.value,
        "metadata_id": original.metadata_id,
        "system_identifier": postgres.system_identifier
        if postgres is not None
        else None,
        "database_oid": postgres.database_oid if postgres is not None else None,
        "connection_sequence": original.connection_sequence,
        "transaction_sequence": original.transaction_sequence,
        "settings_digest": original.settings_digest,
        "native_xid": postgres.xid if postgres is not None else None,
        "backend_pid": postgres.backend_pid if postgres is not None else None,
        "backend_start_us": postgres.backend_start_us if postgres is not None else None,
        "server_start_us": postgres.server_start_us if postgres is not None else None,
        "phase": pending.phase.value,
    }


@dataclass(frozen=True)
class CustodySQLiteInstallation:
    anchor: CustodyFileAnchor
    metadata_id: bytes
    frontier: CustodyFrontier


class CustodyJournalLease:
    _root: CustodyNativeDirectory
    _ownership: CustodyDescriptor
    _executor: ThreadPoolExecutor
    _pending: Awaitable[object] | None
    _open: bool
    _closing: bool
    _phase: CustodyJournalPhase
    _last_outcome: CustodyNativeOutcome | None
    _original_owner: object
    _original: OriginalCustodyTransaction | None
    _prepared: CustodyPendingCommit | None
    _sqlite_checkout: object | None

    def __init__(
        self, root: CustodyNativeDirectory, ownership: CustodyDescriptor
    ) -> None:
        self._root = root
        self._ownership = ownership
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kora-custody"
        )
        self._pending = None
        self._open = True
        self._closing = False
        self._phase = CustodyJournalPhase(root)
        self._last_outcome = None
        self._original_owner = object()
        self._original = None
        self._prepared = None
        self._sqlite_checkout = None
        ACTIVE_LEASES.add(self)

    @property
    def last_outcome(self) -> CustodyNativeOutcome | None:
        return self._last_outcome

    async def run(
        self, operation: Callable[[CustodyJournalTransaction], Result]
    ) -> Result:
        return await self._run_transaction(operation)

    async def require_sqlite_installation(
        self,
        lifetime: CustodyLifetimeLease,
        participant: CustodyParticipantRegistration,
    ) -> CustodySQLiteInstallation:
        authority = await self.run(
            lambda journal: journal.require_ordinary_checkout(lifetime, participant)
        )
        path = authority["metadata_sqlite_path"]
        if authority["metadata_backend"] != "sqlite" or type(path) is not bytes:
            raise CustodyUnavailable
        anchor = CustodyFileAnchor(
            native_identifier(authority["metadata_sqlite_device"]),
            native_identifier(authority["metadata_sqlite_inode"]),
            path,
        )
        if any(
            (item.device, item.inode) == (anchor.device, anchor.inode)
            for item in self._root.file_anchors.values()
        ):
            raise CustodyUnavailable
        return CustodySQLiteInstallation(
            anchor,
            custody_identity(authority["metadata_id"]),
            CustodyFrontier(
                custody_counter(authority["confirmed_generation"]),
                custody_counter(authority["confirmed_commit"]),
                custody_identity(authority["confirmed_token"]),
            ),
        )

    def own_sqlite_checkout(self, owner: object) -> None:
        if self._sqlite_checkout is not None:
            raise CustodyUnavailable
        self._sqlite_checkout = owner

    async def has_sqlite_preparation(
        self, original: OriginalCustodyTransaction
    ) -> bool:
        if self._original is None:
            return False
        if self._original is not original:
            raise CustodyUnavailable
        if self._prepared is not None:
            return True
        await complete(self._verify_no_preparation())
        return False

    async def _verify_no_preparation(self) -> None:
        if await self.run(lambda journal: journal.read(PENDING)) is not None:
            raise CustodyUnavailable
        self._original = None

    def release_sqlite_checkout(self, owner: object) -> None:
        if self._sqlite_checkout is not owner:
            raise CustodyUnavailable
        if self._original is None:
            self._sqlite_checkout = None

    async def prepare_original_commit(
        self,
        original: OriginalCustodyTransaction,
        *,
        journal_id: bytes,
        generation: int,
        previous: CustodyFrontier,
        issuer_kind: CustodyIssuerKind,
    ) -> CustodyPendingCommit:
        self._require_available(allow_closing=False)
        if self._original is not None or self._sqlite_checkout is None:
            raise CustodyUnavailable
        owner = object()
        binding = original.bind_custody_owner(owner)
        self._original_owner = owner
        self._original = original
        return await complete(
            self._persist_original_preparation(
                original,
                journal_id=journal_id,
                generation=generation,
                previous=previous,
                issuer_kind=issuer_kind,
                binding=binding,
            )
        )

    async def _persist_original_preparation(
        self,
        original: OriginalCustodyTransaction,
        *,
        journal_id: bytes,
        generation: int,
        previous: CustodyFrontier,
        issuer_kind: CustodyIssuerKind,
        binding: CustodyTransactionBinding,
    ) -> CustodyPendingCommit:
        await original.verify_native_custody_owner(self._original_owner)
        pending = await self._run_transaction(
            lambda journal: journal.prepare_commit(
                journal_id=journal_id,
                generation=generation,
                previous=previous,
                issuer_kind=issuer_kind,
                original=binding,
            ),
            allow_closing=True,
        )
        self._prepared = pending
        original.binding_for_preparation()
        return pending

    async def commit_prepared(
        self, original: OriginalCustodyTransaction, pending: CustodyPendingCommit
    ) -> None:
        self._require_available(allow_closing=False)
        if self._original is not original or self._prepared is not pending:
            raise CustodyUnavailable
        applied = await complete(
            original.advance_frontier_before_dispatch(pending, self._original_owner)
        )
        await self._run_transaction(
            lambda journal: journal.permit_original_dispatch(applied),
            completion_owner=self._original_owner,
        )
        await original.commit(custody_owner=self._original_owner)

    async def settle_original(self, original: OriginalCustodyTransaction) -> None:
        self._require_available(allow_closing=True)
        pending = self._prepared
        if self._original is not original or pending is None:
            raise CustodyUnavailable
        released = ReleasedCustodyCommit(original, pending, self._original_owner)
        await complete(self._settle(released))

    async def _settle(self, released: ReleasedCustodyCommit) -> None:
        finished = await self._run_transaction(
            lambda journal: journal.record_original_outcome(released),
            allow_closing=True,
            completion_owner=self._original_owner,
        )
        await self._run_transaction(
            lambda journal: journal.compact_original_commit(finished),
            allow_closing=True,
            completion_owner=self._original_owner,
        )
        self._prepared = None
        self._original = None

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def _run_transaction(
        self,
        operation: Callable[[CustodyJournalTransaction], Result],
        *,
        allow_closing: bool = False,
        completion_owner: object | None = None,
    ) -> Result:
        return await self._run(
            lambda: self._transaction(operation, completion_owner=completion_owner),
            allow_closing=allow_closing,
        )

    def _require_available(self, *, allow_closing: bool) -> None:
        if (
            not self._open
            or (self._closing and not allow_closing)
            or self._pending is not None
            or not self._phase.released
            or self._last_outcome is CustodyNativeOutcome.UNKNOWN
        ):
            raise CustodyUnavailable
        self._root.require_current()

    async def _run(
        self, operation: Callable[[], Result], *, allow_closing: bool = False
    ) -> Result:
        self._require_available(allow_closing=allow_closing)
        task = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        self._phase.start_job()
        self._pending = task
        try:
            return await complete_native_journal_job(task)
        finally:
            self._pending = None
            self._phase.finish_job()

    def after_journal_release(self, resume: Callable[[], None]) -> bool:
        if self._phase.released:
            return False
        self._phase.after_release(resume)
        return True

    def _transaction(
        self,
        operation: Callable[[CustodyJournalTransaction], Result],
        *,
        completion_owner: object | None,
    ) -> Result:
        def invoke(connection: Connection) -> Result:
            transaction = CustodyJournalTransaction(
                connection, completion_owner=completion_owner
            )
            try:
                return operation(transaction)
            finally:
                transaction.close()

        return self._perform(invoke)

    def _perform(
        self,
        operation: Callable[[Connection], Result],
        *,
        initialize: bool = False,
    ) -> Result:
        self._last_outcome = None
        with self._phase.connection(initialize=initialize) as connection:
            with closing(connection.execute("BEGIN IMMEDIATE")):
                pass
            try:
                value = operation(connection)
                self._root.require_current()
                self._last_outcome = CustodyNativeOutcome.UNKNOWN
                connection.commit()
                self._last_outcome = CustodyNativeOutcome.COMMITTED
            finally:
                if connection.in_transaction:
                    connection.rollback()
                    if self._last_outcome is None:
                        self._last_outcome = CustodyNativeOutcome.ABORTED
        return value

    def _initialize(self) -> None:
        def create(connection: Connection) -> None:
            for relation in RELATIONS:
                with closing(connection.execute(relation.create)):
                    pass

        self._perform(create, initialize=True)

    async def close(self) -> None:
        self._closing = True
        try:
            if self._pending is not None:
                await complete_native_journal_job(self._pending)
        finally:
            if self in ACTIVE_LEASES:
                original = self._original
                if (
                    not self._phase.released
                    or self._sqlite_checkout is not None
                    or self._last_outcome is CustodyNativeOutcome.UNKNOWN
                    or (
                        original is not None
                        and (
                            not original.resources_released()
                            or self._prepared is not None
                            or original.outcome
                            in (
                                CustodyNativeOutcome.COMMITTED,
                                CustodyNativeOutcome.UNKNOWN,
                            )
                        )
                    )
                ):
                    raise CustodyUnavailable
                self._open = False
                self._executor.shutdown(wait=True)
                self._ownership.close()
                ACTIVE_LEASES.remove(self)


@asynccontextmanager
async def journal_lease(
    root: CustodyNativeDirectory,
    *,
    ordinary: tuple[CustodyLifetimeLease, CustodyParticipantRegistration | None]
    | None = None,
    sqlite_accounting: tuple[Callable[[], None], Callable[[], Awaitable[None]]]
    | None = None,
) -> AsyncGenerator[CustodyJournalLease]:
    with ExitStack() as entry:
        try:
            if ordinary is not None:
                if ordinary[0].require_current() is not root:
                    raise CustodyUnavailable
                root.require_open_latch()
            ownership = root.lock("commit.lock")
            entry.callback(ownership.close)
            if ordinary is not None:
                root.require_open_latch()
        except (CustodyUnavailable, OSError):
            if ordinary is not None:
                custody_log(CUSTODY_REFUSED, "ordinary_admission")
            raise
        lease = CustodyJournalLease(root, ownership)
        entry.pop_all()
    charged = False

    async def close_original() -> None:
        await lease.close()
        if charged and sqlite_accounting is not None:
            await sqlite_accounting[1]()

    async def finish_deferred() -> None:
        try:
            await close_original()
        except (OSError, CustodyUnavailable):
            custody_log(CUSTODY_UNFINISHED, "ordinary_admission")

    def resume_close() -> None:
        asyncio.create_task(finish_deferred())

    try:
        if sqlite_accounting is not None:
            sqlite_accounting[0]()
            charged = True
        if ordinary is not None:
            lifetime, participant = ordinary
            if participant is not None:
                await lease.run(
                    lambda tx: tx.require_ordinary_checkout(lifetime, participant)
                )
            root.require_open_latch()
        yield lease
    finally:
        try:
            await complete_native_journal_job(close_original())
        except CustodyUnavailable:
            lease.after_journal_release(resume_close)
            raise
