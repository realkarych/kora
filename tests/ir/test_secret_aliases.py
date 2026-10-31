import gc
import json
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import AgentNode, GraphDocument, export_ir, validate_secret_aliases
from kora.ir.secret_aliases import SecretAliasChecks
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import node
from tests.ir.secret_alias_cases import (
    all_secret_sites,
    secret_document,
    secret_node,
    webhook,
)


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize(
    "site", ["agent", "command", "mcp_stdio", "mcp_http", "hmac", "bearer"]
)
def test_secret_alias_membership_covers_every_operational_site(
    site: str, valid: bool
) -> None:
    alias = "PRIVATE_ALIAS"
    nodes = {"subject": node()}
    triggers: list[dict[str, object]] = []
    path: tuple[str | int, ...]
    if site in ("agent", "command"):
        nodes = {"subject": secret_node(site, {"TOKEN": alias})}
        path = ("graph", "nodes", "subject", "config", "secret_env", "TOKEN")
    elif site.startswith("mcp_"):
        nodes = {
            "subject": secret_node(
                mcp_servers={
                    "service": {
                        "type": site[4:],
                        "secret_env": {"TOKEN": alias},
                    }
                }
            )
        }
        path = (
            "graph",
            "nodes",
            "subject",
            "config",
            "mcp_servers",
            "<field>",
            "secret_env",
            "<field>",
        )
    else:
        triggers = [webhook(alias, site)]
        path = ("graph", "triggers", 0, "auth", "secret")
    document = secret_document(
        nodes,
        secrets={"aliases": {alias if valid else "UNUSED": "secret://unknown/VALUE"}},
        triggers=triggers,
    )
    before = export_ir(document)

    result = validate_secret_aliases(document)

    assert result.secret_aliases_valid is valid
    assert result.references_checked == result.nodes_checked == 1
    assert result.triggers_checked == len(triggers)
    assert result.mcp_servers_checked == int(site.startswith("mcp_"))
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == "IR007"
        assert error.message == "The secret alias is not declared."
        assert error.path == path
        assert error.node == (None if triggers else "subject")
        assert error.location is error.offset is error.hint is None
        assert alias not in repr(error)


@pytest.mark.parametrize("secrets", [MISSING, {}, {"aliases": {}}])
def test_missing_or_empty_alias_declarations_refuse_references(secrets: object) -> None:
    document = secret_document(
        all_secret_sites("MISSING"),
        secrets=secrets,
        triggers=[webhook("MISSING"), webhook("MISSING", "bearer")],
    )

    result = validate_secret_aliases(document)

    assert not result.secret_aliases_valid
    assert result.references_checked == len(result.diagnostics.errors) == 6
    assert result.mcp_servers_checked == 4
    assert result.triggers_checked == 2


def test_declared_aliases_can_be_reused_exactly() -> None:
    document = secret_document(
        all_secret_sites("SERVICE"),
        secrets={"aliases": {"SERVICE": "secret://unavailable/OTHER"}},
        triggers=[webhook("SERVICE"), webhook("SERVICE", "bearer"), webhook("OTHER")],
    )
    assert document.secrets is not MISSING
    aliases = document.secrets.aliases
    nodes = document.graph.nodes
    agent = nodes["agent"]
    assert isinstance(agent, AgentNode)
    servers = agent.config.mcp_servers
    environment = agent.config.secret_env
    before = export_ir(document)

    result = validate_secret_aliases(document)

    assert not result.secret_aliases_valid
    assert result.references_checked == 7
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "triggers",
        2,
        "auth",
        "secret",
    )
    assert document.secrets.aliases is aliases
    assert document.graph.nodes is nodes
    assert agent.config.mcp_servers is servers
    assert agent.config.secret_env is environment
    assert export_ir(document) == before


@pytest.mark.parametrize("shared", [False, True])
def test_stored_secret_references_are_visited_once_without_expansion(
    shared: bool,
) -> None:
    document = secret_document(
        nodes={
            f"call{index}": node("subgraph", config={"body": "stored0"})
            for index in range(1000 if shared else 1)
        },
        bodies={
            f"stored{index}": {
                "nodes": {"agent": secret_node(secret_env={"TOKEN": "ABSENT"})}
            }
            for index in range(1 if shared else 1000)
        },
    )

    result = validate_secret_aliases(document)

    assert not result.secret_aliases_valid
    assert result.nodes_checked == 1001
    assert (
        result.definitions_checked
        == result.references_checked
        == (1 if shared else 1000)
    )
    assert len(result.diagnostics.errors) == result.references_checked
    assert result.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "stored0",
        "nodes",
        "agent",
        "config",
        "secret_env",
        "TOKEN",
    )


def test_secret_alias_faults_remain_independent() -> None:
    document = secret_document(
        all_secret_sites("ABSENT"),
        bodies={
            "unused": {
                "nodes": {"command": secret_node("command", {"TOKEN": "ABSENT"})}
            }
        },
        triggers=[webhook("ABSENT"), {"kind": "manual"}, webhook("ABSENT", "bearer")],
    )

    result = validate_secret_aliases(document)

    assert not result.secret_aliases_valid
    assert result.references_checked == len(result.diagnostics.errors) == 7
    assert result.nodes_checked == 3
    assert result.definitions_checked == 1
    assert result.triggers_checked == 3
    assert result.mcp_servers_checked == 4
    assert result.diagnostics.errors[-1].path == (
        "graph",
        "triggers",
        2,
        "auth",
        "secret",
    )


@pytest.mark.parametrize("kind", list(NODE_CASES))
def test_opaque_content_has_no_alias_obligation(kind: str) -> None:
    subject = node(kind, **{"x-metadata": {"secret_env": {"TOKEN": "UNDECLARED"}}})
    document = secret_document(
        {"subject": subject},
        secrets={"aliases": {"UNUSED": "secret://unavailable/NO_PROVIDER"}},
        params={"secret_env": {"TOKEN": "UNDECLARED"}},
        triggers=[
            {"kind": "manual", "params": {"secret_env": {"TOKEN": "UNDECLARED"}}}
        ],
    )
    raw = export_ir(document)
    raw["meta"] = {"presets": {"private": {"secret_env": {"TOKEN": "UNDECLARED"}}}}
    document = GraphDocument.model_validate(raw)

    result = validate_secret_aliases(document)

    assert result.secret_aliases_valid
    assert result.references_checked == result.mcp_servers_checked == 0
    assert result.nodes_checked == result.triggers_checked == 1


@pytest.mark.parametrize("empty", [MISSING, {}])
def test_empty_environments_and_all_trigger_kinds_are_counted(empty: object) -> None:
    triggers: list[dict[str, object]] = [
        {"kind": "manual"},
        {"kind": "cron", "expr": "0 6 * * 1"},
        {
            "kind": "sensor",
            "poll": {"sensor": "http", "config": {}, "until": "true"},
            "interval": "1s",
        },
        {"kind": "graph", "graph": "upstream", "on": "run_succeeded"},
    ]
    document = secret_document(
        {
            "agent": secret_node(
                secret_env=empty,
                mcp_servers={"service": {"type": "http", "secret_env": empty}},
            ),
            "command": secret_node("command", empty),
        },
        triggers=triggers,
    )

    result = validate_secret_aliases(document)

    assert result.secret_aliases_valid
    assert result.nodes_checked == 2
    assert result.references_checked == result.definitions_checked == 0
    assert result.triggers_checked == 4
    assert result.mcp_servers_checked == 1


def test_plain_environment_values_and_nonmembership_policies_are_out_of_scope() -> None:
    document = secret_document(
        {
            "subject": node(
                "command",
                config={
                    "cmd": ["PRIVATE_COMMAND"],
                    "workspace": {"repo": "PRIVATE_REPOSITORY"},
                    "env": {"TOKEN": "PRIVATE_ENV_VALUE"},
                    "secret_env": {"HOME": "SERVICE"},
                    "network_policy": {"egress": []},
                },
            )
        },
        secrets={"aliases": {"SERVICE": "secret://unavailable/PRIVATE_REFERENCE"}},
    )
    before = export_ir(document)

    result = validate_secret_aliases(document)

    assert result.secret_aliases_valid
    assert result.references_checked == 1
    assert "PRIVATE" not in repr(result)
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "source",
    [
        MISSING,
        "node.py",
        "/private/source",
        "https://invalid/source",
        "s" * 1025,
        "\ud800",
    ],
)
@pytest.mark.parametrize("site", ["agent", "mcp", "body", "webhook"])
def test_secret_alias_diagnostics_preserve_safe_owning_locations(
    source: object, site: str
) -> None:
    metadata = MISSING if source is MISSING else {"file": source, "line": 7}
    subject = secret_node(
        secret_env=MISSING if site == "mcp" else {"TOKEN": "ABSENT"},
        mcp_servers={"service": {"type": "http", "secret_env": {"TOKEN": "ABSENT"}}}
        if site == "mcp"
        else MISSING,
        source=metadata,
    )
    document = secret_document(
        {"subject": node()} if site in ("body", "webhook") else {"subject": subject},
        bodies={
            "unused": {
                "source": {"file": "body.py", "line": 2},
                "nodes": {"subject": subject},
            }
        }
        if site == "body"
        else MISSING,
        triggers=[webhook("ABSENT")] if site == "webhook" else MISSING,
        source=metadata if site == "webhook" else {"file": "ancestor.py", "line": 1},
    )

    result = validate_secret_aliases(document)

    assert len(result.diagnostics.errors) == 1
    error = result.diagnostics.errors[0]
    assert error.node == (None if site == "webhook" else "subject")
    assert error.offset is None
    if source == "node.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 7)
    else:
        assert error.location is None


@pytest.mark.parametrize(
    "key",
    [
        "TOKEN",
        "config",
        "secret_env",
        "secret://provider/private",
        "https://private.invalid/token",
        "\ud800",
        "s" * 10000,
    ],
)
def test_mcp_map_keys_never_enter_diagnostics(key: str) -> None:
    document = secret_document(
        {
            "subject": secret_node(
                mcp_servers={
                    key: {"type": "stdio", "secret_env": {key: "PRIVATE_ALIAS"}}
                }
            )
        }
    )

    result = validate_secret_aliases(document)

    assert not result.secret_aliases_valid
    assert result.diagnostics.errors[0].model_dump() == {
        "code": "IR007",
        "message": "The secret alias is not declared.",
        "path": (
            "graph",
            "nodes",
            "subject",
            "config",
            "mcp_servers",
            "<field>",
            "secret_env",
            "<field>",
        ),
        "location": None,
        "offset": None,
        "node": "subject",
        "hint": None,
    }
    assert "PRIVATE_ALIAS" not in repr(result)


@pytest.mark.parametrize("source_length", [0, 1000])
def test_secret_alias_diagnostics_preserve_collection_bounds(
    source_length: int,
) -> None:
    count = 3000 if source_length == 0 else 1000
    document = secret_document(
        {
            "n" * 64: secret_node(
                secret_env={f"TOKEN{index}": "ABSENT" for index in range(count)},
                source=MISSING
                if not source_length
                else {"file": "s" * source_length, "line": 1},
            )
        }
    )

    result = validate_secret_aliases(document)
    payload = json.dumps(
        {
            "errors": [
                item.model_dump(mode="json") for item in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.secret_aliases_valid
    assert result.references_checked == count
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == count
    assert 0 < len(result.diagnostics.errors) <= 1000
    assert result.diagnostics.errors_omitted > 0
    assert len(payload) <= 1_048_576
    assert b"ABSENT" not in payload


@pytest.mark.parametrize("valid", [False, True])
def test_secret_alias_validation_releases_inputs_and_results(valid: bool) -> None:
    document = secret_document(
        {
            name: {**value, "source": {"file": "node.py", "line": 2}}
            for name, value in all_secret_sites("SERVICE").items()
        },
        secrets={
            "aliases": {"SERVICE" if valid else "OTHER": "secret://unavailable/PRIVATE"}
        },
        triggers=[webhook("SERVICE")],
        source={"file": "graph.py", "line": 1},
    )
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(document.secrets),
    ]
    references.extend(weakref.ref(value) for value in document.graph.nodes.values())
    references.extend(
        weakref.ref(value.config) for value in document.graph.nodes.values()
    )
    agent = document.graph.nodes["agent"]
    assert isinstance(agent, AgentNode)
    assert agent.config.mcp_servers is not MISSING
    references.extend(
        weakref.ref(server) for server in agent.config.mcp_servers.values()
    )
    references.append(weakref.ref(agent.source))
    del agent

    result = validate_secret_aliases(document)
    diagnostic_references: list[Callable[[], object | None]] = [
        weakref.ref(item) for item in result.diagnostics.errors
    ]
    diagnostic_references.extend(
        weakref.ref(item.location) for item in result.diagnostics.errors
    )
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    retained_checks = sum(
        isinstance(value, SecretAliasChecks) for value in gc.get_objects()
    )
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert retained_checks == 0
    assert bool(diagnostic_references) is not valid
    assert all(reference() is None for reference in diagnostic_references)
    assert result_reference() is None
