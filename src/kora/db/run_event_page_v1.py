from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.run_input_locator_v1 import canonical_locator_key

RUN_EVENT_REVISION: str = "run_event_metadata_0034"
SQLITE_EVENT_PAGE_INDEX: str = "ix_event_run_page_bounded"
CORRUPT_EVENT_KEY: str = "~"
ACTOR_BYTES: int = 1100
KIND_CHARACTERS: int = 255
KIND_BYTES: int = 4 * KIND_CHARACTERS
SQLITE_EVENT_SCOPE: str = " AND ".join(
    f"({canonical_locator_key(name)}) IS 1" for name in ("namespace_id", "run_id")
)


def capped_identity(name: str, *, nullable: bool = False) -> str:
    absent = f"WHEN {name} IS NULL THEN '' " if nullable else ""
    return (
        f"CASE {absent}WHEN ({canonical_locator_key(name)}) IS 1 "
        f"THEN {name} ELSE '{CORRUPT_EVENT_KEY}' END"
    )


def capped_text(name: str, maximum: int) -> str:
    return (
        f"CASE WHEN typeof({name}) = 'text' AND octet_length({name}) <= {maximum} "
        f"THEN {name} END"
    )


SQLITE_EVENT_FIELDS: dict[str, str] = {
    "id": capped_identity("id"),
    "at": capped_text("at", 64),
    "kind": capped_text("kind", KIND_BYTES),
    "node_instance_id": capped_identity("node_instance_id", nullable=True),
    "attempt_id": capped_identity("attempt_id", nullable=True),
    "actor": capped_text("actor", 2 * ACTOR_BYTES),
}
SQLITE_EVENT_KEYS: tuple[str, ...] = tuple(
    f"({expression})" for expression in SQLITE_EVENT_FIELDS.values()
)
SQLITE_EVENT_DDL: str = (
    f"CREATE INDEX {SQLITE_EVENT_PAGE_INDEX} ON event "
    f"(namespace_id, run_id, {', '.join(SQLITE_EVENT_KEYS)}) "
    f"WHERE {SQLITE_EVENT_SCOPE}"
)
SQLITE_EVENT_SCHEMA = text("""
    SELECT EXISTS(
        SELECT 1 FROM main.sqlite_schema
        WHERE type = 'index' AND tbl_name = 'event' AND name = :index_name
          AND sql = :definition
    ) AND NOT EXISTS (
        SELECT 1 FROM main.sqlite_schema WHERE name = 'custody_frontier' COLLATE NOCASE
    )
    """).bindparams(index_name=SQLITE_EVENT_PAGE_INDEX, definition=SQLITE_EVENT_DDL)


async def sqlite_run_event_page_available(connection: AsyncConnection) -> bool:
    return bool(await connection.scalar(SQLITE_EVENT_SCHEMA))
