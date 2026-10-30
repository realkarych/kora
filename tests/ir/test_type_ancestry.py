import pytest

from kora.ir.type_ancestry import index_type_ancestry
from tests.ir.edge_type_cases import ancestry_edges
from tests.ir.type_declaration_cases import custom, model


def parent_oracle(parents: dict[str, str | None], produced: str, expected: str) -> bool:
    current: str | None = produced
    while current is not None:
        if current == expected:
            return True
        current = parents[current]
    return False


@pytest.mark.parametrize("reverse", [False, True])
def test_ancestry_intervals_match_independent_many_root_forest_oracle(
    reverse: bool,
) -> None:
    parents = {
        f"example/T{index}": None if index < 7 else f"example/T{(index - 7) // 3}"
        for index in range(70)
    }
    ordered = list(parents.items())
    if reverse:
        ordered.reverse()
    document = model({name: custom(parent) for name, parent in ordered})

    ancestry = index_type_ancestry(document.type_contracts)

    assert len(ancestry.intervals) == 70
    for produced in parents:
        for expected in parents:
            assert ancestry.accepts(produced, expected) is parent_oracle(
                parents, produced, expected
            )


def test_ancestry_index_handles_deep_parent_chain_without_recursion() -> None:
    document = ancestry_edges(10_000, 1)

    ancestry = index_type_ancestry(document.type_contracts)

    assert len(ancestry.intervals) == 10_000
    assert ancestry.accepts("example/T0", "example/T9999") is True
    assert ancestry.accepts("example/T9999", "example/T0") is False
    assert ancestry.accepts("example/T0", "example/Missing") is None


@pytest.mark.parametrize(
    "ending",
    [
        "missing",
        "self_cycle",
        "cycle",
        "unqualified",
        "revision",
        "builtin",
        "custom_builtin",
    ],
)
def test_unknown_ancestry_never_indexes_descendants_or_accepts_identity(
    ending: str,
) -> None:
    saved: dict[str, dict[str, object]] = {
        "example/Child": custom("example/Bad"),
        "example/Good": custom(),
        "example/Bad": custom(
            {
                "missing": "example/Missing",
                "self_cycle": "example/Bad",
                "cycle": "example/Child",
                "unqualified": "Unqualified",
                "revision": "VcsRef",
                "builtin": "UnknownBuiltin",
                "custom_builtin": "StructuredOutput",
            }[ending]
        ),
        "Unqualified": custom(),
        "VcsRef": {"builtin_revision": "unknown"},
        "UnknownBuiltin": {"builtin_revision": "1"},
        "StructuredOutput": custom(),
    }
    document = model(saved)

    ancestry = index_type_ancestry(document.type_contracts)

    assert set(ancestry.intervals) == {"example/Good"}
    assert ancestry.accepts("example/Child", "example/Child") is None
    assert ancestry.accepts("example/Bad", "example/Good") is None
    assert ancestry.accepts("example/Good", "example/Child") is None


@pytest.mark.parametrize(
    "name", ["StructuredOutput", "VcsRef", "AgentSession", "CommandResult"]
)
def test_published_builtin_is_an_indexed_root_with_custom_descendants(
    name: str,
) -> None:
    document = model(
        {
            name: {"builtin_revision": "1"},
            "example/Child": custom(name),
            "example/Other": custom(),
        }
    )

    ancestry = index_type_ancestry(document.type_contracts)

    assert ancestry.accepts(name, name) is True
    assert ancestry.accepts("example/Child", name) is True
    assert ancestry.accepts(name, "example/Child") is False
    assert ancestry.accepts(name, "example/Other") is False
