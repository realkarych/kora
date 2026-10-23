from kora.observability import (
    CATALOG,
    ComponentLogger,
    EventSpec,
    FieldKind,
    Level,
    current_diagnostics,
)
from kora.protocols.lifetime import InvocationId, LifetimeReason

COMPONENT: str = CATALOG.register_component("conformance.lifetime")
EVENTS: dict[str, EventSpec] = {
    name: CATALOG.register(
        EventSpec(
            f"conformance.lifetime.{name}",
            COMPONENT,
            level,
            message,
            {
                "invocation_id": FieldKind.STRING,
                "reason": FieldKind.STRING,
                "resources": FieldKind.INTEGER,
            },
        )
    )
    for name, level, message in (
        ("started", Level.DEBUG, "local resource ownership started"),
        ("activated", Level.DEBUG, "local resource activated"),
        ("closed", Level.DEBUG, "local resource ownership closed"),
        ("cancelled", Level.DEBUG, "local resource ownership cancelled"),
        ("recovery", Level.DEBUG, "local resource recovery started"),
        ("refused", Level.WARNING, "local resource operation refused"),
        ("escalated", Level.WARNING, "local resource cleanup escalated"),
        ("unknown", Level.WARNING, "local resource closure remains unknown"),
    )
}


class LifetimeDiagnostics:
    def __init__(self, invocation_id: InvocationId) -> None:
        self.invocation_id = invocation_id
        diagnostics = current_diagnostics()
        self.logger: ComponentLogger | None = (
            None if diagnostics is None else diagnostics.logger(COMPONENT)
        )

    def emit(self, event: str, reason: LifetimeReason, resources: int) -> None:
        if self.logger is not None:
            self.logger.emit(
                EVENTS[event],
                invocation_id=self.invocation_id,
                reason=reason.value,
                resources=resources,
            )
