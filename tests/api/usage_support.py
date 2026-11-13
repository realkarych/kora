from kora.api.application import create_application
from kora.api.exchange import ApiExchange
from kora.api.worker_usage_route import WorkerUsageRoute
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.models.worker_usage import UsageReport
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.support import RequestIds
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_usage_acceptance import coverage, usage
from tests.engine.worker_usage_support import UsageCase


def usage_application(
    database: Database,
    capture: Capture,
    case: UsageCase,
    *,
    lock_timeout_ms: int = 10_000,
) -> ApiExchange:
    return create_application(
        StaticTokenAuthenticator({}, database),
        capture.diagnostics,
        case.clock,
        RequestIds(),
        worker_usage=WorkerUsageRoute(
            database,
            LIMITS,
            case.clock,
            case.identifiers,
            lock_timeout_ms=lock_timeout_ms,
        ),
    )


def report(case: UsageCase, **changes: object) -> UsageReport:
    return UsageReport.model_validate(
        {
            "lease_epoch": 1,
            "dispatcher_id": case.dispatcher_id,
            "rows": [usage(input_tokens=1_000_000)],
            "coverage": coverage(),
            **changes,
        }
    )


def usage_path(case: UsageCase) -> str:
    return f"/api/v1/attempts/{case.attempt_id}/usage"
