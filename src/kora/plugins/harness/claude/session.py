from typing import Final

from kora.observability import CATALOG, EventSpec, FieldKind, Level

COMPONENT: Final[str] = CATALOG.register_component("harness.claude")
SESSION_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "harness.claude.session_finished",
        COMPONENT,
        Level.INFO,
        "a scripted Claude session finished",
        {"outcome": FieldKind.STRING},
    )
)
