from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictInt, StringConstraints

from kora.models.errors import WireRecord
from kora.protocols.identity_records import Identifier, ProcessIdentity

WorkerName = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[\x21-\x7e]{1,255}$")
]


class WorkerRegistration(WireRecord):
    name: WorkerName
    incarnation_id: Identifier
    runtime: WorkerName
    execution_domain_id: Identifier
    version: WorkerName
    capacity: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    process: ProcessIdentity


class RegisteredWorker(WireRecord):
    worker_id: WorkerName
    incarnation_id: Identifier
    registered_at: Annotated[datetime, AwareDatetime]
