import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import event, worker, worker_execution_domain, worker_incarnation
from kora.db.worker_registration import (
    insert_registration_row,
    locked_worker,
    registered_incarnation,
)
from kora.ir.json import parse_json_object
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.protocols.authentication import Principal
from kora.protocols.identity_records import ProcessIdentity

COMPONENT: str = CATALOG.register_component("engine")
REGISTERED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.worker.registered",
        COMPONENT,
        Level.INFO,
        "a worker incarnation registered",
        {"replayed": FieldKind.BOOLEAN},
    )
)


class RegistrationForbidden(RuntimeError):
    pass


class RegistrationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Registration:
    name: str
    incarnation_id: str
    runtime: str
    execution_domain_id: str
    version: str
    capacity: int
    process: ProcessIdentity

    def digest(self) -> str:
        return fingerprint(
            [
                self.name,
                self.incarnation_id,
                self.runtime,
                self.execution_domain_id,
                self.version,
                self.capacity,
                self.process.model_dump(mode="json"),
            ]
        )


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def register_worker(
    connection: AsyncConnection,
    registration: Registration,
    principal: Principal,
    *,
    portable: bool,
    now: datetime,
    event_id: str,
) -> tuple[datetime, bool]:
    if (
        principal.kind != "service"
        or principal.owner != registration.name
        or principal.runtime != registration.runtime
    ):
        raise RegistrationForbidden("worker registration authority is required")
    domain = registration.execution_domain_id
    owner = None if portable else registration.name
    await insert_registration_row(
        connection,
        worker_execution_domain,
        {
            "execution_domain_id": domain,
            "runtime": registration.runtime,
            "worker_id": owner,
        },
    )
    domain_result = await connection.execute(
        select(
            worker_execution_domain.c.runtime, worker_execution_domain.c.worker_id
        ).where(worker_execution_domain.c.execution_domain_id == domain)
    )
    if tuple(domain_result.one()) != (registration.runtime, owner):
        raise RegistrationConflict("execution domain ownership conflicts")
    values: dict[str, object] = {
        "incarnation_id": registration.incarnation_id,
        "execution_domain_id": domain,
        "runtime": registration.runtime,
        "version": registration.version,
        "capacity": registration.capacity,
        "last_seen_at": now,
        "updated_at": now,
    }
    await insert_registration_row(
        connection,
        worker,
        {
            "id": registration.name,
            "token_id": principal.token_id,
            "created_at": now,
            **values,
        },
    )
    current = await locked_worker(connection, registration.name)
    if (
        current["token_id"] != principal.token_id
        or current["runtime"] != registration.runtime
    ):
        raise RegistrationForbidden("worker registration authority is required")
    digest = registration.digest()
    previous = await registered_incarnation(
        connection, registration.name, registration.incarnation_id
    )
    if previous is not None:
        if previous["request_digest"] != digest:
            raise RegistrationConflict("incarnation registration conflicts")
        registered: object = previous["registered_at"]
        if not isinstance(registered, datetime):
            raise RuntimeError("invalid registration timestamp")
        return registered, True
    inserted = await insert_registration_row(
        connection,
        worker_incarnation,
        {
            "worker_id": registration.name,
            "incarnation_id": registration.incarnation_id,
            "execution_domain_id": domain,
            "process": parse_json_object(registration.process.model_dump(mode="json")),
            "process_key": fingerprint(registration.process.identity_key()),
            "request_digest": digest,
            "registered_at": now,
        },
    )
    if not inserted:
        raise RegistrationConflict("process is already registered")
    await connection.execute(
        update(worker).where(worker.c.id == registration.name).values(**values)
    )
    await connection.execute(
        insert(event).values(
            id=event_id,
            namespace_id=None,
            run_id=None,
            node_instance_id=None,
            attempt_id=None,
            kind="worker.registered",
            actor={
                "kind": "service",
                "owner": principal.owner,
                "token_id": principal.token_id,
            },
            payload={"runtime": registration.runtime, "version": registration.version},
            at=now,
            created_at=now,
            updated_at=now,
        )
    )
    return now, False


def record_registration(logger: ComponentLogger, replayed: bool) -> None:
    logger.emit(REGISTERED, replayed=replayed)
