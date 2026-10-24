from dataclasses import dataclass

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.state import LocalState, integer, row_values, text
from kora.protocols.storage_models import (
    ContentRef,
    StoragePage,
    StorageScanCursor,
    StorageScanEntry,
    StorageScanKind,
)


@dataclass(frozen=True)
class InventoryResult:
    page: StoragePage
    queried_rows: int
    query_steps: int | None


def scan_entry(kind: StorageScanKind, value: object) -> StorageScanEntry:
    row = row_values(value)
    if row is None:
        refuse("storage_integrity")
    try:
        if kind == "stages":
            status = text(row[1])
            if status not in {"open", "sealed", "retiring", "retired"}:
                refuse("storage_integrity")
            return StorageScanEntry.model_validate(
                {
                    "version": 1,
                    "key": text(row[0]),
                    "state": status,
                    "size": integer(row[2]),
                }
            )
        reference = ContentRef.model_validate_json(text(row[1]))
        return StorageScanEntry(
            version=1, key=text(row[0]), state="content", size=reference.total_size
        )
    except ValueError:
        refuse("storage_integrity")


def scan(
    state: LocalState,
    kind: StorageScanKind,
    cursor: StorageScanCursor | None,
    limit: int,
    *,
    measure: bool = False,
) -> InventoryResult:
    if kind not in {"stages", "content"}:
        refuse("storage_conflict")
    if type(limit) is not int or limit < 1:
        refuse("storage_range")
    if cursor is not None and (
        cursor.storage_identity != state.root.identity or cursor.kind != kind
    ):
        refuse("storage_conflict")
    bound = min(limit, 128)
    after = "" if cursor is None else cursor.last_key
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 0

    query = (
        "SELECT stage_id, state, reserved FROM stages WHERE stage_id>? "
        "ORDER BY stage_id COLLATE BINARY LIMIT ?"
        if kind == "stages"
        else "SELECT digest, reference FROM contents WHERE digest>? "
        "ORDER BY digest COLLATE BINARY LIMIT ?"
    )
    with state.transaction(synchronize=False) as connection:
        if measure:
            connection.set_progress_handler(progress, 1)
        try:
            statement = connection.execute(query, (after, bound))
            try:
                rows = statement.fetchall()
            finally:
                statement.close()
        finally:
            if measure:
                connection.set_progress_handler(None, 0)
    entries: list[StorageScanEntry] = []
    next_cursor: StorageScanCursor | None = None
    for value in rows:
        entry = scan_entry(kind, value)
        candidate_cursor = StorageScanCursor(
            version=1,
            storage_identity=state.root.identity,
            kind=kind,
            last_key=entry.key,
        )
        try:
            StoragePage(
                version=1,
                storage_identity=state.root.identity,
                kind=kind,
                entries=(*entries, entry),
                next_cursor=candidate_cursor,
            )
        except ValueError:
            if not entries:
                refuse("storage_integrity")
            break
        entries.append(entry)
        next_cursor = candidate_cursor
    if len(entries) == len(rows) and len(rows) < bound:
        next_cursor = None
    page = StoragePage(
        version=1,
        storage_identity=state.root.identity,
        kind=kind,
        entries=tuple(entries),
        next_cursor=next_cursor,
    )
    return InventoryResult(page, len(rows), steps if measure else None)
