from typing import Final

import pytest

from kora.ir.globs import (
    CALL_CHARGE,
    MAX_GLOB_BYTES,
    MAX_PROTECTED_PATHS,
    MIDDLE_PIECE_CHARGE,
    PIECE_CHARGE,
    Fuel,
    FuelExhausted,
    GlobInvalid,
    compile_glob,
    compile_globs,
    folded,
    folded_subject,
    has_control_character,
    needle_weight,
    searchable,
    star_match,
    star_pattern,
)

UNLIMITED: Final[int] = 10**12
MATCHING: Final[list[tuple[str, str, bool]]] = [
    ("\u1fb3\u0302", "\u03b1\u0302\u03b9", True),
    ("kelvin", "\u212aelvin", True),
    ("caf\u00e9/x", "cafe\u0301/x", True),
    ("cafe\u0301/x", "caf\u00e9/x", True),
    ("Makefile", "Makefile", True),
    ("Makefile", "makefile", True),
    ("Makefile", "src/Makefile", False),
    ("tests", "tests", True),
    ("tests", "tests/a.py", True),
    ("tests", "tests/unit/a.py", True),
    ("tests", "testsuite/a.py", False),
    ("tests/**", "tests", True),
    ("tests/**", "tests/a/b.py", True),
    ("**/conftest.py", "conftest.py", True),
    ("**/conftest.py", "a/b/conftest.py", True),
    ("**/conftest.py", "a/b/conftest.pyc", False),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "a/x/y/c", False),
    ("a/**/**/b", "a/x/b", True),
    ("a/*", "a/b", True),
    ("a/*", "a/b/c", True),
    ("a/*", "a", False),
    ("*.yml", "ci.yml", True),
    ("*.yml", "dir/ci.yml", False),
    ("src/*/gate.sh", "src/x/gate.sh", True),
    ("src/*/gate.sh", "src/x/y/gate.sh", False),
    ("ab*ba", "abba", True),
    ("TESTS/**", "tests/a.py", True),
    ("**", "anything/at/all", True),
    ("tests", "./tests/a", True),
    ("tests", "x/../tests/a", True),
    ("tests", "/tests/a", True),
    ("STRASSE", "stra\u00dfe", True),
    ("/".join(["a"] * 32), "/".join(["a"] * 32), True),
    ("a" * 4096, "a" * 4096, True),
    ("gates/run*", "gates/run\u0303.sh", True),
    ("gates/run*", "gates/ru\u00f1.sh", True),
]
REFUSED: Final[list[str]] = [
    "",
    "/etc/passwd",
    "~/x",
    "a//b",
    "a/./b",
    "a/../b",
    "a/",
    "a?b",
    "a[b]",
    "{a,b}",
    "a\\b",
    "a**b",
    "a/b**",
    "a\x00b",
    "a\nb",
    "a\x85b",
    "a/***b",
    "~backup",
    "*" + "a" * 97 + "*",
    "*" + "\u00df" * 49 + "*",
    "tests/\udc80",
    "gates/x\u00e1*",
    "gates/ru\u00f1*",
    "gates/*\u0301x",
    "gates/\u1fb3*",
    "a" * 4097,
    "/".join(["a"] * 33),
]


def unlimited() -> Fuel:
    return Fuel(UNLIMITED)


@pytest.mark.parametrize(("pattern", "path", "expected"), MATCHING)
def test_protected_path_globs(pattern: str, path: str, expected: bool) -> None:
    glob = compile_glob(pattern)

    matched = glob.matches(folded_subject(folded(path)), unlimited())

    assert matched is expected


@pytest.mark.parametrize("pattern", REFUSED)
def test_protected_path_globs_refuse_other_forms(pattern: str) -> None:
    with pytest.raises(GlobInvalid) as refused:
        compile_glob(pattern)

    assert str(refused.value) == "the path glob is invalid"


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("ls", "ls", True),
        ("ls", "ls -la", False),
        ("git *", "git status", True),
        ("git *", "git", False),
        ("*", "", True),
        ("a***b", "ab", True),
        ("a*b*c", "abc", True),
        ("a*b*c", "acb", False),
        ("ab*ba", "aba", False),
        ("*x*", "a/x/b", True),
    ],
)
def test_star_match_cases(pattern: str, text: str, expected: bool) -> None:
    compiled = star_pattern(pattern)

    matched = star_match(compiled, text, unlimited())

    assert matched is expected


def test_consecutive_stars_collapse() -> None:
    compiled = star_pattern("a**" + "*" * 50 + "b")

    pieces = compiled.pieces

    assert pieces == ("a", "b")


def test_needles_weigh_by_length_and_long_ones_are_unsearchable() -> None:
    short = star_pattern("*" + "a" * 40 + "b*")
    longest = star_pattern("*" + "a" * 96 + "*")
    long = star_pattern("*" + "a" * 97 + "*")

    observed = (short.weight, searchable(longest), searchable(long))

    assert observed == (22, True, False)


def test_star_match_from_an_offset() -> None:
    pattern = star_pattern("rm *")

    matched = [
        star_match(pattern, "sudo rm -rf x", unlimited(), start) for start in (0, 5)
    ]

    assert matched == [False, True]


def test_fuel_is_charged_and_runs_out_on_a_star_dense_glob() -> None:
    glob = compile_glob("**/" + "/".join(["*a*a*a*"] * 30) + "/b")
    subject = folded_subject(folded("/".join(["aaaa"] * 200)))
    fuel = Fuel(50_000)

    with pytest.raises(FuelExhausted) as exhausted:
        glob.matches(subject, fuel)

    assert fuel.spent > fuel.budget
    assert str(exhausted.value) == "the decision ran out of fuel"
    assert not isinstance(exhausted.value, ValueError | OSError)


CHARGED: Final[list[tuple[str, str, bool, int]]] = [
    ("ab*cd*ef", "xxxxxxxxxx", False, 0),
    ("ab*ef", "abxxxxef", True, 0),
    ("ab*cd*ef", "ab__cd__ef", True, 6 * needle_weight(2)),
    ("ab*cd*ef", "abxxxxxxxx", False, 0),
]


@pytest.mark.parametrize(("pattern", "text", "expected", "window"), CHARGED)
def test_the_window_is_charged_only_once_both_edges_match(
    pattern: str, text: str, expected: bool, window: int
) -> None:
    compiled = star_pattern(pattern)
    fuel = Fuel(UNLIMITED)
    fixed = (
        CALL_CHARGE
        + PIECE_CHARGE * min(len(compiled.pieces), 2)
        + MIDDLE_PIECE_CHARGE * max(len(compiled.pieces) - 2, 0)
        + len(compiled.pieces[0])
        + len(compiled.pieces[-1])
    )

    found = star_match(compiled, text, fuel)

    assert found is expected
    assert fuel.spent == fixed + window


def test_folding_follows_the_interpreter_case_tables() -> None:
    capital, small = "Ɤ", "ɤ"

    same = folded(capital) == folded(small)

    assert same == (capital.casefold() == small.casefold())


STRUCTURAL: Final[frozenset[str]] = frozenset("/.*~?[]{}\\")


def test_no_code_point_folds_into_glob_structure() -> None:
    points = (point for point in range(0x80, 0x110000) if not 0xD800 <= point <= 0xDFFF)

    invented = [
        point
        for point in points
        if not folded(chr(point))
        or not STRUCTURAL.isdisjoint(folded(chr(point)))
        or has_control_character(folded(chr(point))) > has_control_character(chr(point))
    ]

    assert invented == []


GLOB_SIZE: Final[int] = MAX_GLOB_BYTES // MAX_PROTECTED_PATHS
AT_BYTE_BOUND: Final[tuple[str, ...]] = ("a" * GLOB_SIZE,) * MAX_PROTECTED_PATHS


def test_glob_lists_reach_their_byte_bound() -> None:
    globs = AT_BYTE_BOUND

    compiled = compile_globs(globs)

    assert len(compiled) == MAX_PROTECTED_PATHS


def test_glob_lists_past_their_byte_bound_are_refused() -> None:
    globs = (*AT_BYTE_BOUND[:-1], "a" * (GLOB_SIZE + 1))

    with pytest.raises(GlobInvalid):
        compile_globs(globs)
