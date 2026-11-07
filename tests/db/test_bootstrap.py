import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO

import pytest
from sqlalchemy import delete, func, insert, select, text

from kora.conformance.authentication import authentication_context, kora_capture
from kora.conformance.capture import WAIT_SECONDS, Capture
from kora.db.bootstrap import (
    BootstrapMaterialError,
    BootstrapOutcome,
    BootstrapResult,
    IssuedBootstrap,
    initialize,
    issue_bootstrap,
    persist_bootstrap,
    publish,
)
from kora.db.connection import Database, Dialect
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import api_token, bootstrap_initialization, namespace
from kora.engine.start_capacity import outstanding_limit
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import NamespaceGrant, Principal, RequestInfo
from tests.support.event_loop import turn

__all__ = ["kora_capture"]

MOMENT: datetime = datetime(2026, 11, 8, tzinfo=UTC)
POLICY: dict[str, dict[str, int]] = {"concurrency": {"max_pending_runs": 1000}}
WAITING_NAMESPACE_LOCK: str = (
    "SELECT count(*) FROM pg_locks AS held "
    "JOIN pg_class AS relation ON relation.oid = held.relation "
    "WHERE relation.relname = 'namespace' AND NOT held.granted"
)


def clock() -> datetime:
    return MOMENT


def credentials(*values: str) -> Callable[[], str]:
    pending = iter(values)

    def source() -> str:
        return next(pending)

    return source


def refuse_credential() -> str:
    raise AssertionError("a committed initialization must not issue a credential")


def same_credential() -> str:
    return "c" * 64


def digest(value: str) -> str:
    return sha256(value.encode("ascii")).hexdigest()


class StoppingWriter:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, data: str, /) -> int:
        self.chunks.append(data)
        raise BrokenPipeError


async def registry(database: Database) -> tuple[int, int, int]:
    async with database.read_transaction() as connection:
        namespaces = await connection.scalar(
            select(func.count()).select_from(namespace)
        )
        tokens = await connection.scalar(select(func.count()).select_from(api_token))
        markers = await connection.scalar(
            select(func.count()).select_from(bootstrap_initialization)
        )
    assert type(namespaces) is int
    assert type(tokens) is int
    assert type(markers) is int
    return namespaces, tokens, markers


async def snapshot(
    database: Database,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    async with database.read_transaction() as connection:
        namespaces = await connection.execute(
            select(namespace).order_by(namespace.c.id)
        )
        tokens = await connection.execute(select(api_token).order_by(api_token.c.id))
        markers = await connection.execute(select(bootstrap_initialization))
    return (
        [dict(row) for row in namespaces.mappings()],
        [dict(row) for row in tokens.mappings()],
        [dict(row) for row in markers.mappings()],
    )


async def seed_namespace(database: Database, name: str) -> None:
    generator = IdentifierGenerator(clock)
    async with database.transaction() as connection:
        await connection.execute(
            insert(namespace).values(
                id=generator.new(),
                name=name,
                policy_revision=1,
                policy={"concurrency": {"max_pending_runs": 1}},
                created_at=MOMENT,
                updated_at=MOMENT,
            )
        )


async def seed_token(database: Database) -> None:
    generator = IdentifierGenerator(clock)
    async with database.transaction() as connection:
        await connection.execute(
            insert(api_token).values(
                id=generator.new(),
                kind="user",
                digest="ab" * 32,
                owner=generator.new(),
                namespaces=[],
                approve=False,
                platform=False,
                runtime=None,
                expires_at=None,
                created_at=MOMENT,
                updated_at=MOMENT,
            )
        )


async def interrupt(database: Database, issued: IssuedBootstrap) -> None:
    async with database.transaction() as connection:
        await persist_bootstrap(connection, database.dialect, issued)
        raise RuntimeError("interrupted")


async def clear_registry(database: Database) -> None:
    async with database.transaction() as connection:
        await connection.execute(delete(bootstrap_initialization))
        await connection.execute(delete(api_token))
        await connection.execute(delete(namespace))


def text_of(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    assert type(value) is str
    return value


def flag_of(row: Mapping[str, object], key: str) -> bool:
    value = row[key]
    assert type(value) is bool
    return value


async def assert_unchanged_refusal(database: Database) -> None:
    before = await snapshot(database)

    result = await initialize(
        database, clock, credentials=credentials("a" * 64, "b" * 64)
    )
    published = StringIO()
    publish(result, published)
    after = await snapshot(database)

    assert result.outcome is BootstrapOutcome.REFUSED
    assert result.cli_credential is None
    assert result.mcp_credential is None
    assert published.getvalue() == ""
    assert before == after


@pytest.mark.asyncio
async def test_standalone_initialization_is_atomic(
    database: Database, kora_capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> str:
        raise RuntimeError("the credential source failed")

    with pytest.raises(RuntimeError, match="the credential source failed"):
        await initialize(database, clock, credentials=fail)
    with pytest.raises(BootstrapMaterialError) as malformed:
        await initialize(database, clock, credentials=credentials("secret with spaces"))
    with pytest.raises(BootstrapMaterialError) as repeated:
        await initialize(database, clock, credentials=same_credential)
    issued = issue_bootstrap(clock, credentials=credentials("d" * 64, "e" * 64))
    with pytest.raises(RuntimeError, match="interrupted"):
        await interrupt(database, issued)

    assert "secret with spaces" not in str(malformed.value)
    assert "c" * 64 not in str(repeated.value)
    assert await registry(database) == (0, 0, 0)

    await seed_namespace(database, "default")
    await assert_unchanged_refusal(database)
    await clear_registry(database)
    await seed_token(database)
    await assert_unchanged_refusal(database)
    await clear_registry(database)
    assert await registry(database) == (0, 0, 0)

    cli = "1" * 64
    mcp = "2" * 64
    result = await initialize(database, clock, credentials=credentials(cli, mcp))
    published = StringIO()
    publish(result, published)
    namespaces, tokens, markers = await snapshot(database)
    authenticator = StaticTokenAuthenticator({}, database)
    context = authentication_context(kora_capture)
    cli_principal = await authenticator.authenticate(
        RequestInfo("Bearer " + cli), context
    )
    mcp_principal = await authenticator.authenticate(
        RequestInfo("Bearer " + mcp), context
    )
    again = await initialize(database, clock, credentials=refuse_credential)
    silent = StringIO()
    publish(again, silent)

    assert result.outcome is BootstrapOutcome.INITIALIZED
    assert result.cli_credential == cli
    assert result.mcp_credential == mcp
    assert published.getvalue() == (
        "KORA_TOKEN=" + cli + "\n" + "KORA_MCP_TOKEN=" + mcp + "\n"
    )
    assert await registry(database) == (1, 2, 1)
    assert len(namespaces) == 1
    namespace_row = namespaces[0]
    namespace_id = text_of(namespace_row, "id")
    assert text_of(namespace_row, "name") == "default"
    assert namespace_row["policy_revision"] == 1
    assert namespace_row["policy"] == POLICY
    assert outstanding_limit(namespace_row["policy"]) == 1000
    assert len(markers) == 1
    marker = markers[0]
    owner = text_of(marker, "owner")
    cli_token_id = text_of(marker, "cli_token_id")
    mcp_token_id = text_of(marker, "mcp_token_id")
    assert text_of(marker, "namespace_id") == namespace_id
    assert cli_token_id != mcp_token_id
    by_digest = {text_of(row, "digest"): row for row in tokens}
    cli_row = by_digest[digest(cli)]
    mcp_row = by_digest[digest(mcp)]
    assert text_of(cli_row, "id") == cli_token_id
    assert text_of(mcp_row, "id") == mcp_token_id
    for row, approve, platform in (
        (cli_row, True, True),
        (mcp_row, False, False),
    ):
        assert text_of(row, "kind") == "user"
        assert text_of(row, "owner") == owner
        assert row["runtime"] is None
        assert row["expires_at"] is None
        assert flag_of(row, "approve") is approve
        assert flag_of(row, "platform") is platform
        assert row["namespaces"] == [
            {"namespace_id": namespace_id, "transcripts": True}
        ]
        assert text_of(row, "digest") != cli
        assert text_of(row, "digest") != mcp
    assert cli_principal == Principal(
        token_id=cli_token_id,
        kind="user",
        owner=owner,
        namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=True),),
        approve=True,
        platform=True,
        runtime=None,
    )
    assert mcp_principal == Principal(
        token_id=mcp_token_id,
        kind="user",
        owner=owner,
        namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=True),),
        approve=False,
        platform=False,
        runtime=None,
    )
    assert again.outcome is BootstrapOutcome.ALREADY_INITIALIZED
    assert again.cli_credential is None
    assert again.mcp_credential is None
    assert silent.getvalue() == ""
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_standalone_committed_output_failure_requires_explicit_recovery(
    database: Database,
) -> None:
    cli = "3" * 64
    mcp = "4" * 64
    result = await initialize(database, clock, credentials=credentials(cli, mcp))
    writer = StoppingWriter()

    with pytest.raises(BrokenPipeError):
        publish(result, writer)
    stored = await snapshot(database)
    again = await initialize(database, clock, credentials=refuse_credential)
    silent = StringIO()
    publish(again, silent)

    assert writer.chunks == ["KORA_TOKEN=" + cli + "\n"]
    assert again.outcome is BootstrapOutcome.ALREADY_INITIALIZED
    assert again.cli_credential is None
    assert again.mcp_credential is None
    assert silent.getvalue() == ""
    assert await snapshot(database) == stored
    assert await registry(database) == (1, 2, 1)
    assert {text_of(row, "digest") for row in stored[1]} == {digest(cli), digest(mcp)}


@pytest.mark.asyncio
async def test_bootstrap_overlapping_initialization_has_one_winner(
    database: Database,
) -> None:
    first, second = await asyncio.gather(
        initialize(database, clock),
        initialize(database, clock),
    )

    created = [
        item for item in (first, second) if item.outcome is BootstrapOutcome.INITIALIZED
    ]
    skipped = [
        item
        for item in (first, second)
        if item.outcome is BootstrapOutcome.ALREADY_INITIALIZED
    ]
    published = StringIO()
    silent = StringIO()
    assert len(created) == 1
    assert len(skipped) == 1
    publish(created[0], published)
    publish(skipped[0], silent)
    cli = created[0].cli_credential
    mcp = created[0].mcp_credential

    assert skipped[0].cli_credential is None
    assert skipped[0].mcp_credential is None
    assert silent.getvalue() == ""
    assert cli is not None
    assert mcp is not None
    assert cli != mcp
    assert published.getvalue() == (
        "KORA_TOKEN=" + cli + "\n" + "KORA_MCP_TOKEN=" + mcp + "\n"
    )
    assert await registry(database) == (1, 2, 1)


async def hold_namespace(
    database: Database, inserted: asyncio.Event, release: asyncio.Event
) -> None:
    generator = IdentifierGenerator(clock)
    async with database.transaction() as connection:
        await connection.execute(
            insert(namespace).values(
                id=generator.new(),
                name="other",
                policy_revision=1,
                policy={"concurrency": {"max_pending_runs": 1}},
                created_at=MOMENT,
                updated_at=MOMENT,
            )
        )
        inserted.set()
        await release.wait()


async def namespace_lock_waiting(database: Database) -> bool:
    async with database.read_transaction() as connection:
        waiting = await connection.scalar(text(WAITING_NAMESPACE_LOCK))
    if type(waiting) is not int:
        raise RuntimeError("the lock wait count is missing")
    return waiting > 0


async def bootstrap_is_waiting(
    database: Database, initialization: asyncio.Task[BootstrapResult]
) -> bool:
    if initialization.done() or database.units_in_flight() < 2:
        return False
    if database.dialect is Dialect.SQLITE:
        return not database.writer_is_free()
    return await namespace_lock_waiting(database)


async def observe_bootstrap_wait(
    database: Database, initialization: asyncio.Task[BootstrapResult]
) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not await bootstrap_is_waiting(database, initialization):
        if time.monotonic() >= deadline:
            raise TimeoutError("the bootstrap lock was not observed")
        await turn()


@pytest.mark.asyncio
async def test_bootstrap_occupancy_follows_committed_writers(
    database: Database,
) -> None:
    inserted = asyncio.Event()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_namespace(database, inserted, release))
    initialization: asyncio.Task[BootstrapResult] | None = None
    result: BootstrapResult | None = None
    try:
        await inserted.wait()
        initialization = asyncio.create_task(initialize(database, clock))
        await observe_bootstrap_wait(database, initialization)
    finally:
        release.set()
        await holder
        if initialization is not None:
            result = await initialization
    assert result is not None
    published = StringIO()
    publish(result, published)
    names = await snapshot(database)

    assert result.outcome is BootstrapOutcome.REFUSED
    assert result.cli_credential is None
    assert result.mcp_credential is None
    assert published.getvalue() == ""
    assert await registry(database) == (1, 0, 0)
    assert [text_of(row, "name") for row in names[0]] == ["other"]
