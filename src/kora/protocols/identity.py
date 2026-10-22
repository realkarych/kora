from typing import Annotated, Final, Protocol

from pydantic import Field, StrictBool, StrictInt, StringConstraints

from kora.protocols.common import BoundaryModel

Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[\x21-\x7e]{1,256}$")
]
MAX_PID: Final[int] = 2**31 - 1
MAX_COUNT: Final[int] = 2**63 - 1
Pid = Annotated[StrictInt, Field(ge=1, le=MAX_PID)]
Count = Annotated[StrictInt, Field(ge=0, le=MAX_COUNT)]


class ProcessIdentity(BoundaryModel):
    host_id: Identifier | None
    boot_id: Identifier | None
    pid_namespace: Identifier | None
    pid: Pid
    start_time: Count
    leads_group: StrictBool

    def identity_key(self) -> tuple[str | None, str | None, str | None, int, int]:
        return (
            self.host_id,
            self.boot_id,
            self.pid_namespace,
            self.pid,
            self.start_time,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProcessIdentity):
            return NotImplemented
        return self.identity_key() == other.identity_key()

    def __hash__(self) -> int:
        return hash(self.identity_key())


class ProcessIdentitySource(Protocol):
    def current(self) -> ProcessIdentity: ...


class GrantBinding(BoundaryModel):
    dispatcher_id: Identifier
    worker_id: Identifier
    incarnation_id: Identifier
    lease_epoch: Count
    execution_domain_id: Identifier
    process: ProcessIdentity | None
