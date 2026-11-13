from datetime import timedelta

from kora.api.application import create_application
from kora.api.exchange import ApiExchange
from kora.api.graph_services import GraphServices
from kora.api.worker_registration_route import WorkerRegistrationRoute
from kora.conformance.authentication import AuthenticationCase
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.models.workers import WorkerRegistration
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import RuntimeCapabilities
from tests.api.support import RequestIds
from tests.plugins.authentication.support import AuthorityClock

CAPABILITIES = RuntimeCapabilities(
    supports_resume=False,
    portable_handles=False,
    enforces_network_policy=False,
    isolates_tool_secrets=False,
    credential_proxy=False,
    protected_execution=False,
    plan_executors=frozenset({"runner"}),
    max_concurrent=None,
    default_stall_after=timedelta(seconds=30),
)


def registration(case: AuthenticationCase) -> WorkerRegistration:
    return WorkerRegistration(
        name=case.principal.owner,
        incarnation_id="incarnation-" + "a" * 64,
        runtime="local",
        execution_domain_id="domain-" + "a" * 64,
        version="0.1.0",
        capacity=1,
        process=ProcessIdentity(
            host_id="host",
            boot_id="boot",
            pid_namespace="namespace",
            pid=1234,
            start_time=500,
            leads_group=True,
        ),
    )


def worker_application(
    database: Database,
    capture: Capture,
    clock: AuthorityClock,
    capabilities: RuntimeCapabilities = CAPABILITIES,
    *,
    deployment: GraphServices | None = None,
) -> ApiExchange:
    return create_application(
        StaticTokenAuthenticator({}, database),
        capture.diagnostics,
        clock,
        RequestIds(),
        deployment,
        worker_registration=WorkerRegistrationRoute(
            database,
            {"local": capabilities},
            clock,
            capture.diagnostics,
        ),
    )


def bearer(case: AuthenticationCase) -> str:
    value = case.request.authorization
    assert value is not None
    return value.removeprefix("Bearer ")
