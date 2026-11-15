import hashlib
import json
import math
from dataclasses import replace

import pytest

from kora.cel import CheckedExpression, Environment, ResultKind, check_typed
from kora.cel_eval.artifact_projection_codec import (
    ProjectionFailure,
    ProjectionRejected,
)
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import ErrorContext
from kora.cel_eval.evaluate import Machine, ResultType
from kora.conformance.common import context_for
from kora.engine.artifact_projection import (
    check_artifact_projection,
    prepare_artifact_projection,
)
from kora.engine.artifact_projection_source import projection_source
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import encode_snapshot
from tests.cel_eval.test_cel_evaluation import clock
from tests.engine.artifact_projection_cases import (
    LIMITS,
    PATH,
    metadata,
    native_values,
    projection_capture,
    projection_graph,
)
from tests.engine.test_graph_resource_references import SCOPE
from tests.protocols.effective_artifact_cases import installed


@pytest.mark.parametrize(
    ("source_schema", "destination_schema", "value", "expression"),
    [
        ({"type": "integer"}, {"type": "number"}, -0.0, "value == 0.0"),
        (
            {"type": "number"},
            {"type": "integer"},
            9007199254740993,
            "value == 9007199254740993",
        ),
    ],
)
def test_cel_projection_composes_through_collect_and_exports(
    source_schema: JsonObject,
    destination_schema: JsonObject,
    value: JsonValue,
    expression: str,
) -> None:
    producer = projection_graph(source_schema)
    destination = projection_graph(destination_schema)
    source = projection_source(destination, PATH, LIMITS, validating=False)
    checked = check_typed(
        expression, Environment({"value": source.view.variable}, ResultKind.BOOL)
    )
    assert isinstance(checked, CheckedExpression)
    machine = Machine(load_binding(), clock)

    with projection_capture() as capture:
        first = prepare_artifact_projection(
            producer, PATH, value, installed(), context_for(capture), limits=LIMITS
        )
        restored = check_artifact_projection(
            producer, PATH, first.encoded(), metadata(first), limits=LIMITS
        )
        original, earlier_conversion = native_values(json.loads(restored.encoded()))
        composed = prepare_artifact_projection(
            destination,
            PATH,
            original,
            installed(),
            context_for(capture),
            limits=LIMITS,
        )
        wrong_source = prepare_artifact_projection(
            destination,
            PATH,
            earlier_conversion,
            installed(),
            context_for(capture),
            limits=LIMITS,
        )
    _, converted = native_values(json.loads(composed.encoded()))
    _, lost = native_values(json.loads(wrong_source.encoded()))
    full_decision = machine.evaluate(
        checked,
        {"value": value},
        {"value": source.view.variable.type},
        ErrorContext("projection"),
        ResultType.BOOL,
    )
    cut_decision = machine.evaluate(
        checked,
        {"value": converted},
        {"value": source.view.variable.type},
        ErrorContext("projection"),
        ResultType.BOOL,
    )

    assert full_decision is cut_decision is True
    assert encode_snapshot(converted) != encode_snapshot(lost)
    if type(value) is float:
        assert type(converted) is float
        assert math.copysign(1.0, converted) == -1
    else:
        assert type(converted) is int
        assert converted == 9007199254740993


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ("true", b'{"whole":[],"presence":[]}'),
        ("has(inputs.data)", b'{"whole":[],"presence":[""]}'),
        (
            "has(inputs.data) && inputs.data == inputs.data",
            b'{"whole":[""],"presence":[]}',
        ),
        (
            "has(inputs.data) && inputs.data['😀'] == true",
            rb"""{"whole":["['\\U0001f600']"],"presence":[""]}""",
        ),
    ],
)
def test_projection_path_digest_vectors(predicate: str, expected: bytes) -> None:
    checked = projection_graph(predicate=predicate)

    source = projection_source(checked, PATH, LIMITS, validating=False)

    assert source.binding.path_set_digest == hashlib.sha256(expected).hexdigest()


def test_projection_binding_separates_equal_content_across_graph_owners() -> None:
    left = projection_graph()
    right = projection_graph(scope=replace(SCOPE, version_id="another-version"))

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            left, PATH, 7, installed(), context_for(capture), limits=LIMITS
        )
        independent = prepare_artifact_projection(
            right, PATH, 7, installed(), context_for(capture), limits=LIMITS
        )
        with pytest.raises(ProjectionRejected) as swapped:
            check_artifact_projection(
                right, PATH, prepared.encoded(), metadata(prepared), limits=LIMITS
            )

    assert prepared.encoded() == independent.encoded()
    assert prepared.binding != independent.binding
    assert swapped.value.reason is ProjectionFailure.BINDING


@pytest.mark.parametrize(
    "value", [{"flag": True, "unused": [1, 2]}, {"flag": False}, {}]
)
def test_projection_selected_fields_match_the_full_cel_decision(
    value: JsonValue,
) -> None:
    graph = projection_graph(
        {"type": "object", "properties": {"flag": {"type": "boolean"}}},
        "has(inputs.data) && has(inputs.data.flag) && inputs.data.flag",
    )
    source = projection_source(graph, PATH, LIMITS, validating=False)
    expression = check_typed(
        "has(value.flag) && value.flag",
        Environment({"value": source.view.variable}, ResultKind.BOOL),
    )
    assert isinstance(expression, CheckedExpression)
    machine = Machine(load_binding(), clock)

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            graph, PATH, value, installed(), context_for(capture), limits=LIMITS
        )
    original, converted = native_values(json.loads(prepared.encoded()))
    full = machine.evaluate(
        expression,
        {"value": value},
        {"value": source.view.variable.type},
        ErrorContext("projection"),
        ResultType.BOOL,
    )
    projected = machine.evaluate(
        expression,
        {"value": converted},
        {"value": source.view.variable.type},
        ErrorContext("projection"),
        ResultType.BOOL,
    )

    assert projected is full
    assert isinstance(projected, bool)
    assert isinstance(original, dict)
    assert "unused" not in original
