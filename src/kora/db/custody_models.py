from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MAX_CUSTODY_COUNTER: Final[int] = 2**63 - 1
MAX_NATIVE_IDENTIFIER: Final[int] = 2**64 - 1
MAX_CUSTODY_ROW_BYTES: Final[int] = 16_384
MAX_CUSTODY_PARTICIPANTS: Final[int] = 64
ZERO_CUSTODY_ID: Final[bytes] = bytes(16)


class CustodyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("execution custody is unavailable")


class CustodyMode(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    FENCED = "fenced"
    RESTORING = "restoring"


class CustodyCommitPhase(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    COMMITTED = "committed"
    ABORTED = "aborted"


class CustodyIssuerKind(StrEnum):
    ORDINARY = "ordinary"
    RECOVERY = "recovery"
    PROVISIONING = "provisioning"


class CustodyGate(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class CustodyNativeOutcome(StrEnum):
    COMMITTED = "committed"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


def custody_counter(value: object, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= MAX_CUSTODY_COUNTER:
        raise CustodyUnavailable
    return value


def custody_identity(value: object, *, allow_zero: bool = False) -> bytes:
    if type(value) is not bytes or len(value) != 16:
        raise CustodyUnavailable
    if not allow_zero and value == ZERO_CUSTODY_ID:
        raise CustodyUnavailable
    return value


def custody_digest(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise CustodyUnavailable
    return value


def native_identifier(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 8:
        raise CustodyUnavailable
    return value


def encode_native_identifier(value: int) -> bytes:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_IDENTIFIER:
        raise CustodyUnavailable
    return value.to_bytes(8, "big")


def custody_ascii(value: object, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.isascii()
        or not value.isprintable()
    ):
        raise CustodyUnavailable
    return value


@dataclass(frozen=True)
class CustodyProcessIdentity:
    host_id: str
    boot_id: str
    pid_namespace: str
    pid: int
    start_time: int

    def __post_init__(self) -> None:
        for identifier in (self.host_id, self.boot_id, self.pid_namespace):
            if " " in custody_ascii(identifier):
                raise CustodyUnavailable
        if custody_counter(self.pid) > 2**31 - 1:
            raise CustodyUnavailable
        custody_counter(self.start_time, allow_zero=True)


@dataclass(frozen=True)
class CustodyFileAnchor:
    device: bytes
    inode: bytes
    canonical_path: bytes

    def __post_init__(self) -> None:
        native_identifier(self.device)
        native_identifier(self.inode)
        if (
            type(self.canonical_path) is not bytes
            or not 1 <= len(self.canonical_path) <= 4096
            or b"\x00" in self.canonical_path
        ):
            raise CustodyUnavailable


@dataclass(frozen=True)
class CustodyFrontier:
    generation: int
    sequence: int
    token: bytes

    def __post_init__(self) -> None:
        custody_counter(self.generation, allow_zero=True)
        custody_counter(self.sequence, allow_zero=True)
        custody_identity(self.token, allow_zero=True)
        empty = self.generation == 0 and self.sequence == 0
        if empty != (self.token == ZERO_CUSTODY_ID):
            raise CustodyUnavailable
        if not empty and (self.generation == 0 or self.sequence == 0):
            raise CustodyUnavailable

    @property
    def is_initial(self) -> bool:
        return self.generation == 0


@dataclass(frozen=True)
class CustodyPostgresTransaction:
    system_identifier: bytes
    database_oid: int
    xid: bytes
    backend_pid: int
    backend_start_us: int
    server_start_us: int

    def __post_init__(self) -> None:
        native_identifier(self.system_identifier)
        native_identifier(self.xid)
        if custody_counter(self.database_oid) > 2**32 - 1:
            raise CustodyUnavailable
        if custody_counter(self.backend_pid) > 2**31 - 1:
            raise CustodyUnavailable
        for instant in (self.backend_start_us, self.server_start_us):
            if type(instant) is not int or not -(2**63) <= instant < 2**63:
                raise CustodyUnavailable


@dataclass(frozen=True)
class CustodyTransactionBinding:
    metadata_id: bytes
    issuer_sequence: int
    connection_sequence: int
    transaction_sequence: int
    settings_digest: bytes
    postgres: CustodyPostgresTransaction | None

    def __post_init__(self) -> None:
        custody_identity(self.metadata_id)
        custody_counter(self.issuer_sequence)
        custody_counter(self.connection_sequence)
        custody_counter(self.transaction_sequence)
        custody_digest(self.settings_digest)


@dataclass(frozen=True)
class CustodyPendingCommit:
    journal_id: bytes
    issuer_kind: CustodyIssuerKind
    frontier: CustodyFrontier
    previous: CustodyFrontier
    transaction: CustodyTransactionBinding
    phase: CustodyCommitPhase

    def __post_init__(self) -> None:
        custody_identity(self.journal_id)
        if (
            self.frontier.is_initial
            or self.frontier.generation < self.previous.generation
            or self.frontier.sequence <= self.previous.sequence
            or self.frontier.token == self.previous.token
        ):
            raise CustodyUnavailable
