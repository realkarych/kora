import base64
import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl

from pydantic import Field, StrictStr

from kora.models.errors import WireRecord
from kora.models.run_events import (
    CURSOR_BYTES,
    ULID_PATTERN,
    EventLimit,
    RunEventsQuery,
)

CanonicalId = Annotated[StrictStr, Field(pattern=f"^{ULID_PATTERN}$")]
RAW_QUERY_BYTES: int = 3 * (CURSOR_BYTES + len("limit=100&cursor=&follow=false"))


class RunEventPosition(WireRecord):
    version: Literal[1] = 1
    operation: Literal["run_events"] = "run_events"
    order: Literal["id_desc"] = "id_desc"
    namespace: CanonicalId
    run: CanonicalId
    limit: EventLimit
    ceiling: CanonicalId
    after: CanonicalId

    def encode(self) -> str:
        return (
            base64.urlsafe_b64encode(self.model_dump_json().encode())
            .rstrip(b"=")
            .decode("ascii")
        )


def parse_event_query(raw: bytes) -> RunEventsQuery:
    if len(raw) > RAW_QUERY_BYTES:
        raise ValueError("invalid run event page selector")
    pairs = parse_qsl(
        raw.decode("ascii"),
        keep_blank_values=True,
        strict_parsing=True,
        encoding="ascii",
        errors="strict",
        max_num_fields=3,
    )
    values = dict(pairs)
    limit = values.get("limit", "100")
    if (
        len(values) != len(pairs)
        or values.keys() - RunEventsQuery.model_fields.keys()
        or values.get("follow", "false") != "false"
        or re.fullmatch(r"[1-9][0-9]{0,2}", limit) is None
    ):
        raise ValueError("invalid run event page selector")
    return RunEventsQuery(limit=int(limit), cursor=values.get("cursor"))


def decode_event_position(
    query: RunEventsQuery, namespace: str, run: str
) -> RunEventPosition | None:
    cursor = query.cursor
    if cursor is None:
        return None
    data = base64.b64decode(
        cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
    )
    position = RunEventPosition.model_validate_json(data)
    if (
        position.namespace != namespace
        or position.run != run
        or position.limit != query.limit
        or position.after > position.ceiling
        or position.encode() != cursor
    ):
        raise ValueError("invalid run event page selector")
    return position
