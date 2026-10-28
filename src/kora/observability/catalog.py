import enum
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from kora.observability.settings import Level, is_dotted_name

SCHEMA_VERSION: int = 1

CONTEXT_STRING_KEYS: frozenset[str] = frozenset(
    {
        "namespace_id",
        "graph_id",
        "run_id",
        "node_path",
        "attempt_id",
        "session_id",
        "worker_id",
        "request_id",
        "execution_domain_id",
        "replayed_request_id",
    }
)
CONTEXT_INTEGER_KEYS: frozenset[str] = frozenset({"lease_epoch"})
CONTEXT_KEYS: frozenset[str] = CONTEXT_STRING_KEYS | CONTEXT_INTEGER_KEYS
RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "message",
        "component",
        "logger",
        "instance_id",
    }
)

LOGGING_COMPONENT: str = "logging"
THIRD_PARTY_COMPONENT: str = "thirdparty"


class FieldKind(enum.Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRINGS = "strings"
    MAPPING = "mapping"
    EXCEPTION = "exception"


class LossReason(enum.StrEnum):
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_FIELD = "invalid_field"
    OVER_BUDGET = "over_budget"
    CONTEXT_OVERRIDE = "context_override"
    INVALID_CONTEXT = "invalid_context"
    POLICY_FAILURE = "policy_failure"
    QUEUE_RECORDS = "queue_records"
    QUEUE_BYTES = "queue_bytes"
    SINK_FAILED = "sink_failed"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class EventSpec:
    name: str
    component: str
    level: Level
    message: str
    fields: Mapping[str, FieldKind]


class CatalogError(ValueError):
    pass


class EventCatalog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._components: set[str] = set()
        self._events: dict[str, EventSpec] = {}

    def register_component(self, name: str) -> str:
        if not is_dotted_name(name):
            raise CatalogError("component names are bounded dotted identifiers")
        with self._lock:
            self._components.add(name)
        return name

    def register(self, spec: EventSpec) -> EventSpec:
        if not is_dotted_name(spec.name):
            raise CatalogError("event names are bounded dotted identifiers")
        if spec.component not in self.components():
            raise CatalogError("an event belongs to a registered component")
        if RESERVED_KEYS & spec.fields.keys() or CONTEXT_KEYS & spec.fields.keys():
            raise CatalogError("event fields cannot shadow record or context keys")
        with self._lock:
            existing = self._events.get(spec.name)
            if existing is not None and existing != spec:
                raise CatalogError("an event is registered once")
            self._events[spec.name] = spec
        return spec

    def get(self, name: str) -> EventSpec | None:
        with self._lock:
            return self._events.get(name)

    def components(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._components)


CATALOG: EventCatalog = EventCatalog()

CATALOG.register_component(LOGGING_COMPONENT)
CATALOG.register_component(THIRD_PARTY_COMPONENT)

LOGGING_CONFIGURED: EventSpec = CATALOG.register(
    EventSpec(
        name="logging.configured",
        component=LOGGING_COMPONENT,
        level=Level.DEBUG,
        message="process diagnostics configured",
        fields={
            "configured_level": FieldKind.STRING,
            "render_format": FieldKind.STRING,
            "max_record_bytes": FieldKind.INTEGER,
            "queue_records": FieldKind.INTEGER,
            "queue_bytes": FieldKind.INTEGER,
            "drain_seconds": FieldKind.FLOAT,
        },
    )
)
RECORD_REJECTED: EventSpec = CATALOG.register(
    EventSpec(
        name="logging.record_rejected",
        component=LOGGING_COMPONENT,
        level=Level.WARNING,
        message="a diagnostic record was rejected by the content policy",
        fields={
            "reason": FieldKind.STRING,
            "source_component": FieldKind.STRING,
            "source_level": FieldKind.STRING,
            "suppressed": FieldKind.INTEGER,
        },
    )
)
RECORDS_DROPPED: EventSpec = CATALOG.register(
    EventSpec(
        name="logging.records_dropped",
        component=LOGGING_COMPONENT,
        level=Level.WARNING,
        message="diagnostic records were dropped before the sink",
        fields={
            "reason": FieldKind.STRING,
            "source_component": FieldKind.STRING,
            "source_level": FieldKind.STRING,
            "suppressed": FieldKind.INTEGER,
        },
    )
)
SINK_RECOVERED: EventSpec = CATALOG.register(
    EventSpec(
        name="logging.sink_recovered",
        component=LOGGING_COMPONENT,
        level=Level.WARNING,
        message="the diagnostic sink accepts records again",
        fields={"dropped": FieldKind.INTEGER},
    )
)
LOSS_SUMMARY: EventSpec = CATALOG.register(
    EventSpec(
        name="logging.loss_summary",
        component=LOGGING_COMPONENT,
        level=Level.WARNING,
        message="diagnostic records were lost during this process",
        fields={
            "rejected": FieldKind.INTEGER,
            "dropped": FieldKind.INTEGER,
            "suppressed": FieldKind.INTEGER,
            "secret_admission_failures": FieldKind.INTEGER,
            "sink_failures": FieldKind.INTEGER,
        },
    )
)
THIRD_PARTY_RECORD: EventSpec = CATALOG.register(
    EventSpec(
        name="thirdparty.record",
        component=THIRD_PARTY_COMPONENT,
        level=Level.DEBUG,
        message="a third-party library emitted a diagnostic",
        fields={"exception": FieldKind.EXCEPTION},
    )
)
