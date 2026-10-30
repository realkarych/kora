import pytest

from kora.ir import ToolPolicy
from kora.ir.json import JsonObject
from kora.ir.tool_patterns import EDIT_PATH_NAMES, tool_policy_refusals
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.policy.allow_deny.patterns import FAMILY_NAMES, FILE_EDIT_TOOLS
from tests.ir.tool_policy_cases import byte_boundary_patterns, piece_boundary_patterns


@pytest.mark.parametrize(
    "patterns",
    [
        ["Read", "Bash(git *)"],
        ["(", "Bash()"],
        ["Read"] * 129,
        byte_boundary_patterns() + ["Read"],
        piece_boundary_patterns() + ["Read"],
        ["Write(gates/x\u00e1*)"],
    ],
)
def test_provider_and_core_keep_shared_grammar_results(patterns: list[str]) -> None:
    document: JsonObject = {"deny": [*patterns]}
    policy = ToolPolicy.model_validate(document)

    provider = AllowDenyPolicy().validate(document)
    core = list(tool_policy_refusals(policy))

    assert [(item.path or (), item.message) for item in provider] == [
        (path, refusal.value) for path, refusal in core
    ]


def test_reserved_output_rule_keeps_provider_validation_unchanged() -> None:
    document: JsonObject = {
        "allow": ["StructuredOutput"],
        "deny": ["StructuredOutput(value)"],
    }
    policy = ToolPolicy.model_validate(document)

    provider = AllowDenyPolicy().validate(document)
    core = list(tool_policy_refusals(policy))

    assert provider == []
    assert [path for path, _ in core] == [("allow", 0), ("deny", 0)]


def test_lexical_edit_names_match_unchanged_runtime_table() -> None:
    runtime_names = frozenset(
        (*FAMILY_NAMES, *(tool for tools in FILE_EDIT_TOOLS.values() for tool in tools))
    )

    matched = EDIT_PATH_NAMES == runtime_names

    assert matched
