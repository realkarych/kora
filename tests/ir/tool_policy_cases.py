from kora.ir import GraphDocument
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def policy_agent(policy: dict[str, object], **fields: object) -> dict[str, object]:
    return node(
        "agent",
        config={
            "harness": "claude",
            "prompt": "",
            "workspace": {"repo": ""},
            "tool_policy": policy,
        },
        **fields,
    )


def policy_document(policy: dict[str, object], count: int = 1) -> GraphDocument:
    return document_for(
        nodes={f"n{index}": policy_agent(policy) for index in range(count)}
    )


def byte_boundary_patterns() -> list[str]:
    return ["Bash(" + "a" * 4090 + ")"] * 15 + ["Bash(" + "\U0001f600" * 1022 + "aa)"]


def piece_boundary_patterns() -> list[str]:
    return ["Bash(" + "*a" * 1023 + ")"] * 8


def folding_boundary_patterns(extra: int = 0) -> list[str]:
    return ["Bash(" + "\u1f82" * 4090 + ")"] * 4 + [
        "Bash(" + "\u1f82" * (24 + extra) + ")"
    ]
