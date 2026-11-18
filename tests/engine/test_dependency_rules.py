from itertools import product

import pytest

from kora.engine.dependency_rules import (
    FAILURES,
    FINISHED,
    RULES,
    UNFINISHED,
    DependencyOutcome,
    decide_dependencies,
)

OUTCOMES: dict[str, tuple[str, ...]] = {
    "all_success": (
        "satisfied",
        "upstream_failed",
        "skipped",
        "upstream_failed",
        "skipped",
        "upstream_failed",
    ),
    "all_done": ("satisfied",) * 6,
    "one_success": (
        "satisfied",
        "satisfied",
        "satisfied",
        "upstream_failed",
        "skipped",
        "upstream_failed",
    ),
    "one_failed": (
        "skipped",
        "satisfied",
        "skipped",
        "satisfied",
        "skipped",
        "satisfied",
    ),
    "none_failed": (
        "satisfied",
        "upstream_failed",
        "satisfied",
        "upstream_failed",
        "satisfied",
        "upstream_failed",
    ),
    "none_failed_min_one_success": (
        "satisfied",
        "upstream_failed",
        "satisfied",
        "upstream_failed",
        "skipped",
        "upstream_failed",
    ),
    "all_skipped": ("skipped", "skipped", "skipped", "skipped", "satisfied", "skipped"),
}
PAIRS = (
    ("succeeded", "succeeded"),
    ("succeeded", "failed"),
    ("succeeded", "skipped"),
    ("failed", "skipped"),
    ("skipped", "skipped"),
    ("failed", "failed"),
)


@pytest.mark.parametrize("rule", RULES)
def test_trigger_rules_have_complete_terminal_outcomes(rule: str) -> None:
    expected = OUTCOMES[rule]
    cases = tuple(zip(PAIRS, expected, strict=True))

    actual = tuple(decide_dependencies(rule, states) for states, _ in cases)

    assert actual == expected
    for states, outcome in cases:
        for first, second in product(FAILURES, repeat=2):
            replaced = (
                first if states[0] == "failed" else states[0],
                second if states[1] == "failed" else states[1],
            )
            assert decide_dependencies(rule, replaced) == outcome
            assert decide_dependencies(rule, tuple(reversed(replaced))) == outcome


@pytest.mark.parametrize("rule", RULES)
def test_every_rule_waits_for_every_predecessor(rule: str) -> None:
    states = tuple(product(UNFINISHED, FINISHED))

    actual = tuple(decide_dependencies(rule, pair) for pair in states)

    assert set(actual) == {DependencyOutcome.WAITING}
    assert all(
        decide_dependencies(rule, tuple(reversed(pair))) is DependencyOutcome.WAITING
        for pair in states
    )


@pytest.mark.parametrize("rule", RULES)
def test_all_rules_accept_a_genuine_root(rule: str) -> None:
    predecessors: tuple[str, ...] = ()

    actual = decide_dependencies(rule, predecessors)

    assert actual is DependencyOutcome.SATISFIED


@pytest.mark.parametrize(
    ("rule", "states"),
    [
        ("unknown", ()),
        ("all_done", ("unknown",)),
        ("one_success", ("succeeded", "unknown")),
        ("one_failed", ("failed", "unknown")),
    ],
)
def test_unknown_rule_or_state_never_supplies_authority(
    rule: str, states: tuple[str, ...]
) -> None:
    actual = decide_dependencies(rule, states)

    assert actual is DependencyOutcome.REFUSED
