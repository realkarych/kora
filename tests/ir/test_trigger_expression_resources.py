import gc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel import CheckedExpression, Environment
from kora.cel.checker import Checker
from kora.cel.cost import BoundShapes, Estimator
from kora.cel.parser import Parser
from kora.cel.tree import Node
from kora.ir import SensorTrigger, validate_sensor_trigger_expressions
from tests.ir.trigger_expression_cases import triggers


@pytest.mark.parametrize("text", ["observation.ready == true", "unknown", "true &&"])
@pytest.mark.parametrize("count", [1, 100, 1000])
def test_sensor_trigger_work_counts_each_expression_once(text: str, count: int) -> None:
    one = triggers(1, text)
    many = triggers(count, text)

    single = validate_sensor_trigger_expressions(one)
    repeated = validate_sensor_trigger_expressions(many)

    assert repeated.triggers_checked == repeated.sensor_triggers_checked == count
    assert repeated.expressions_checked + repeated.expressions_unresolved == count
    assert repeated.expressions_checked == single.expressions_checked * count
    assert repeated.expressions_unresolved == single.expressions_unresolved * count
    assert repeated.syntax_steps == single.syntax_steps * count
    assert repeated.checker_steps == single.checker_steps * count
    assert repeated.expressions_valid is (text == "observation.ready == true")


@pytest.mark.parametrize("text", [" " * 4092 + "true", "(" * 31 + "true" + ")" * 31])
def test_sensor_trigger_preserves_existing_admitted_source_boundaries(
    text: str,
) -> None:
    document = triggers(1, text)

    result = validate_sensor_trigger_expressions(document)

    assert result.expressions_valid
    assert result.expressions_checked == 1
    assert result.expressions_unresolved == 0


@pytest.mark.parametrize("text", ["observation.ready", "unknown", "true &&"])
def test_sensor_trigger_releases_inputs_and_temporary_state_while_result_survives(
    text: str,
) -> None:
    document = triggers(1, text)
    assert document.graph.triggers is not MISSING
    trigger = document.graph.triggers[0]
    assert isinstance(trigger, SensorTrigger)
    assert document.graph.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(trigger),
        weakref.ref(trigger.poll),
        weakref.ref(document.graph.source),
    ]
    del trigger
    gc.collect()
    owned_types = (
        Environment
        | Checker
        | Parser
        | CheckedExpression
        | BoundShapes
        | Estimator
        | Node
    )
    baseline = sum(isinstance(value, owned_types) for value in gc.get_objects())

    result = validate_sensor_trigger_expressions(document)
    diagnostic_refs = [weakref.ref(value) for value in result.diagnostics.errors]
    source_refs = [
        weakref.ref(value.location)
        for value in result.diagnostics.errors
        if value.location is not None
    ]
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    remaining = sum(isinstance(value, owned_types) for value in gc.get_objects())
    result_ref = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert remaining == baseline
    assert bool(diagnostic_refs) is (text == "unknown")
    assert all(reference() is None for reference in diagnostic_refs + source_refs)
    assert result_ref() is None
