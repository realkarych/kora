from pydantic import MISSING

from kora.ir import GraphDocument
from tests.ir.graph_link_cases import node


def secret_node(
    kind: str = "agent",
    secret_env: object = MISSING,
    mcp_servers: object = MISSING,
    **fields: object,
) -> dict[str, object]:
    config: dict[str, object] = {
        "workspace": {"repo": ""},
        "secret_env": secret_env,
    }
    if kind == "agent":
        config.update(harness="claude", prompt="", mcp_servers=mcp_servers)
    else:
        config["cmd"] = ["true"]
    return node(kind, config=config, **fields)


def webhook(alias: str, kind: str = "hmac") -> dict[str, object]:
    return {"kind": "webhook", "name": "hook", "auth": {"kind": kind, "secret": alias}}


def secret_document(
    nodes: dict[str, dict[str, object]] | None = None,
    bodies: object = MISSING,
    secrets: object = MISSING,
    triggers: object = MISSING,
    **graph_fields: object,
) -> GraphDocument:
    return GraphDocument.model_validate(
        {
            "ir_version": "1.0",
            "type_contracts": {},
            "graph": {
                "id": "graph",
                "namespace": "example",
                "nodes": {"timer": node()} if nodes is None else nodes,
                "subgraphs": bodies,
                "triggers": triggers,
                **graph_fields,
            },
            "runtime": {"runtime": "local", "vcs": "git"},
            "secrets": secrets,
        }
    )


def all_secret_sites(alias: str) -> dict[str, dict[str, object]]:
    return {
        "agent": secret_node(
            secret_env={"AGENT_TOKEN": alias},
            mcp_servers={
                "stdio": {"type": "stdio", "secret_env": {"MCP_TOKEN": alias}},
                "http": {"type": "http", "secret_env": {"OTHER_TOKEN": alias}},
                "empty": {"type": "http", "secret_env": {}},
                "absent": {"type": "stdio"},
            },
        ),
        "command": secret_node("command", {"COMMAND_TOKEN": alias}),
    }
