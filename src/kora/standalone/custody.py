import os

from kora.db.connection import (
    Database,
    DatabaseSettings,
    LocalSQLiteWriterEntry,
)
from kora.db.custody_models import (
    CustodyParticipantRegistration,
    CustodyProcessIdentity,
    CustodyUnavailable,
)
from kora.db.custody_native import (
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    register_custody_fork,
)
from kora.db.custody_writer import LocalSQLiteWriterScope
from kora.plugins.storage.native import filesystem, synchronize_file, validate_acl
from kora.protocols.errors import KoraProtocolError
from kora.runner.identity import IdentityState, NativeIdentityReader


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
