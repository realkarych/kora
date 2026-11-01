import json
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings
from tests.api.sink_probe import SinkReport
from tests.api.support import api_capture, application, client_for, lifespan
from tests.db.closing import ConnectionLedger
from tests.plugins.authentication.support import seed_case
from tests.support.observability import run_python

__all__ = ["api_capture"]


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_api_logging_sink_failure_does_not_stall_work(
    database_settings: DatabaseSettings, tmp_path: Path, sink: str
) -> None:
    configuration = tmp_path / "api-database.json"
    configuration.write_text(
        json.dumps(database_settings.url.render_as_string(hide_password=False))
    )

    process = run_python(["-m", "tests.api.sink_probe", sink, str(configuration)], {})

    assert process.code == 0, process.stderr
    report = SinkReport.model_validate_json(process.stdout)
    assert report.completed == 96
    assert report.outstanding == 0
    assert not report.writer_alive
    assert process.stderr == b""
    assert b"private-" not in process.stdout
    if sink == "blocked":
        assert report.dropped > 0
    else:
        assert report.failed


@pytest.mark.asyncio
async def test_api_lifespan_preserves_borrowed_database_and_diagnostics(
    database: Database, api_capture: Capture
) -> None:
    case = await seed_case(database, "api-borrowed")
    ledger = ConnectionLedger(database)
    app = application(database, api_capture)

    async with lifespan(app), client_for(app) as client:
        response = await client.get(
            "/api/v1/openapi.json",
            headers={"Authorization": str(case.request.authorization)},
        )
    async with database.read_transaction():
        assert ledger.outstanding == 1

    assert response.status_code == 200
    assert ledger.outstanding == 0
    assert api_capture.diagnostics.writer_is_alive()
