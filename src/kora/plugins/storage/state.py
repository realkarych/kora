import os
import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Final, Literal, TypeAlias, TypeVar, cast

from kora.plugins.storage.errors import refuse, require_deadline
from kora.plugins.storage.files import FileLease, RootAnchor, RootManifest
from kora.plugins.storage.payload import PayloadFiles, PrivateFile
from kora.protocols.common import ProtocolContext
from kora.protocols.identity import MAX_COUNT
from kora.protocols.storage_models import (
    MAX_PAYLOAD_BYTES,
    ContentAddress,
    ContentRef,
    MutationObservation,
    MutationReceipt,
    MutationResult,
    NoEffectFailure,
    PrefixCapture,
    StageRef,
    StageSpec,
    StageWrite,
    StorageModel,
    StorageMutationSet,
    StorageOperationRef,
)

StorageConnection: TypeAlias = sqlite3.Connection
Settled = TypeVar("Settled", bound=MutationResult)

SCHEMA: Final[tuple[str, ...]] = (
    "CREATE TABLE root (singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch "
    "INTEGER NOT NULL CHECK(epoch>=0))",
    "CREATE TABLE stages (slot INTEGER PRIMARY KEY, stage_id TEXT NOT NULL UNIQUE "
    "COLLATE BINARY, state TEXT NOT NULL, reserved INTEGER NOT NULL "
    "CHECK(reserved>=0), spec TEXT)",
    "CREATE TABLE receipts (operation_id TEXT PRIMARY KEY COLLATE BINARY, owner_kind "
    "TEXT NOT NULL, owner_key TEXT NOT NULL COLLATE BINARY, semantic TEXT NOT NULL, "
    "operation TEXT NOT NULL, arguments TEXT NOT NULL, result TEXT, details TEXT, "
    "publication TEXT, address_digest TEXT, "
    "publication_ready INTEGER NOT NULL DEFAULT 0, "
    "UNIQUE(owner_kind, owner_key, semantic))",
    "CREATE INDEX receipt_owner ON receipts(owner_kind, owner_key, operation_id)",
    "CREATE INDEX receipt_address ON receipts(address_digest, semantic, result)",
    "CREATE TABLE contents (digest TEXT PRIMARY KEY COLLATE BINARY, reference TEXT "
    "NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL)",
    "CREATE TABLE deletions (slot INTEGER PRIMARY KEY, operation_id TEXT NOT NULL "
    "UNIQUE, request TEXT NOT NULL, target TEXT, device INTEGER, inode INTEGER, "
    "resolved INTEGER NOT NULL DEFAULT 0)",
    "INSERT INTO root(singleton, epoch) VALUES(1, 0)",
)


def row_values(value: object) -> tuple[object, ...] | None:
    if value is None:
        return None
    if not isinstance(value, tuple):
        refuse("storage_integrity")
    return cast("tuple[object, ...]", value)


def integer(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_COUNT:
        refuse("storage_integrity")
    return value


def text(value: object) -> str:
    if not isinstance(value, str):
        refuse("storage_integrity")
    return value


def rows(cursor: sqlite3.Cursor) -> Iterator[tuple[object, ...]]:
    for value in cursor.fetchall():
        row = row_values(value)
        if row is None:
            refuse("storage_integrity")
        yield row


def free_slot(connection: sqlite3.Connection, query: str, count: int) -> int | None:
    occupied = {integer(row[0]) for row in rows(connection.execute(query))}
    return next((index for index in range(count) if index not in occupied), None)


def operation_value(
    connection: sqlite3.Connection, query: str, operation: StorageOperationRef
) -> object:
    row = row_values(connection.execute(query, (operation.operation_id,)).fetchone())
    if row is None:
        refuse("storage_integrity")
    return row[0]


def unsettled_observation(
    operation: StorageOperationRef, state: Literal["pending", "evidence_unavailable"]
) -> MutationObservation:
    return MutationObservation(
        version=1, operation=operation, state=state, result=None, effects_closed=False
    )


def settled_result(
    saved: MutationObservation | None, kind: type[Settled]
) -> Settled | None:
    if saved is None or saved.state != "settled":
        return None
    if not isinstance(saved.result, kind):
        refuse("storage_conflict")
    return saved.result


class LocalState:
    def __init__(self, root: RootAnchor) -> None:
        self.root = root
        self.connections = 0
        self.initial_epoch: Final[int] = self.initialize()

    def validate_files(self, creating: bool) -> None:
        self.root.validate_anchor()
        for name in ("control.sqlite-wal", "control.sqlite-shm"):
            try:
                os.stat(name, dir_fd=self.root.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                refuse("storage_identity")
        flags = os.O_RDWR | (os.O_CREAT if creating else 0)
        descriptor = self.root.open_file(("control.sqlite",), flags)
        try:
            if (
                self.root.manifest.control_inode is not None
                and os.fstat(descriptor).st_ino != self.root.manifest.control_inode
            ):
                refuse("storage_identity")
        finally:
            os.close(descriptor)
        try:
            os.stat(
                "control.sqlite-journal", dir_fd=self.root.fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        os.close(self.root.open_file(("control.sqlite-journal",), os.O_RDWR))

    def connect(self, creating: bool) -> sqlite3.Connection:
        self.root.refresh_manifest()
        creating = creating and not self.root.manifest.control_initialized
        self.validate_files(creating)
        connection = sqlite3.connect(
            str(self.root.path / "control.sqlite"), timeout=0, isolation_level=None
        )
        try:
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA mmap_size=0")
            connection.execute("PRAGMA cache_size=-256")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA fullfsync=ON")
            if creating:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA auto_vacuum=FULL")
            journal = row_values(connection.execute("PRAGMA journal_mode").fetchone())
            vacuum = row_values(connection.execute("PRAGMA auto_vacuum").fetchone())
            if journal != ("delete",) or vacuum != (1,):
                refuse("storage_integrity")
        except BaseException:
            connection.close()
            raise
        self.connections += 1
        return connection

    @contextmanager
    def transaction(
        self, write: bool = False, creating: bool = False, synchronize: bool = True
    ) -> Generator[sqlite3.Connection]:
        gate = self.root.try_lock("root")
        if gate is None:
            refuse("storage_busy")
        with gate:
            connection: sqlite3.Connection | None = None
            recovery = False
            try:
                try:
                    os.stat(
                        "control.sqlite-journal",
                        dir_fd=self.root.fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    recovery = False
                else:
                    recovery = True
                connection = self.connect(creating)
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
                if (write or synchronize) and not recovery:
                    self.root.synchronize_directory(self.root.fd)
            except ValueError:
                refuse("storage_integrity")
            except sqlite3.Error as error:
                if connection is not None:
                    connection.rollback()
                code = (
                    error.sqlite_errorcode if hasattr(error, "sqlite_errorcode") else 0
                )
                if code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    refuse("storage_busy")
                refuse("storage_io")
            finally:
                try:
                    if connection is not None:
                        connection.close()
                        self.connections -= 1
                finally:
                    if recovery:
                        self.root.synchronize_directory(self.root.fd)

    def initialize(self) -> int:
        creating = not self.root.manifest.control_initialized
        with self.transaction(
            write=creating, creating=creating, synchronize=creating
        ) as connection:
            if creating:
                existing = row_values(
                    connection.execute(
                        "SELECT name FROM sqlite_schema LIMIT 1"
                    ).fetchone()
                )
                if existing is None:
                    for statement in SCHEMA:
                        connection.execute(statement)
            initial_epoch = self.epoch(connection)
        if creating:
            gate = self.root.try_lock("root")
            if gate is None:
                refuse("storage_busy")
            with gate:
                info = os.stat(
                    "control.sqlite", dir_fd=self.root.fd, follow_symlinks=False
                )
                updated = RootManifest.model_validate(
                    {
                        **self.root.manifest.model_dump(),
                        "control_initialized": True,
                        "control_inode": info.st_ino,
                    }
                )
                self.root.replace_manifest(updated)
        return initial_epoch

    def epoch(self, connection: sqlite3.Connection) -> int:
        row = row_values(
            connection.execute("SELECT epoch FROM root WHERE singleton=1").fetchone()
        )
        if row is None:
            refuse("storage_integrity")
        return integer(row[0])

    def admit_epoch(self, connection: sqlite3.Connection) -> None:
        current = self.epoch(connection)
        if self.initial_epoch != current or current == MAX_COUNT:
            refuse("storage_issuer_closed")

    def admit(self, connection: sqlite3.Connection, ctx: ProtocolContext) -> None:
        require_deadline(ctx)
        self.admit_epoch(connection)

    def advance_epoch(self, connection: sqlite3.Connection) -> None:
        current = self.epoch(connection)
        if current < MAX_COUNT:
            connection.execute(
                "UPDATE root SET epoch=? WHERE singleton=1", (current + 1,)
            )

    def stage(
        self, connection: sqlite3.Connection, stage: StageRef
    ) -> tuple[int, str, int, StageSpec | None] | None:
        if stage.storage_identity != self.root.identity:
            refuse("storage_conflict")
        row = row_values(
            connection.execute(
                "SELECT slot, state, reserved, spec FROM stages WHERE stage_id=?",
                (stage.stage_id,),
            ).fetchone()
        )
        if row is None:
            return None
        return (
            integer(row[0]),
            text(row[1]),
            integer(row[2]),
            None if row[3] is None else StageSpec.model_validate_json(text(row[3])),
        )

    def allocate_stage(
        self, connection: sqlite3.Connection, stage: StageRef, spec: StageSpec | None
    ) -> int:
        slot = free_slot(
            connection, "SELECT slot FROM stages", self.root.manifest.limits.stage_slots
        )
        total = row_values(
            connection.execute(
                "SELECT COALESCE(SUM(reserved), 0) FROM stages"
            ).fetchone()
        )
        if total is None:
            refuse("storage_integrity")
        reserved = 0 if spec is None else 2 * min(spec.total_size, MAX_PAYLOAD_BYTES)
        if (
            slot is None
            or reserved + integer(total[0]) > self.root.manifest.limits.private_bytes
        ):
            refuse("storage_capacity")
        connection.execute(
            "INSERT INTO stages(slot, stage_id, state, reserved, spec) VALUES(?, ?, "
            "?, ?, ?)",
            (
                slot,
                stage.stage_id,
                "retiring" if spec is None else "open",
                reserved,
                None if spec is None else spec.model_dump_json(),
            ),
        )
        return slot

    def receipt(
        self, connection: sqlite3.Connection, operation: StorageOperationRef
    ) -> MutationObservation | None:
        row = row_values(
            connection.execute(
                "SELECT operation, result FROM receipts WHERE operation_id=?",
                (operation.operation_id,),
            ).fetchone()
        )
        if row is None:
            return None
        saved = StorageOperationRef.model_validate_json(text(row[0]))
        if saved != operation:
            refuse("storage_conflict")
        if row[1] is None:
            return unsettled_observation(operation, "pending")
        return MutationObservation.model_validate_json(text(row[1]))

    def settled_receipt(
        self,
        connection: sqlite3.Connection,
        operation: StorageOperationRef,
        kind: type[Settled],
    ) -> Settled | None:
        return settled_result(self.receipt(connection, operation), kind)

    def register(
        self,
        connection: sqlite3.Connection,
        operation: StorageOperationRef,
        arguments: StorageModel,
        owner_kind: str,
        owner_key: str,
        semantic: str,
    ) -> None:
        existing = row_values(
            connection.execute(
                "SELECT operation_id FROM receipts WHERE owner_kind=? AND owner_key=? "
                "AND semantic=?",
                (owner_kind, owner_key, semantic),
            ).fetchone()
        )
        if existing is not None:
            if text(existing[0]) != operation.operation_id:
                refuse("storage_conflict")
            return
        connection.execute(
            "INSERT INTO receipts(operation_id, owner_kind, owner_key, semantic, "
            "operation, arguments) VALUES(?, ?, ?, ?, ?, ?)",
            (
                operation.operation_id,
                owner_kind,
                owner_key,
                semantic,
                operation.model_dump_json(),
                arguments.model_dump_json(),
            ),
        )

    def settle(
        self,
        connection: sqlite3.Connection,
        operation: StorageOperationRef,
        result: MutationResult,
    ) -> None:
        observation = MutationObservation(
            version=1,
            operation=operation,
            state="settled",
            result=result,
            effects_closed=True,
        )
        connection.execute(
            "UPDATE receipts SET result=? WHERE operation_id=?",
            (observation.model_dump_json(), operation.operation_id),
        )

    def abandon(
        self, connection: sqlite3.Connection, operation: StorageOperationRef
    ) -> None:
        self.settle(
            connection,
            operation,
            NoEffectFailure(
                version=1, operation=operation, code="storage_issuer_closed"
            ),
        )

    def file_identity(
        self, connection: sqlite3.Connection, operation: StorageOperationRef
    ) -> PrivateFile | None:
        value = operation_value(
            connection, "SELECT details FROM receipts WHERE operation_id=?", operation
        )
        return None if value is None else PrivateFile.model_validate_json(text(value))

    def ensure_file_identity(
        self, connection: sqlite3.Connection, operation: StorageOperationRef
    ) -> PrivateFile:
        identity = self.file_identity(connection, operation)
        if identity is None:
            identity = PrivateFile(
                version=1,
                private_identity=self.root.new_identity(),
                device=self.root.manifest.device,
                inode=None,
            )
            self.save_file_identity(connection, operation, identity)
        return identity

    def save_file_identity(
        self,
        connection: sqlite3.Connection,
        operation: StorageOperationRef,
        identity: PrivateFile,
    ) -> None:
        connection.execute(
            "UPDATE receipts SET details=? WHERE operation_id=?",
            (identity.model_dump_json(), operation.operation_id),
        )

    def open_writer(
        self,
        slot: int,
        semantic: str,
        operation: StorageOperationRef,
        expected: PrivateFile,
    ) -> tuple[FileLease, PrivateFile]:
        files = PayloadFiles(self.root)
        writer = files.create(slot, semantic, expected)
        try:
            identity = files.identity(writer, expected.private_identity)
            with self.transaction(write=True) as connection:
                self.save_file_identity(connection, operation, identity)
            os.ftruncate(writer.fd, 0)
            return writer, identity
        except BaseException:
            writer.close()
            raise

    def stage_observations(
        self, connection: sqlite3.Connection, stage: StageRef
    ) -> tuple[tuple[str, MutationObservation, PrivateFile | None], ...]:
        records: list[tuple[str, MutationObservation, PrivateFile | None]] = []
        for row in rows(
            connection.execute(
                "SELECT semantic, operation, result, details FROM receipts WHERE "
                "owner_kind='stage' AND owner_key=? ORDER BY semantic",
                (stage.stage_id,),
            )
        ):
            operation = StorageOperationRef.model_validate_json(text(row[1]))
            observation = (
                unsettled_observation(operation, "pending")
                if row[2] is None
                else MutationObservation.model_validate_json(text(row[2]))
            )
            records.append(
                (
                    text(row[0]),
                    observation,
                    None
                    if row[3] is None
                    else PrivateFile.model_validate_json(text(row[3])),
                )
            )
        return tuple(records)

    def retained_stage_bytes(
        self, connection: sqlite3.Connection, stage: StageRef
    ) -> int:
        retained = 0
        for row in rows(
            connection.execute(
                "SELECT semantic, arguments FROM receipts WHERE owner_kind='stage' "
                "AND owner_key=? AND (semantic LIKE 'part-%' OR semantic='prefix')",
                (stage.stage_id,),
            )
        ):
            if text(row[0]) == "prefix":
                retained += PrefixCapture.model_validate_json(text(row[1])).count
            else:
                retained += StageWrite.model_validate_json(text(row[1])).spec.size
        return retained

    def reserve_stage(
        self, connection: sqlite3.Connection, stage: StageRef, reserved: int
    ) -> None:
        other = row_values(
            connection.execute(
                "SELECT COALESCE(SUM(reserved), 0) FROM stages WHERE stage_id<>?",
                (stage.stage_id,),
            ).fetchone()
        )
        if other is None:
            refuse("storage_integrity")
        if reserved + integer(other[0]) > self.root.manifest.limits.private_bytes:
            refuse("storage_capacity")
        connection.execute(
            "UPDATE stages SET reserved=? WHERE stage_id=?", (reserved, stage.stage_id)
        )

    def content(
        self, connection: sqlite3.Connection, address: ContentAddress
    ) -> tuple[ContentRef, PrivateFile] | None:
        if address.storage_identity != self.root.identity:
            refuse("storage_conflict")
        row = row_values(
            connection.execute(
                "SELECT reference, device, inode FROM contents WHERE digest=?",
                (address.payload_digest,),
            ).fetchone()
        )
        if row is None:
            return None
        reference = ContentRef.model_validate_json(text(row[0]))
        if reference.address != address:
            refuse("storage_integrity")
        return reference, PrivateFile(
            version=1,
            private_identity=reference.object_incarnation,
            device=integer(row[1]),
            inode=integer(row[2]),
        )

    def publication(
        self, connection: sqlite3.Connection, operation: StorageOperationRef
    ) -> ContentRef | None:
        value = operation_value(
            connection,
            "SELECT publication FROM receipts WHERE operation_id=?",
            operation,
        )
        return None if value is None else ContentRef.model_validate_json(text(value))

    def save_publication(
        self,
        connection: sqlite3.Connection,
        operation: StorageOperationRef,
        reference: ContentRef,
    ) -> None:
        connection.execute(
            "UPDATE receipts SET publication=?, address_digest=? WHERE operation_id=?",
            (
                reference.model_dump_json(),
                reference.address.payload_digest,
                operation.operation_id,
            ),
        )

    def mutation_set(
        self, connection: sqlite3.Connection, stage: StageRef
    ) -> StorageMutationSet:
        receipts: list[MutationReceipt] = []
        for value in connection.execute(
            "SELECT operation, result FROM receipts WHERE owner_kind='stage' AND "
            "owner_key=? ORDER BY operation_id",
            (stage.stage_id,),
        ).fetchall():
            row = row_values(value)
            if row is None or row[1] is None:
                refuse("storage_busy")
            observation = MutationObservation.model_validate_json(text(row[1]))
            if observation.result is None:
                refuse("storage_integrity")
            operation = observation.operation
            receipts.append(
                MutationReceipt(
                    version=1,
                    operation_id=operation.operation_id,
                    kind=operation.kind,
                    request_digest=operation.request_digest,
                    outcome_digest=observation.result.metadata_digest(),
                )
            )
        return StorageMutationSet(version=1, stage=stage, receipts=tuple(receipts))
