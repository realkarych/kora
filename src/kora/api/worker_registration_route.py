from collections.abc import Mapping

from starlette.responses import Response

from kora.api.errors import definitive_error
from kora.api.exchange import current_exchange
from kora.api.responses import error_response
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.engine.worker_registration import (
    COMPONENT,
    Registration,
    RegistrationConflict,
    RegistrationForbidden,
    record_registration,
    register_worker,
)
from kora.models.errors import ApiErrorCode
from kora.models.workers import RegisteredWorker, WorkerRegistration
from kora.observability import ProcessDiagnostics
from kora.protocols.runtime import RuntimeCapabilities
from kora.protocols.time import Clock


class WorkerRegistrationRoute:
    def __init__(
        self,
        database: Database,
        runtimes: Mapping[str, RuntimeCapabilities],
        clock: Clock,
        diagnostics: ProcessDiagnostics,
    ) -> None:
        self.database = database
        self.runtimes = {
            name: capabilities.portable_handles
            for name, capabilities in runtimes.items()
        }
        self.clock = clock
        self.identifiers = IdentifierGenerator(clock.now)
        self.logger = diagnostics.logger(COMPONENT)

    async def __call__(self, body: WorkerRegistration) -> RegisteredWorker | Response:
        exchange = current_exchange()
        principal = exchange.principal
        if principal is None:
            raise RuntimeError("worker authentication is required")
        try:
            portable = self.runtimes.get(body.runtime)
            if portable is None:
                raise RegistrationForbidden("configured runtime is required")
            registration = Registration(
                body.name,
                body.incarnation_id,
                body.runtime,
                body.execution_domain_id,
                body.version,
                body.capacity,
                body.process,
            )
            async with self.database.transaction() as connection:
                registered_at, replayed = await register_worker(
                    connection,
                    registration,
                    principal,
                    portable=portable,
                    now=self.clock.now(),
                    event_id=self.identifiers.new(),
                )
        except (RegistrationForbidden, RegistrationConflict) as error:
            code = (
                ApiErrorCode.FORBIDDEN
                if isinstance(error, RegistrationForbidden)
                else ApiErrorCode.CONFLICT
            )
            return error_response(
                definitive_error(code, request_id=exchange.request_id), method="POST"
            )
        record_registration(self.logger.bind(request_id=exchange.request_id), replayed)
        return RegisteredWorker(
            worker_id=body.name,
            incarnation_id=body.incarnation_id,
            registered_at=registered_at,
        )
