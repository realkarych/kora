from pathlib import Path
from typing import cast

import pytest

from kora.ir import ToolPolicy
from tests.ir.support import EXAMPLES, load_yaml

EXAMPLE_FILES: list[Path] = sorted(EXAMPLES.rglob("*.ir.yaml"))


def tool_policies(value: object) -> list[object]:
    if isinstance(value, dict):
        items = cast("dict[object, object]", value)
        found = [item for key, item in items.items() if key == "tool_policy"]
        return found + [
            policy for item in items.values() for policy in tool_policies(item)
        ]
    if isinstance(value, list):
        return [
            policy
            for item in cast("list[object]", value)
            for policy in tool_policies(item)
        ]
    return []


def test_the_examples_carry_tool_policies() -> None:
    policies = [
        policy for path in EXAMPLE_FILES for policy in tool_policies(load_yaml(path))
    ]

    assert len(EXAMPLE_FILES) >= 2
    assert len(policies) >= 6


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_ir_models_accept_examples(path: Path) -> None:
    policies = tool_policies(load_yaml(path))

    accepted = [ToolPolicy.model_validate(policy) for policy in policies]

    dumped = [policy.model_dump(mode="json", exclude_unset=True) for policy in accepted]
    assert dumped == policies
