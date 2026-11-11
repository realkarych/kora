import json

import pytest

from kora.api.graph_cel import COMPLETED
from tests.api.cel_pattern_validation_scenarios import PATTERN_MARKER, REQUEST_ID
from tests.api.graph_cel_scenarios import REFUSAL_MARKER
from tests.cel_eval.children import ROOT, full_path, run_child


@pytest.mark.parametrize(
    ("scenario", "statuses"),
    [
        ("mixed", ["complete", "complete"]),
        (
            "refusals",
            [
                "complete",
                *(["invalid_allowance"] * 4),
                *(["capacity_exhausted"] * 4),
                *(["typed_refused"] * 2),
                "syntax_unresolved",
                *(["typed_refused"] * 13),
                "schema_unresolved",
                "context_unresolved",
                *(["typed_refused"] * 3),
            ],
        ),
        (
            "infrastructure",
            [
                *(["complete"] * 5),
                *(["typed_refused"] * 6),
                "schema_unresolved",
                "syntax_unresolved",
                "capacity_exhausted",
                "capacity_exhausted",
            ],
        ),
        (
            "facts",
            [
                "complete",
                "complete",
                "complete",
                "capacity_exhausted",
                "invalid_allowance",
                "complete",
            ],
        ),
        (
            "typed-limits",
            [
                "complete",
                "complete",
                "complete",
                "capacity_exhausted",
                "capacity_exhausted",
                "capacity_exhausted",
                "invalid_allowance",
                "invalid_allowance",
            ],
        ),
        ("lifetime", ["complete"]),
        ("native-refusal", ["rendering_refused", "complete"]),
    ],
    ids=[
        "cross-family",
        "preflight",
        "infrastructure",
        "facts",
        "typed-limits",
        "resource-release",
        "native-refusal",
    ],
)
def test_graph_cel_uses_complete_bounded_native_verification(
    scenario: str, statuses: list[str]
) -> None:
    code = f"from tests.api.graph_cel_scenarios import run\nrun({scenario!r})"

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stdout == ""
    assert PATTERN_MARKER not in result.stderr
    assert REFUSAL_MARKER not in result.stderr
    assert "Function 'matches' error:" not in result.stderr
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    completions = [record for record in records if record["event"] == COMPLETED.name]
    assert [record["status"] for record in completions] == statuses
    assert all(record["request_id"] == REQUEST_ID for record in completions)
    assert all(record["level"] == "DEBUG" for record in completions)
