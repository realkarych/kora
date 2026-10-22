import pytest

from kora.ir import ToolPolicy

ROUND_TRIPS: list[dict[str, object]] = [
    {},
    {"deny": ["Bash(rm -rf *)"]},
    {"allow": [], "deny": ["Bash(rm -rf *)"]},
    {"allow": ["Read"], "x-owner": "platform"},
    {"x-review": {"required": True, "levels": [1, 2.5, None, "two"]}},
]


@pytest.mark.parametrize("document", ROUND_TRIPS)
def test_tool_policy_extensions_round_trip(document: dict[str, object]) -> None:
    extensions = {key: item for key, item in document.items() if key.startswith("x-")}

    policy = ToolPolicy.model_validate(document)
    dumped = policy.model_dump(mode="json", by_alias=True, exclude_unset=True)

    assert dumped == document
    assert policy.extensions == extensions
    assert ToolPolicy.model_validate(dumped) == policy
