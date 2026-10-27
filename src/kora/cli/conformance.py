from typing import Literal

from kora.cli.app import CLI_COMPONENT
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import BoundaryModel
from kora.protocols.lifetime import ClosureReport, InvocationId, OriginalOutcome

CONFORMANCE_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "cli.conformance_finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "local conformance operation finished",
        {
            "invocation_id": FieldKind.STRING,
            "outcome": FieldKind.STRING,
            "reason": FieldKind.STRING,
            "completed": FieldKind.INTEGER,
            "expected": FieldKind.INTEGER,
        },
    )
)


class CommandResult(BoundaryModel):
    outcome: OriginalOutcome
    reason: Literal[
        "complete", "refused", "cancelled", "cleanup_unknown", "cleanup_complete"
    ]
    invocation_id: InvocationId | None = None
    completed: int = 0
    expected: int = 0
    cleanup: ClosureReport | None = None

    @property
    def exit_code(self) -> int:
        if self.outcome is OriginalOutcome.CANCELLED:
            return 130
        return 0 if self.reason in {"complete", "cleanup_complete"} else 1
