import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import update

from kora.conformance.authentication import authentication_context as context_for
from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings
from kora.db.tables import api_token
from kora.observability.catalog import LOGGING_COMPONENT
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import RequestInfo
from kora.protocols.errors import KoraProtocolError
from tests.plugins.authentication.support import seed_case
from tests.support.observability import run_python

__all__ = ["kora_capture"]


@pytest.mark.asyncio
async def test_authentication_diagnostics_preserve_concurrent_authorized_contexts(
    database: Database, kora_capture: Capture
) -> None:
    cases = [
        await seed_case(database, "first-context"),
        await seed_case(database, "second-context"),
    ]
    provider = StaticTokenAuthenticator({}, database)
    context = context_for(kora_capture)
    contexts = [
        replace(
            context,
            logger=context.logger.bind(
                namespace_id=case.principal.namespaces[0].namespace_id,
                request_id="request-" + str(index),
            ),
        )
        for index, case in enumerate(cases)
    ]

    results = await asyncio.gather(
        *(
            provider.authenticate(case.request, scoped)
            for case, scoped in zip(cases, contexts, strict=True)
        )
    )
    records = [
        record
        for record in kora_capture.finish()
        if record["event"] == "authentication.static_token.outcome"
    ]

    assert results == [case.principal for case in cases]
    assert {(record["request_id"], record["namespace_id"]) for record in records} == {
        ("request-" + str(index), case.principal.namespaces[0].namespace_id)
        for index, case in enumerate(cases)
    }


@pytest.mark.asyncio
async def test_authentication_diagnostics_exclude_credentials_and_validity_categories(
    database: Database, kora_capture: Capture
) -> None:
    case = await seed_case(database, "diagnostics")
    provider = StaticTokenAuthenticator({}, database)
    context = context_for(kora_capture)

    assert await provider.authenticate(case.request, context) == case.principal
    for request in (
        RequestInfo(None),
        RequestInfo("Basic private-header-marker"),
        RequestInfo("Bearer private-unknown-marker"),
    ):
        with pytest.raises(KoraProtocolError):
            await provider.authenticate(request, context)
    await provider.revoke(case.principal.token_id, context)
    with pytest.raises(KoraProtocolError):
        await provider.authenticate(case.request, context)
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(kind="private-corrupt-row-marker")
        )
    with pytest.raises(KoraProtocolError):
        await provider.authenticate(case.request, context)
    records = kora_capture.finish()

    encoded = json.dumps(records)
    assert len(records) == 8
    assert records[0]["event"] == "logging.configured"
    assert records[0]["component"] == LOGGING_COMPONENT
    assert all(
        record["event"] == "authentication.static_token.outcome"
        for record in records[1:]
    )
    assert all(record["component"] == "conformance" for record in records[1:])
    for forbidden in (
        "private-",
        "expired",
        "revoked",
        "unknown",
        "malformed",
        case.principal.token_id,
        case.principal.owner,
    ):
        assert forbidden not in encoded
    assert all(record["level"] == "DEBUG" for record in records)


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_authentication_progress_and_database_release_survive_failed_diagnostics(
    database_settings: DatabaseSettings, tmp_path: Path, sink: str
) -> None:
    configuration = tmp_path / "database.json"
    configuration.write_text(
        json.dumps(database_settings.url.render_as_string(hide_password=False))
    )

    process = run_python(
        ["-m", "tests.plugins.authentication.sink_probe", sink, str(configuration)], {}
    )

    assert process.code == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["completed"] == 150
    assert report["outstanding"] == 0
    assert not report["writer_alive"]
    assert process.stderr == b""
    assert b"private-" not in process.stdout
    if sink == "blocked":
        assert report["dropped"] > 0
    else:
        assert report["failed"]
