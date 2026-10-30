import subprocess
import sys

import pytest

from kora.cel import (
    ABSENT,
    Diagnostic,
    Field,
    JsonValue,
    Path,
    Position,
    Reason,
    check_path,
    resolve,
)


def parsed_path(source: str) -> Path:
    result = check_path(source)
    assert isinstance(result, Path), f"rejected {result}"
    return result


def rejected_path(source: str) -> Diagnostic:
    result = check_path(source)
    assert isinstance(result, Diagnostic), f"accepted {source!r}"
    return result


DOCUMENT: JsonValue = {
    "bundle": {"facts": {"change": {"commit": "abc"}}},
    "items": [{"name": "first"}, {"name": "second"}],
    "flags": {"0": "zero"},
    "count": 3,
}


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("a" + ".b" * 33, Reason.DEPTH),
        ("a" + "[0]" * 33, Reason.DEPTH),
        ("a." + "b" * 4095, Reason.LENGTH),
        ("a[-1]", Reason.INVALID_PATH),
        ("a[0x1]", Reason.INVALID_PATH),
        ("a[1u]", Reason.INVALID_PATH),
        ("a[1.0]", Reason.INVALID_PATH),
        ("a[b]", Reason.INVALID_PATH),
        ("a.b()", Reason.INVALID_PATH),
        ("a + b", Reason.INVALID_PATH),
        ("a.", Reason.INVALID_PATH),
        ("['k'].a", Reason.INVALID_PATH),
        ("a[99999999999999999999]", Reason.INVALID_LITERAL),
        ("if.b", Reason.RESERVED_IDENTIFIER),
    ],
)
def test_cel_paths_obey_limits_and_never_reach_binding(
    source: str, reason: Reason
) -> None:
    diagnostic = rejected_path(source)

    assert diagnostic.reason is reason


@pytest.mark.parametrize(
    ("prefix", "separator", "excessive_step", "trailing"),
    [
        ("root" + ".field" * 32, "", ".overflow", ""),
        ("root" + "[0]" * 32, "", "[0]", ""),
        ("root['😀😀😀']" + ".field" * 31, "", "[0]", ""),
        ("root" + ".field" * 32, "", ".overflow", ".later"),
        ("root" + ".field" * 32, " \t// comment\n", ".overflow", ""),
    ],
    ids=["fields", "indexes", "unicode_code_points", "trailing_step", "trivia"],
)
def test_cel_paths_report_first_excessive_step(
    prefix: str, separator: str, excessive_step: str, trailing: str
) -> None:
    source = prefix + separator + excessive_step + trailing
    step_start = len(prefix + separator)
    step_end = step_start + len(excessive_step)

    accepted = check_path(prefix)
    diagnostic = check_path(source)

    assert isinstance(accepted, Path)
    assert isinstance(diagnostic, Diagnostic)
    assert diagnostic.code.value == "IR027"
    assert diagnostic.reason is Reason.DEPTH
    assert step_start <= diagnostic.offset < step_end


PATH_PROCESS = """
import sys
from kora.cel import check_path, resolve
path = check_path("bundle.facts['change'].items[0]")
resolve({"bundle": {"facts": {"change": {"items": [1]}}}}, path.steps)
check_path("a[-1]")
print("cel" in sys.modules)
"""


def test_cel_paths_never_load_the_binding() -> None:
    command = [sys.executable, "-I", "-c", PATH_PROCESS]

    completed = subprocess.run(command, capture_output=True, text=True, check=True)

    assert completed.stdout.strip() == "False"


def test_cel_paths_accept_selection_and_literal_indexes() -> None:
    deepest = "a" + ".b" * 32

    simple = parsed_path("inputs.bundle.facts.change")
    keyed = parsed_path("artifacts['implement.change']")
    mixed = parsed_path("items[1].name")
    limit = parsed_path(deepest)

    assert simple.steps == (
        Field("inputs"),
        Field("bundle"),
        Field("facts"),
        Field("change"),
    )
    assert keyed.steps == (Field("artifacts"), Field("implement.change"))
    assert mixed.steps == (Field("items"), Position(1), Field("name"))
    assert len(limit.steps) == 33


def test_cel_paths_walker_follows_cel_indexing() -> None:
    commit = parsed_path("bundle.facts.change.commit")
    second = parsed_path("items[1].name")
    beyond = parsed_path("items[2].name")
    key_on_list = parsed_path("items['0']")
    position_on_map = parsed_path("flags[0]")
    field_on_scalar = parsed_path("count.value")
    missing = parsed_path("bundle.facts.other")

    assert resolve(DOCUMENT, commit.steps) == "abc"
    assert resolve(DOCUMENT, second.steps) == "second"
    assert resolve(DOCUMENT, beyond.steps) is ABSENT
    assert resolve(DOCUMENT, key_on_list.steps) is ABSENT
    assert resolve(DOCUMENT, position_on_map.steps) is ABSENT
    assert resolve(DOCUMENT, field_on_scalar.steps) is ABSENT
    assert resolve(DOCUMENT, missing.steps) is ABSENT
    assert resolve([True, False], (Position(1),)) is False
    assert resolve({"a": [1, 2]}, (Field("a"), Position(-1))) is ABSENT
