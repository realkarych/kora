import gc
import json
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import export_ir, validate_graph_links, validate_root_previous_attempt_refs
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import model, node, ref


@pytest.mark.parametrize("kind", list(NODE_CASES))
def test_root_previous_attempt_refuses_every_node_kind(kind: str) -> None:
    document = model(
        {"subject": node(kind, inputs={"previous": ref("$previous_attempt")})}
    )

    result = validate_root_previous_attempt_refs(document)

    assert not result.root_previous_attempt_refs_valid
    assert result.root_nodes_checked == result.root_inputs_checked == 1
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].model_dump() == {
        "code": "IR006",
        "message": "Previous-attempt input is unavailable in the root graph.",
        "path": ("graph", "nodes", "subject", "inputs", "previous", "from"),
        "location": None,
        "offset": None,
        "node": "subject",
        "hint": None,
    }


@pytest.mark.parametrize("optional", [MISSING, False, True])
@pytest.mark.parametrize(
    "retry", [MISSING, {}, {"max_attempts": 1}, {"max_attempts": 3}]
)
def test_root_previous_attempt_is_not_legalized_by_node_retry_or_optional(
    retry: object, optional: object
) -> None:
    document = model(
        {
            "subject": node(
                retry=retry,
                inputs={"previous": ref("$previous_attempt", optional=optional)},
            )
        }
    )

    result = validate_root_previous_attempt_refs(document)

    assert not result.root_previous_attempt_refs_valid
    assert len(result.diagnostics.errors) == 1


@pytest.mark.parametrize("kind", ["subgraph", "fan_out"])
@pytest.mark.parametrize(
    "retry", [MISSING, {}, {"max_attempts": 1}, {"max_attempts": 3}]
)
def test_root_previous_attempt_cannot_borrow_referenced_body_retry(
    kind: str, retry: object
) -> None:
    document = model(
        {"call": node(kind, inputs={"items": ref("$previous_attempt")})},
        {"body": {"nodes": {"subject": node()}, "retry": retry}},
    )

    result = validate_root_previous_attempt_refs(document)

    assert not result.root_previous_attempt_refs_valid
    assert result.root_nodes_checked == result.root_inputs_checked == 1
    assert len(result.diagnostics.errors) == 1


@pytest.mark.parametrize("invalid_root", [False, True])
def test_root_previous_attempt_does_not_check_stored_bodies(
    invalid_root: bool,
) -> None:
    previous = {"previous": ref("$previous_attempt")}
    document = model(
        {
            "first": node("subgraph", inputs=previous if invalid_root else {}),
            "second": node("subgraph"),
        },
        {
            "body": {
                "nodes": {"nested": node("subgraph", inputs=previous)},
                "retry": {"max_attempts": 3},
            },
            "unused": {"nodes": {"subject": node(inputs=previous)}},
            "empty_retry": {"nodes": {"subject": node(inputs=previous)}, "retry": {}},
            "single": {
                "nodes": {"subject": node(inputs=previous)},
                "retry": {"max_attempts": 1},
            },
        },
    )

    result = validate_root_previous_attempt_refs(document)

    assert result.root_previous_attempt_refs_valid is not invalid_root
    assert result.root_nodes_checked == 2
    assert (
        result.root_inputs_checked
        == len(result.diagnostics.errors)
        == int(invalid_root)
    )


@pytest.mark.parametrize("inputs", [MISSING, {}])
def test_root_previous_attempt_accepts_absent_or_empty_inputs(inputs: object) -> None:
    document = model({"subject": node(inputs=inputs)})

    result = validate_root_previous_attempt_refs(document)

    assert result.root_previous_attempt_refs_valid
    assert result.root_nodes_checked == 1
    assert result.root_inputs_checked == 0


def test_root_previous_attempt_checks_only_exact_operational_sources() -> None:
    document = model(
        {
            "subject": node(
                "command",
                inputs={
                    "ordinary": ref("previous_attempt"),
                    "params": ref("$params"),
                    "body": ref("$inputs"),
                    "absent": {"type": "StructuredOutput"},
                    "path": ref("$params", path="$previous_attempt"),
                },
                config={
                    "cmd": ["$previous_attempt"],
                    "workspace": {"repo": "$previous_attempt"},
                    "env": {"VALUE": "$previous_attempt"},
                },
                **{"x-metadata": {"inputs": {"from": "$previous_attempt"}}},
            )
        },
        params={"inputs": {"from": "$previous_attempt"}},
        triggers=[{"kind": "manual", "params": {"from": "$previous_attempt"}}],
    )

    result = validate_root_previous_attempt_refs(document)

    assert result.root_previous_attempt_refs_valid
    assert result.root_inputs_checked == 5


def test_root_previous_attempt_faults_are_independent_of_other_prerequisites() -> None:
    document = model(
        {
            "second": node(
                inputs={"previous": ref("$previous_attempt", type="Unpublished")},
                depends_on=["missing"],
            ),
            "first": node(
                inputs={
                    "z": ref("$previous_attempt", path="unparsed["),
                    "a": ref("$previous_attempt", output="missing"),
                    "ordinary": ref("missing"),
                }
            ),
        }
    )

    links = validate_graph_links(document)
    result = validate_root_previous_attempt_refs(document)

    assert not links.links_valid
    assert all(error.code == "IR002" for error in links.diagnostics.errors)
    assert not result.root_previous_attempt_refs_valid
    assert result.root_nodes_checked == 2
    assert result.root_inputs_checked == 4
    assert [error.path for error in result.diagnostics.errors] == [
        ("graph", "nodes", "first", "inputs", "a", "from"),
        ("graph", "nodes", "first", "inputs", "z", "from"),
        ("graph", "nodes", "second", "inputs", "previous", "from"),
    ]
    assert all(error.code == "IR006" for error in result.diagnostics.errors)


@pytest.mark.parametrize(
    "source",
    [
        MISSING,
        "node.py",
        "/private/source",
        "https://invalid/source",
        "\ud800",
        "s" * 1025,
    ],
)
def test_root_previous_attempt_preserves_only_safe_node_source(source: object) -> None:
    document = model(
        {
            "subject": node(
                inputs={"previous": ref("$previous_attempt")},
                source=MISSING if source is MISSING else {"file": source, "line": 7},
            )
        },
        source={"file": "ancestor.py", "line": 1},
    )

    result = validate_root_previous_attempt_refs(document)

    error = result.diagnostics.errors[0]
    assert error.node == "subject"
    assert error.offset is error.hint is None
    if source == "node.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 7)
    else:
        assert error.location is None


def test_root_previous_attempt_preserves_omission_invalidity() -> None:
    count = 2000
    document = model(
        {
            "n" * 64: node(
                inputs={
                    "i" + str(index): ref("$previous_attempt") for index in range(count)
                },
                source={"file": "s" * 1000, "line": 1},
            )
        }
    )

    result = validate_root_previous_attempt_refs(document)
    payload = json.dumps(
        {
            "errors": [
                error.model_dump(mode="json") for error in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.root_previous_attempt_refs_valid
    assert result.root_inputs_checked == count
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == count
    assert 0 < len(result.diagnostics.errors) < 1000
    assert result.diagnostics.errors_omitted > 1000
    assert len(payload) <= 1_048_576


@pytest.mark.parametrize("invalid", [False, True])
def test_root_previous_attempt_preserves_and_releases_borrowed_inputs(
    invalid: bool,
) -> None:
    document = model(
        {
            "subject": node(
                inputs={"previous": ref("$previous_attempt" if invalid else "$params")},
                source={"file": "node.py", "line": 7},
            )
        }
    )
    subject = document.graph.nodes["subject"]
    inputs = subject.inputs
    assert inputs is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(subject),
        weakref.ref(subject.source),
        weakref.ref(inputs["previous"]),
    ]
    before = export_ir(document)

    result = validate_root_previous_attempt_refs(document)

    assert document.graph.nodes["subject"] is subject
    assert subject.inputs is inputs
    assert export_ir(document) == before
    assert result.root_previous_attempt_refs_valid is not invalid
    del document, subject, inputs
    gc.collect()
    assert all(reference() is None for reference in references)
    diagnostic_references: list[Callable[[], object | None]] = [
        weakref.ref(error) for error in result.diagnostics.errors
    ]
    diagnostic_references.extend(
        weakref.ref(error.location) for error in result.diagnostics.errors
    )
    result_reference = weakref.ref(result)
    del result
    gc.collect()
    assert all(reference() is None for reference in diagnostic_references)
    assert result_reference() is None
