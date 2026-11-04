from typing import cast

import pytest

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_retry_policies import (
    build_graph_retry_policies,
    read_graph_retry_policies,
)
from kora.ir.json import JsonObject, JsonValue
from tests.engine.graph_output_cases import LIMIT, child, output_document
from tests.engine.graph_retry_cases import retry_document


def test_disabled_policies_preserve_exact_sources_and_declarations() -> None:
    source = render_object(retry_document(), TextBudget(LIMIT))
    infrastructure: JsonObject = {
        "max_attempts": 1,
        "initial_interval": "0002m",
        "backoff_coefficient": 10**1000,
        "retry_on": ["timeout", "stalled"],
        "non_retryable": [],
    }

    def node(retry: JsonObject) -> JsonObject:
        return {
            "retry": retry,
            "provider_retry": {"max_wait": "0s"},
            "infrastructure_retry": infrastructure,
        }

    disabled: JsonObject = {"max_attempts": 1}
    expected: JsonObject = {
        "interpretation": "kora.disabled-retry-policies.v1",
        "nodes": {
            "command": node(disabled),
            "producer": node(
                {
                    "max_attempts": 1,
                    "initial_interval": "003s",
                    "backoff_coefficient": 2.0,
                    "max_interval": "020m",
                    "retry_on": ["artifact_invalid", "harness_error"],
                    "non_retryable": ["command_failed", "budget_exhausted"],
                    "predicate": '"é" == "é"',
                    "strategy": "resume",
                }
            ),
        },
        "subgraphs": {
            "alpha": {"retry": disabled, "nodes": {"timer": node(disabled)}},
            "unused": {
                "retry": {"max_attempts": 1, "predicate": "false"},
                "nodes": {
                    "approve": node(disabled),
                    "timer": node(disabled),
                    "wait": node(disabled),
                },
            },
        },
    }
    expected_text = render_object(expected, TextBudget(LIMIT))

    text = build_graph_retry_policies(source, max_encoded_bytes=LIMIT)
    saved = ObjectReader(text).object()
    checked = read_graph_retry_policies(source, saved, max_encoded_bytes=LIMIT)

    assert text == checked == expected_text
    assert (
        type(child(saved, "nodes", "producer", "retry")["backoff_coefficient"]) is float
    )
    assert (
        type(child(saved, "nodes", "producer", "infrastructure_retry")["max_attempts"])
        is int
    )


@pytest.mark.parametrize(
    "runtime_fields",
    [
        {},
        {"provider_retry": {}, "infrastructure_retry": {}},
        {
            "provider_retry": {"max_wait": "0000d"},
            "infrastructure_retry": {"max_attempts": 1},
        },
    ],
)
def test_absent_empty_and_disabled_controls_serialize_explicitly(
    runtime_fields: JsonObject,
) -> None:
    raw = output_document()
    child(raw, "runtime").update(runtime_fields)
    source = render_object(raw, TextBudget(LIMIT))

    text = build_graph_retry_policies(source, max_encoded_bytes=LIMIT)

    assert text == (
        '{"interpretation":"kora.disabled-retry-policies.v1",'
        '"nodes":{"producer":{"retry":{"max_attempts":1},'
        '"provider_retry":{"max_wait":"0s"},'
        '"infrastructure_retry":{"max_attempts":1}}},"subgraphs":{}}'
    )


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (
            ("runtime", "provider_retry"),
            {"max_wait": "0001ms"},
            GraphValueFailure.VERSION,
        ),
        (
            ("runtime", "infrastructure_retry"),
            {"max_attempts": 2, "retry_on": [], "predicate": "false"},
            GraphValueFailure.VERSION,
        ),
        (
            ("graph", "nodes", "producer", "retry"),
            {"max_attempts": 2},
            GraphValueFailure.VERSION,
        ),
        (
            ("graph", "subgraphs", "unused", "retry"),
            {"max_attempts": 2},
            GraphValueFailure.VERSION,
        ),
        (
            ("graph", "subgraphs", "unused", "nodes", "wait", "retry"),
            {"max_attempts": 2},
            GraphValueFailure.VERSION,
        ),
        (("runtime", "provider_retry"), {"max_wait": "0"}, GraphValueFailure.INVALID),
        (("graph", "nodes", "producer", "retry"), None, GraphValueFailure.INVALID),
        (
            ("graph", "nodes", "producer", "retry"),
            {"max_attempts": True},
            GraphValueFailure.INVALID,
        ),
    ],
)
def test_enabled_or_malformed_authored_controls_refuse(
    path: tuple[str, ...], value: JsonValue, reason: GraphValueFailure
) -> None:
    raw = retry_document()
    source = render_object(raw, TextBudget(LIMIT))
    saved = ObjectReader(
        build_graph_retry_policies(source, max_encoded_bytes=LIMIT)
    ).object()
    child(raw, *path[:-1])[path[-1]] = value
    changed = render_object(raw, TextBudget(LIMIT))

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_retry_policies(changed, max_encoded_bytes=LIMIT)
    with pytest.raises(GraphValueRejected) as read_failure:
        read_graph_retry_policies(changed, saved, max_encoded_bytes=LIMIT)

    assert failure.value.reason is read_failure.value.reason is reason
    assert str(failure.value) == reason.value


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing_binding", GraphValueFailure.INVALID),
        ("unknown_binding", GraphValueFailure.VERSION),
        ("bad_binding_type", GraphValueFailure.INVALID),
        ("section_order", GraphValueFailure.INVALID),
        ("missing_body", GraphValueFailure.INVALID),
        ("body_order", GraphValueFailure.INVALID),
        ("node_order", GraphValueFailure.INVALID),
        ("different_source", GraphValueFailure.INVALID),
        ("integer_for_float", GraphValueFailure.INVALID),
        ("retry_order", GraphValueFailure.INVALID),
        ("added_default", GraphValueFailure.INVALID),
        ("alias", GraphValueFailure.INVALID),
        ("cycle", GraphValueFailure.INVALID),
        ("key_collision", GraphValueFailure.INVALID),
        ("foreign_value", GraphValueFailure.INVALID),
    ],
)
def test_checker_rejects_incomplete_or_substituted_saved_section(
    change: str, reason: GraphValueFailure
) -> None:
    source = render_object(retry_document(), TextBudget(LIMIT))
    saved = ObjectReader(
        build_graph_retry_policies(source, max_encoded_bytes=LIMIT)
    ).object()
    nodes = child(saved, "nodes")
    retry = child(nodes, "producer", "retry")
    bodies = child(saved, "subgraphs")
    candidate: object = saved
    if change == "missing_binding":
        del saved["interpretation"]
    elif change == "unknown_binding":
        saved["interpretation"] = "kora.disabled-retry-policies.future"
    elif change == "bad_binding_type":
        saved["interpretation"] = True
    elif change == "section_order":
        saved["interpretation"] = saved.pop("interpretation")
    elif change == "missing_body":
        del bodies["unused"]
    elif change == "body_order":
        saved["subgraphs"] = dict(reversed(bodies.items()))
    elif change == "node_order":
        saved["nodes"] = dict(reversed(nodes.items()))
    elif change == "different_source":
        nodes["producer"], nodes["command"] = nodes["command"], nodes["producer"]
    elif change == "integer_for_float":
        retry["backoff_coefficient"] = 2
    elif change == "retry_order":
        errors = cast(list[JsonValue], retry["retry_on"])
        errors.reverse()
    elif change == "added_default":
        child(nodes, "command", "retry")["strategy"] = "fresh"
    elif change == "alias":
        child(nodes, "producer")["infrastructure_retry"] = child(
            nodes, "command", "infrastructure_retry"
        )
    elif change == "cycle":
        saved["nodes"] = saved
    elif change == "key_collision":
        saved["x-collision"] = {"\U0001f600": 1, "\ud83d\ude00": 2}
    else:
        candidate = tuple(saved.items())

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_retry_policies(source, candidate, max_encoded_bytes=LIMIT)

    assert failure.value.reason is reason


def test_retry_admission_charges_complete_capture_and_each_template_copy() -> None:
    raw = retry_document()
    child(raw, "runtime", "infrastructure_retry")["max_interval"] = "0" * 8192 + "m"
    source = render_object(raw, TextBudget(LIMIT))
    text = build_graph_retry_policies(source, max_encoded_bytes=LIMIT)
    saved = ObjectReader(text).object()
    template = render_object(
        child(saved, "nodes", "producer", "infrastructure_retry"), TextBudget(LIMIT)
    )
    names = (
        "producer",
        "command",
        "alpha",
        "unused",
        "timer",
        "wait",
        "approve",
        "timer",
    )
    exact = (
        len(source) + sum(len(name) + 2 for name in names) + len(template) + len(text)
    )

    built = build_graph_retry_policies(source, max_encoded_bytes=exact)
    checked = read_graph_retry_policies(
        source, saved, max_encoded_bytes=exact + len(text)
    )
    with pytest.raises(GraphValueRejected) as under:
        build_graph_retry_policies(source, max_encoded_bytes=exact - 1)
    with pytest.raises(GraphValueRejected) as one_copy:
        build_graph_retry_policies(source, max_encoded_bytes=exact - 5 * len(template))
    with pytest.raises(GraphValueRejected) as saved_under:
        read_graph_retry_policies(
            source, saved, max_encoded_bytes=exact + len(text) - 1
        )

    assert built == checked == text
    assert (
        under.value.reason
        is one_copy.value.reason
        is saved_under.value.reason
        is GraphValueFailure.BUDGET
    )


def test_constructor_refuses_noncanonical_source() -> None:
    source = render_object(retry_document(), TextBudget(LIMIT))
    noncanonical = source.replace('"ir_version":', '"ir_version": ', 1)

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_retry_policies(noncanonical, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID
