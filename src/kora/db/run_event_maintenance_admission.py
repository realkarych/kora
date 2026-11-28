import fcntl
import json
import os
import stat
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import Connection, NullPool, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from kora.db.run_event_position_schema import postgres_shape_fingerprint
from kora.db.run_event_position_v1 import (
    RunEventPositionRejected as PositionMaintenanceRefused,
)
from kora.ir.vcs_ref_json import unique_object
from kora.sqlite.driver import Admission as Admission
from kora.sqlite.driver import Connection as NativeConnection
from kora.sqlite.driver import Error as NativeSQLiteError
from kora.sqlite.driver import NativeRetirement

PREDECESSOR_REVISION: str = "run_event_metadata_0034"
PROFILE_PATH: Path = Path(
    "/private/etc/kora/run-event-maintenance.json"
    if sys.platform == "darwin"
    else "/etc/kora/run-event-maintenance.json"
)
EVIDENCE_BYTES: int = 8192
POSITION_AUTHORITY: str = "kora_position_migration_authority"
POSITION_VERIFYING: str = "kora_position_verifying_migration"
POSITION_TARGET: str = "kora_position_migration_target"
CONTROL_LOCK: str = "prepare.lock"
RELEASE_EVIDENCE: str = "release.json"
PROTECTED_OPEN: int = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
RequireACL = Callable[[int], None]
POSTGRESQL_FRESH_OWNER = text("""
    SELECT d.oid, r.oid, pg_catalog.pg_backend_pid(),
           pg_catalog.pg_postmaster_start_time()
    FROM pg_catalog.pg_database d
    JOIN pg_catalog.pg_roles r ON r.rolname = SESSION_USER
    WHERE d.datname = pg_catalog.current_database() AND d.datconnlimit = 1
      AND SESSION_USER = CURRENT_USER AND r.rolcanlogin AND NOT r.rolsuper
      AND NOT r.rolreplication AND NOT pg_catalog.pg_is_in_recovery()
      AND pg_catalog.current_schemas(false) = ARRAY['public']::pg_catalog.name[]
      AND pg_catalog.current_setting('transaction_read_only') = 'off'
      AND pg_catalog.current_setting('session_replication_role') = 'origin'
    """)
POSTGRESQL_FRESH_EMPTY = text("""
    SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE oid >= 16384)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid >= 16384)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend
                        WHERE dbid = (SELECT oid FROM pg_catalog.pg_database
                                       WHERE datname = pg_catalog.current_database())
                          AND objid >= 16384)
    """)
POSTGRESQL_QUIET = text("""
    SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity
                       WHERE datname = pg_catalog.current_database()
                         AND pid <> pg_catalog.pg_backend_pid())
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_prepared_xacts
                       WHERE database = pg_catalog.current_database())
    """)
POSTGRESQL_INVENTORY_BOUND = text("""
    SELECT count(*) = 5 AND sum(setting::int) <= 2048 FROM pg_catalog.pg_settings
    WHERE name IN ('max_connections', 'max_prepared_transactions', 'max_wal_senders',
                   'max_worker_processes', 'autovacuum_max_workers')
    """)
POSTGRESQL_MANAGED_OWNER = text("""
    SELECT (pg_catalog.pg_control_system()).system_identifier = :system_identifier
           AND (pg_catalog.pg_control_checkpoint()).timeline_id = :timeline
           AND
           pg_catalog.to_char(pg_catalog.pg_postmaster_start_time() AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') = :primary_generation
    FROM pg_catalog.pg_database d
    JOIN pg_catalog.pg_roles s ON s.rolname = SESSION_USER
    JOIN pg_catalog.pg_roles o ON o.rolname = CURRENT_USER
    JOIN pg_catalog.pg_roles old ON old.rolname = :original_login
    JOIN pg_catalog.pg_roles next ON next.rolname = :successor_login
    WHERE d.datname = pg_catalog.current_database() AND d.datname = :catalog
      AND d.oid = :oid
      AND s.rolname = :maintenance_login AND o.rolname = :catalog_owner
      AND s.rolcanlogin AND NOT s.rolsuper AND NOT s.rolreplication
      AND NOT o.rolcanlogin AND NOT o.rolsuper AND NOT o.rolreplication
      AND NOT old.rolcanlogin AND NOT next.rolcanlogin AND old.oid <> next.oid
      AND o.oid NOT IN (old.oid, next.oid) AND d.datdba = o.oid
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relowner = o.oid
                  AND oid = pg_catalog.to_regclass('public.event'))
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members
                      WHERE roleid IN (old.oid, next.oid))
      AND NOT pg_catalog.pg_is_in_recovery() AND pg_catalog.pg_my_temp_schema() = 0
      AND pg_catalog.current_setting('transaction_read_only') = 'off'
      AND pg_catalog.current_setting('session_replication_role') = 'origin'
      AND pg_catalog.current_schemas(false) = ARRAY['public']::pg_catalog.name[]
    """)
SQLITE_FRESH_EMPTY = text("""
    SELECT NOT EXISTS (SELECT 1 FROM main.sqlite_schema)
       AND NOT EXISTS (SELECT 1 FROM temp.sqlite_schema)
    """)

PREDECESSOR_TABLES = tuple(
    (
        "alembic_version attempt attempt_dispatcher attempt_first_claim_basis "
        "event graph_version namespace node_instance run worker"
    ).split()
)
SQLITE_PREDECESSOR_SCHEMA = text("""
    SELECT type, CASE WHEN octet_length(name) <= 512 THEN name ELSE '~' END, tbl_name,
        CASE WHEN sql IS NULL THEN NULL WHEN typeof(sql) = 'text'
          AND octet_length(sql) <= 65536 THEN sql ELSE '~' END
    FROM main.sqlite_schema WHERE lower(tbl_name) IN :tables
      AND NOT EXISTS (SELECT 1 FROM temp.sqlite_schema)
      AND NOT EXISTS (SELECT 1 FROM main.sqlite_schema
                      WHERE lower(name) = 'custody_frontier')
    ORDER BY type, name COLLATE BINARY LIMIT 57
    """).bindparams(bindparam("tables", expanding=True))
SQLITE_PREDECESSOR_SIGNATURE = (
    "090467c26fdfba77dcfbf25195988d34d9642d960ce7c557776ef39a69a9bbd6"
)
POSTGRESQL_PREDECESSOR_SIGNATURE = (
    "8e8eff29919d7cd3955215da3d9fafca0bf19d80b0a8efee10c4579d4f097927"
)

Operation = Annotated[str, StringConstraints(pattern=r"\A[0-9a-f]{32}\z")]
Digest = Annotated[str, StringConstraints(pattern=r"\A[0-9a-f]{64}\z")]
AbsolutePath = Annotated[str, StringConstraints(min_length=2, max_length=4096)]
HostUser = Annotated[int, Field(ge=0, lt=2**32 - 1)]
NativeIdentifier = Annotated[int, Field(ge=0, lt=2**64)]
CatalogName = Annotated[str, StringConstraints(pattern=r"\A[a-z_][a-z0-9_]{0,62}\z")]
Timestamp = Annotated[str, StringConstraints(min_length=20, max_length=36)]


def earlier_position_target(target: str) -> bool:
    numbered = len(target) == 4 and target.isascii() and target.isdecimal()
    return (
        target in {"base", PREDECESSOR_REVISION}
        or numbered
        and "0001" <= target <= "0033"
    )


@dataclass(frozen=True)
class PositionMigrationAuthority:
    owner: Connection
    verify: Callable[[Connection], None]
    binding: tuple[str, str] | None


def position_migration_authority(connection: Connection) -> tuple[str, str] | None:
    authority: object = connection.info.get(POSITION_AUTHORITY)
    if (
        not isinstance(authority, PositionMigrationAuthority)
        or authority.owner is not connection
    ):
        raise PositionMaintenanceRefused
    connection.info[POSITION_VERIFYING] = True
    try:
        authority.verify(connection)
    finally:
        connection.info.pop(POSITION_VERIFYING, None)
    return authority.binding


def fresh_position_authority(connection: Connection) -> PositionMigrationAuthority:
    if connection.dialect.name == "sqlite":
        original = connection.get_transaction()
        if original is None or connection.scalar(SQLITE_FRESH_EMPTY) not in (1, True):
            raise PositionMaintenanceRefused

        def verify(owner: Connection) -> None:
            if not original.is_active or owner.get_transaction() is not original:
                raise PositionMaintenanceRefused
    else:
        with connection.execute(POSTGRESQL_FRESH_OWNER) as result:
            observed = result.one_or_none()
        if (
            observed is None
            or not connection.scalar(POSTGRESQL_FRESH_EMPTY)
            or not connection.scalar(POSTGRESQL_QUIET)
        ):
            raise PositionMaintenanceRefused

        def verify(owner: Connection) -> None:
            with owner.execute(POSTGRESQL_FRESH_OWNER) as rows:
                if rows.one_or_none() != observed:
                    raise PositionMaintenanceRefused

    return PositionMigrationAuthority(connection, verify, None)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MaintenanceProfile(Evidence):
    format: Literal["kora.run-event-maintenance-profile/1"]
    installation: Operation
    control_directory: AbsolutePath
    control_device: NativeIdentifier
    control_inode: NativeIdentifier
    operator_uid: HostUser
    successor_uid: HostUser


class SQLiteIdentity(Evidence):
    backend: Literal["sqlite"]
    path: AbsolutePath
    device: NativeIdentifier
    inode: NativeIdentifier
    old_uid: HostUser


class PostgreSQLIdentity(Evidence):
    backend: Literal["postgresql"]
    catalog: CatalogName
    oid: NativeIdentifier
    system_identifier: NativeIdentifier
    primary_generation: Timestamp
    timeline: NativeIdentifier
    maintenance_login: CatalogName
    original_login: CatalogName
    successor_login: CatalogName
    catalog_owner: CatalogName


def require_postgresql_fence(
    connection: Connection,
    identity: PostgreSQLIdentity,
    *,
    inventory: bool = False,
) -> None:
    if inventory:
        connection.execute(text("SET LOCAL statement_timeout = '5s'")).close()
    if not connection.scalar(POSTGRESQL_MANAGED_OWNER, identity.model_dump()):
        raise PositionMaintenanceRefused
    if inventory:
        if not connection.scalar(POSTGRESQL_INVENTORY_BOUND) or not connection.scalar(
            POSTGRESQL_QUIET
        ):
            raise PositionMaintenanceRefused


class RestoreReceipt(Evidence):
    verified_image: Digest
    isolated_verification: Digest


class MaintenanceProof(Evidence):
    format: Literal["kora.run-event-maintenance-proof/1"]
    operation: Operation
    phase: Literal["held", "released"]
    installation: Operation
    fence_generation: Operation
    predecessor: Literal["run_event_metadata_0034"]
    successor: Literal["run_event_position_0035"]
    predecessor_artifact: Digest
    successor_artifact: Digest
    original_cohort: Digest
    restore: RestoreReceipt
    database: Annotated[
        SQLiteIdentity | PostgreSQLIdentity, Field(discriminator="backend")
    ]

    def binding(self) -> tuple[str, str]:
        return self.operation, sha256(
            self.model_dump_json(exclude={"phase"}).encode()
        ).hexdigest()


EvidenceType = TypeVar("EvidenceType", bound=Evidence)
Observation = TypeVar("Observation")


def sqlite_has_sidecars(path: Path) -> bool:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def sqlite_main_admit(path: Path) -> Admission:
    named = path.lstat()

    def admit(descriptor: int, empty: int) -> bool:
        opened = os.fstat(descriptor)
        return (
            not empty
            and stat.S_ISREG(named.st_mode)
            and opened.st_nlink == 1
            and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        )

    return admit


def sqlite_snapshot(
    path: Path,
    admit: Admission,
    inspect: Callable[[Connection], Observation],
    *,
    owner: NativeConnection | None = None,
) -> Observation:
    resources: list[NativeConnection] = []
    try:
        canonical = path.resolve(strict=True)
        with ExitStack() as stack:
            immutable = owner is not None or not sqlite_has_sidecars(path)
            if owner is None and immutable:
                guard = stack.enter_context(closing(NativeConnection()))
                resources.append(guard)
                guard.open(canonical, False, 0, admit, snapshot="exclusive")
                if sqlite_has_sidecars(path):
                    raise PositionMaintenanceRefused
            native = stack.enter_context(closing(NativeConnection()))
            resources.append(native)
            native.open(
                canonical, True, 0, admit, snapshot="immutable" if immutable else "live"
            )
            engine = create_engine(
                "sqlite://", creator=lambda: native, poolclass=NullPool
            )
            with engine.connect() as connection:
                connection.execute(text("BEGIN")).close()
                return inspect(connection)
    except (OSError, NativeSQLiteError, SQLAlchemyError):
        raise PositionMaintenanceRefused from None
    finally:
        unsettled = [resource for resource in resources if not resource.settled()]
        for resource in unsettled:
            NativeRetirement.retain(path, resource, lambda: None)
        if unsettled and owner is None:
            raise PositionMaintenanceRefused from None


def event_constraint_order(line: str) -> str:
    return line if line.lstrip().startswith("CONSTRAINT fk_event_") else ""


def predecessor_shape(connection: Connection) -> bool:
    if connection.dialect.name == "postgresql":
        return (
            postgres_shape_fingerprint(connection, PREDECESSOR_TABLES)
            == POSTGRESQL_PREDECESSOR_SIGNATURE
        )
    vectors: list[tuple[object, ...]] = []
    with connection.execute(
        SQLITE_PREDECESSOR_SCHEMA, {"tables": PREDECESSOR_TABLES}
    ) as rows:
        for kind, name, table, ddl in rows:
            if name == "event" and isinstance(ddl, str):
                ddl = "\n".join(sorted(ddl.splitlines(), key=event_constraint_order))
            vectors.append((kind, name, table, ddl))
    payload = json.dumps(vectors, ensure_ascii=True, separators=(",", ":")).encode()
    return sha256(payload).hexdigest() == SQLITE_PREDECESSOR_SIGNATURE


def parse_evidence(value: bytes, model: type[EvidenceType]) -> EvidenceType:
    if not 0 < len(value) <= EVIDENCE_BYTES:
        raise PositionMaintenanceRefused
    try:
        decoded: object = json.loads(
            value.decode("utf-8"), object_pairs_hook=unique_object
        )
        return model.model_validate(decoded)
    except (ValueError, RecursionError):
        raise PositionMaintenanceRefused from None


def protected_open(
    stack: ExitStack,
    path: str | Path,
    owner: int,
    acl: RequireACL,
    parent: int | None = None,
    flags: int = os.O_RDONLY,
) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, flags | PROTECTED_OPEN, dir_fd=parent)
    stack.callback(os.close, descriptor)
    native = os.fstat(descriptor)
    expected_type = stat.S_ISDIR if flags & os.O_DIRECTORY else stat.S_ISREG
    protected = expected_type(native.st_mode) and not native.st_mode & 0o022
    if native.st_uid != owner or not protected:
        raise PositionMaintenanceRefused
    if not flags & os.O_DIRECTORY and native.st_nlink != 1:
        raise PositionMaintenanceRefused
    acl(descriptor)
    return descriptor, native


def protected_directory(
    stack: ExitStack, path: Path, owner: int, acl: RequireACL
) -> tuple[int, os.stat_result]:
    if path.anchor != "/" or len(path.parts) < 2 or ".." in path.parts:
        raise PositionMaintenanceRefused
    with ExitStack() as parents:
        native = None
        for component in path.parts[:-1]:
            native, _ = protected_open(
                parents, component, 0, acl, native, os.O_DIRECTORY
            )
        return protected_open(stack, path.name, owner, acl, native, os.O_DIRECTORY)


def control_evidence(
    root: int, name: str, owner: int, acl: RequireACL, model: type[EvidenceType]
) -> EvidenceType:
    with ExitStack() as stack:
        descriptor, native = protected_open(stack, name, owner, acl, root)
        data = os.read(descriptor, EVIDENCE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(data) != native.st_size or after.st_ctime_ns != native.st_ctime_ns:
            raise PositionMaintenanceRefused
        return parse_evidence(data, model)


@contextmanager
def position_control(acl: RequireACL) -> Generator[tuple[MaintenanceProfile, int]]:
    try:
        with ExitStack() as stack:
            config, _ = protected_directory(stack, PROFILE_PATH.parent, 0, acl)
            profile = control_evidence(
                config, PROFILE_PATH.name, 0, acl, MaintenanceProfile
            )
            directory, root = protected_directory(
                stack, Path(profile.control_directory), profile.operator_uid, acl
            )
            if (root.st_dev, root.st_ino) != (
                profile.control_device,
                profile.control_inode,
            ) or profile.successor_uid in {0, profile.operator_uid}:
                raise PositionMaintenanceRefused
            yield profile, directory
    except (OSError, NativeSQLiteError, SQLAlchemyError):
        raise PositionMaintenanceRefused from None


def require_unreleased(directory: int) -> None:
    try:
        os.stat(RELEASE_EVIDENCE, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise PositionMaintenanceRefused


def installed_artifact_identity() -> str:
    try:
        record = distribution("kora").read_text("RECORD")
    except (OSError, PackageNotFoundError, UnicodeError):
        raise PositionMaintenanceRefused from None
    if not record:
        raise PositionMaintenanceRefused
    return sha256(record.encode()).hexdigest()


def qualified_sqlite_main(
    path: Path, identity: SQLiteIdentity, acl: RequireACL
) -> Admission:
    owner = os.geteuid()
    if path != Path(identity.path):
        raise PositionMaintenanceRefused

    def admit(descriptor: int, empty: int) -> bool:
        opened = os.fstat(descriptor)
        anchor = opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink
        if empty or anchor != (identity.device, identity.inode, owner, 1):
            return False
        if opened.st_mode != stat.S_IFREG | 0o600:
            return False
        acl(descriptor)
        return os.pread(descriptor, 2, 18) == b"\x02\x02"

    with ExitStack() as stack:
        _, parent = protected_directory(stack, path.parent, owner, acl)
        if stat.S_IMODE(parent.st_mode) != 0o700:
            raise PositionMaintenanceRefused
    return admit


def require_position_release(
    binding: tuple[str, str],
    path: Path,
    acl: RequireACL,
) -> Admission:
    with position_control(acl) as (profile, directory):
        proof = control_evidence(
            directory, RELEASE_EVIDENCE, profile.operator_uid, acl, MaintenanceProof
        )
        identity = proof.database
        if (
            proof.phase != "released"
            or proof.installation != profile.installation
            or profile.successor_uid != os.geteuid()
            or proof.binding() != binding
            or proof.successor_artifact != installed_artifact_identity()
            or not isinstance(identity, SQLiteIdentity)
        ):
            raise PositionMaintenanceRefused
        return qualified_sqlite_main(path, identity, acl)


@contextmanager
def hold_position_control(
    path: Path,
    acl: RequireACL,
) -> Generator[tuple[MaintenanceProof, Callable[[], None]]]:
    with position_control(acl) as (profile, directory), ExitStack() as stack:
        if (
            os.geteuid() != profile.operator_uid
            or path.parent != Path(profile.control_directory)
            or path.name in {CONTROL_LOCK, RELEASE_EVIDENCE}
        ):
            raise PositionMaintenanceRefused
        lock, owner = protected_open(
            stack, CONTROL_LOCK, profile.operator_uid, acl, directory
        )
        if stat.S_IMODE(owner.st_mode) != 0o600:
            raise PositionMaintenanceRefused
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_unreleased(directory)
        proof = control_evidence(
            directory, path.name, profile.operator_uid, acl, MaintenanceProof
        )
        identity = proof.database
        privileged = {0, profile.operator_uid, profile.successor_uid}
        if isinstance(identity, SQLiteIdentity) and identity.old_uid in privileged:
            raise PositionMaintenanceRefused
        if (
            proof.phase != "held"
            or proof.installation != profile.installation
            or proof.successor_artifact != installed_artifact_identity()
        ):
            raise PositionMaintenanceRefused

        def verify() -> None:
            named = os.stat(CONTROL_LOCK, dir_fd=directory, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (owner.st_dev, owner.st_ino):
                raise PositionMaintenanceRefused
            with position_control(acl) as (current, root):
                require_unreleased(root)
                if (
                    current != profile
                    or control_evidence(
                        root, path.name, profile.operator_uid, acl, MaintenanceProof
                    )
                    != proof
                ):
                    raise PositionMaintenanceRefused

        verify()
        yield proof, verify
