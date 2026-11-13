from urllib.parse import urlencode

from anyio.lowlevel import checkpoint

from kora.db.connection import require_snapshot_time
from kora.models.worker_history import WorkerHistoryQuery
from kora.protocols.identity_records import MAX_COUNT

TEXT_FIELDS: frozenset[str] = frozenset(
    {"dispatcher_id", "snapshot_digest", "prefix_digest"}
)


class HistoryQueryRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the grant history query is invalid")


def _octet(raw: bytes, index: int, end: int) -> tuple[int, int]:
    value = raw[index]
    if value == 37:
        if index + 2 >= end:
            raise HistoryQueryRejected()
        digits = raw[index + 1 : index + 3]
        if any(item not in b"0123456789abcdefABCDEF" for item in digits):
            raise HistoryQueryRejected()
        return int(digits, 16), index + 3
    return (32 if value == 43 else value), index + 1


async def parse_history_query(raw: bytes, deadline: float) -> WorkerHistoryQuery:
    values: dict[str, str | int] = {}
    offset = 0
    while offset < len(raw):
        key = bytearray()
        while offset < len(raw) and raw[offset] != 61:
            value, offset = _octet(raw, offset, len(raw))
            if not 33 <= value <= 126 or len(key) >= 20 or value == 38:
                raise HistoryQueryRejected()
            key.append(value)
        name = key.decode("ascii")
        if (
            name not in WorkerHistoryQuery.model_fields
            or name in values
            or offset == len(raw)
        ):
            raise HistoryQueryRejected()
        offset += 1
        value_start = offset
        text = bytearray()
        count = 0
        scanned = 0
        while offset < len(raw) and raw[offset] != 38:
            value, offset = _octet(raw, offset, len(raw))
            if name in TEXT_FIELDS:
                maximum = 256 if name == "dispatcher_id" else 64
                if not 33 <= value <= 126 or len(text) >= maximum:
                    raise HistoryQueryRejected()
                text.append(value)
            else:
                if not 48 <= value <= 57 or count > (MAX_COUNT - value + 48) // 10:
                    raise HistoryQueryRejected()
                count = count * 10 + value - 48
            scanned += 1
            if scanned % 256 == 0:
                require_snapshot_time(deadline)
                await checkpoint()
        if offset == value_start:
            raise HistoryQueryRejected()
        values[name] = text.decode("ascii") if name in TEXT_FIELDS else count
        if offset < len(raw):
            offset += 1
            if offset == len(raw):
                raise HistoryQueryRejected()
        require_snapshot_time(deadline)
    return WorkerHistoryQuery.model_validate(values)


def canonical_history_query(query: WorkerHistoryQuery) -> bytes:
    return urlencode(query.model_dump(exclude_none=True)).encode("ascii")
