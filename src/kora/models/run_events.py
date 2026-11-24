from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from kora.models.errors import WireRecord
from kora.protocols.identity import Identifier, Instant

PAGE_BYTES: int = 1024 * 1024
CURSOR_BYTES: int = 8192
DEFAULT_LIMIT: int = 100
ULID_PATTERN: str = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
CURSOR_PATTERN: str = r"^[A-Za-z0-9_-]+$"

EventIdentity = Annotated[StrictStr, Field(pattern=f"^event_{ULID_PATTERN}$")]
RunIdentity = Annotated[StrictStr, Field(pattern=f"^run_{ULID_PATTERN}$")]
NodeIdentity = Annotated[StrictStr, Field(pattern=f"^node_{ULID_PATTERN}$")]
AttemptIdentity = Annotated[StrictStr, Field(pattern=f"^att_{ULID_PATTERN}$")]
EventLimit = Annotated[StrictInt, Field(ge=1, le=DEFAULT_LIMIT)]
EventCursor = Annotated[
    StrictStr, Field(max_length=CURSOR_BYTES, pattern=CURSOR_PATTERN)
]


class SystemEventActor(WireRecord):
    kind: Literal["system"]
    component: Literal["scheduler"]


class TokenEventActor(WireRecord):
    kind: Literal["user", "service"]
    owner: Identifier
    token_id: Identifier


class RunEvent(WireRecord):
    id: EventIdentity
    kind: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    at: Instant
    actor: Annotated[SystemEventActor | TokenEventActor, Field(discriminator="kind")]
    run_id: RunIdentity
    node_instance_id: NodeIdentity | None
    attempt_id: AttemptIdentity | None


class RunEventPage(WireRecord):
    items: tuple[RunEvent, ...] = Field(max_length=DEFAULT_LIMIT)
    next_cursor: EventCursor | None
    observed_at: Instant


class RunEventsQuery(WireRecord):
    limit: EventLimit = DEFAULT_LIMIT
    cursor: EventCursor | None = None
    follow: Literal[False] = Field(
        default=False,
        description="Only a finite page is supported. true returns API003.",
    )
