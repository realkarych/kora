from dataclasses import dataclass

from pydantic import MISSING

from kora.cel import Environment, ResultKind, Variable, check_typed
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.cel.meter import Meter
from kora.cel.parser import parse
from kora.cel.types import DYN
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.trigger import SensorTrigger
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.sensor_trigger_expressions.completed",
        COMPONENT,
        Level.INFO,
        "sensor trigger expression validation completed",
        {
            "expressions_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "triggers": FieldKind.INTEGER,
            "sensors": FieldKind.INTEGER,
            "expressions": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
            "syntax_steps": FieldKind.INTEGER,
            "checker_steps": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class SensorTriggerExpressionResult:
    diagnostics: DiagnosticCollection
    triggers_checked: int
    sensor_triggers_checked: int
    expressions_checked: int
    expressions_unresolved: int
    syntax_steps: int
    checker_steps: int

    @property
    def expressions_valid(self) -> bool:
        return self.diagnostics.valid and self.expressions_unresolved == 0


def validate_sensor_trigger_expressions(
    document: GraphDocument,
) -> SensorTriggerExpressionResult:
    environment = Environment({"observation": Variable(DYN)}, ResultKind.BOOL)
    syntax_meter = Meter()
    checker_meter = Meter()
    collector = DiagnosticCollector()
    graph = document.graph
    triggers = () if graph.triggers is MISSING else graph.triggers
    sensors = 0
    checked = 0
    unresolved = 0
    for index, trigger in enumerate(triggers):
        if not isinstance(trigger, SensorTrigger):
            continue
        sensors += 1
        source = trigger.poll.until
        try:
            parse(source, meter=syntax_meter)
        except DiagnosticError:
            unresolved += 1
            continue
        checked += 1
        outcome = check_typed(source, environment, checker_meter)
        if isinstance(outcome, CelDiagnostic):
            collector.add(
                from_cel(
                    outcome,
                    path=("graph", "triggers", index, "poll", "until"),
                    exact_expression_field=True,
                    location=safe_source(graph.source),
                )
            )
    result = SensorTriggerExpressionResult(
        collector.finish(),
        len(triggers),
        sensors,
        checked,
        unresolved,
        syntax_meter.steps,
        checker_meter.steps,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            expressions_valid=result.expressions_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            triggers=result.triggers_checked,
            sensors=result.sensor_triggers_checked,
            expressions=result.expressions_checked,
            unresolved=result.expressions_unresolved,
            syntax_steps=result.syntax_steps,
            checker_steps=result.checker_steps,
        )
    return result
