from kora.ir import GraphDocument
from kora.ir.json import JsonObject, JsonValue
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def contracts() -> dict[str, dict[str, object]]:
    return {
        name: {"builtin_revision": "1"}
        for name in ("AgentSession", "StructuredOutput", "VcsRef")
    }


def agent(
    outputs: dict[str, dict[str, object]],
    schema: JsonObject | None = None,
    **fields: object,
) -> dict[str, object]:
    config: dict[str, object] = {
        "harness": "claude",
        "prompt": "",
        "workspace": {"repo": ""},
    }
    if schema is not None:
        config["output_schema"] = schema
    return node(
        "agent",
        config=config,
        outputs={"session": {"type": "AgentSession"}, **outputs},
        **fields,
    )


def human(
    form: JsonObject | None = None,
    schema: JsonObject | None = None,
    **fields: object,
) -> dict[str, object]:
    config: dict[str, object] = {"channel": "cli", "timeout": "0s", "on_expiry": "fail"}
    output: dict[str, object] = {"type": "StructuredOutput"}
    if form is not None:
        config["form"] = form
    if schema is not None:
        output["schema"] = schema
    return node("human_input", config=config, outputs={"response": output}, **fields)


def many_humans(count: int, invalid: bool = False) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": human({}, {"type": "string"} if invalid else {})
            for index in range(count)
        }
    )


def deep_value(depth: int, leaf: JsonValue = 0) -> JsonValue:
    value: JsonValue = leaf
    for _ in range(depth):
        value = [value]
    return value


def shared_ancestry(count: int, depth: int, ending: str = "valid") -> GraphDocument:
    saved = contracts()
    for index in range(depth):
        parent = (
            f"example/T{index + 1}"
            if index + 1 < depth
            else {
                "valid": "StructuredOutput",
                "missing": "example/Missing",
                "cycle": "example/T0",
                "unpublished": "StructuredOutput",
            }[ending]
        )
        saved[f"example/T{index}"] = custom(parent)
    if ending == "unpublished":
        saved["StructuredOutput"] = {"builtin_revision": "unknown"}
    return document_for(
        nodes={
            f"n{index}": agent({"data": {"type": "example/T0", "schema": {}}}, {})
            for index in range(count)
        },
        contracts=saved,
    )


def shared_body(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": node("subgraph", config={"body": "body"})
            for index in range(count)
        },
        bodies={"body": {"nodes": {"gate": human({}, {})}}},
    )
