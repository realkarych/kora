import json
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir import (
    CommandConfig,
    CommandResource,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Workspace,
)
from kora.ir.errors import safe_field_errors
from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES
from tests.ir.config_cases import CONFIG_FIELDS, DIGEST, MINIMAL, POPULATED, RESOURCE
from tests.ir.support import nested

MARKER: str = "confidential-input-7319"
SAFE_MESSAGE: str = "the execution configuration is invalid"
ROUND_TRIPS: list[tuple[type[BaseModel], dict[str, object]]] = [
    *MINIMAL.items(),
    *POPULATED.items(),
    (McpServer, {"type": "http"}),
    (NodeLimits, {"budget_usd": 10**400}),
    (NodeLimits, {"timeout": "0s", "stall_after": "1" * 10000 + "d"}),
    (Workspace, {"repo": "", "from_ref": "a" * 4096}),
    (CommandResource, {**RESOURCE, "size": 0, "name": "a.../e\u0301"}),
    (CommandConfig, {**MINIMAL[CommandConfig], "resources": [RESOURCE]}),
    (CommandConfig, {**MINIMAL[CommandConfig], "resource_set_digest": DIGEST}),
    (
        CommandConfig,
        {
            **MINIMAL[CommandConfig],
            "x-root": [None, False],
            "workspace": {"repo": "", "x-workspace": {"label": "value"}},
            "network_policy": {"x-network": {"egress": []}},
            "resources": [{**RESOURCE, "x-resource": {"size": 0}}],
        },
    ),
]


@pytest.mark.parametrize(("model", "document"), ROUND_TRIPS)
def test_ir_config_models_preserve_omission_and_explicit_values(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    encoded = json.dumps(document)

    parsed = model.model_validate_json(encoded)
    dumped = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)

    assert dumped == document
    assert model.model_validate(dumped) == parsed


INVALID_FIELDS: list[tuple[type[BaseModel], str, object]] = (
    [
        (NodeLimits, "max_events", value)
        for value in (0, 100001, 1.5, True, "1", float("nan"), float("inf"))
    ]
    + [
        (NodeLimits, "budget_usd", value)
        for value in (0, -0.5, True, "1", float("nan"), float("inf"), -float("inf"))
    ]
    + [
        (NodeLimits, "timeout", value)
        for value in ("1", "-1s", "1.0s", "1S", " s", "1s!", 1)
    ]
    + [
        (Workspace, "from_ref", "a" * 4097),
        (Workspace, "repo", 1),
        (NetworkPolicy, "egress", {"host"}),
        (NetworkPolicy, "egress", [1]),
        (McpServer, "type", "sse"),
        (McpServer, "args", "one argument"),
        (McpServer, "args", [False]),
        (McpServer, "headers", []),
        (McpServer, "digest", "sha256:" + "A" * 64),
        (CommandResource, "size", -1),
        (CommandResource, "size", 262145),
        (CommandResource, "size", True),
        (CommandResource, "executable", 0),
        (CommandResource, "executable", "false"),
        (CommandResource, "sha256", "sha256:" + "a" * 63),
        (CommandResource, "sha256", "sha256:" + "a" * 65),
        (CommandConfig, "cmd", []),
        (CommandConfig, "cmd", {"tool"}),
        (CommandConfig, "cmd", [True]),
        (CommandConfig, "workspace", []),
        (CommandConfig, "resources", [RESOURCE] * 65),
        (CommandConfig, "resources", [{**RESOURCE, "size": 262145}]),
        (CommandConfig, "success_exit_codes", [False]),
        (CommandConfig, "success_exit_codes", [0.5]),
        (CommandConfig, "success_exit_codes", ["0"]),
    ]
    + [
        (CommandResource, "name", value)
        for value in (
            "",
            "/a",
            ".",
            "..",
            "a/../b",
            "a/./b",
            "a//b",
            "a/",
            "a\\b",
            "a\x00b",
            "a\x1fb",
            "a\x7fb",
            "a\nb",
        )
    ]
    + [
        (model, name, None)
        for model, document in POPULATED.items()
        for name in document
    ]
    + [
        (model, name, cast("object", {}))
        for model in MINIMAL
        for name in ("unknown", "extensions")
    ]
)


@pytest.mark.parametrize(("model", "field", "value"), INVALID_FIELDS)
def test_ir_config_models_enforce_structural_boundaries(
    model: type[BaseModel], field: str, value: object
) -> None:
    document = {**MINIMAL[model], field: value}

    with pytest.raises(ValidationError):
        model.model_validate(document)


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (model, {key: value for key, value in document.items() if key != required})
        for model, document in MINIMAL.items()
        for required in document
    ],
)
def test_ir_config_models_require_declared_fields(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(document)


@pytest.mark.parametrize(
    ("model", "document", "expected"),
    [
        (NodeLimits, {"max_events": 1.0}, {"max_events": 1}),
        (CommandResource, {**RESOURCE, "size": 262144.0}, RESOURCE),
        (
            CommandConfig,
            {**MINIMAL[CommandConfig], "success_exit_codes": [-1.0, 0, 1e100]},
            {**MINIMAL[CommandConfig], "success_exit_codes": [-1, 0, int(1e100)]},
        ),
    ],
)
def test_ir_config_integer_fields_accept_integral_json_numbers(
    model: type[BaseModel], document: dict[str, object], expected: dict[str, object]
) -> None:
    parsed = model.model_validate(document)

    dumped = parsed.model_dump(mode="json", exclude_unset=True)

    assert dumped == expected


@pytest.mark.parametrize(
    ("model", "field", "entries", "valid"),
    [
        (McpServer, "env", {"lower-key": "value", "x-entry": "value"}, True),
        (McpServer, "headers", {"X-Header": "value", "": "value"}, True),
        (McpServer, "secret_env", {"lower-key": "ALIAS", "x-entry": "TOKEN"}, True),
        (McpServer, "secret_env", {"TOKEN": "lower-alias"}, False),
        (McpServer, "env", {"x-entry": {"ignored": True}}, False),
        (McpServer, "headers", {"x-entry": 1}, False),
        (McpServer, "env", {1: "value"}, False),
        (CommandConfig, "env", {"A": "value", "A" * 64: ""}, True),
        (CommandConfig, "env", {"A" * 65: "value"}, False),
        (CommandConfig, "env", {"_A": "value"}, False),
        (CommandConfig, "env", {"lower-key": "value"}, False),
        (CommandConfig, "env", {"x-entry": "value"}, False),
        (CommandConfig, "secret_env", {"TOKEN": "ALIAS"}, True),
        (CommandConfig, "secret_env", {"lower-key": "ALIAS"}, False),
        (CommandConfig, "secret_env", {"TOKEN": "lower-alias"}, False),
    ],
)
def test_ir_config_maps_keep_their_distinct_contracts(
    model: type[BaseModel], field: str, entries: object, valid: bool
) -> None:
    document = {**MINIMAL[model], field: entries}

    if valid:
        parsed = model.model_validate(document)
        assert parsed.model_dump(mode="json", exclude_unset=True) == document
    else:
        with pytest.raises(ValidationError):
            model.model_validate(document)


@pytest.mark.parametrize("model", MINIMAL)
def test_ir_config_extensions_round_trip(model: type[BaseModel]) -> None:
    document = {**MINIMAL[model], "x-meta": {"items": [None, False, 3, "text"]}}

    parsed = model.model_validate(document)
    dumped = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)

    assert dumped == document


@pytest.mark.parametrize(
    "extension",
    [
        nested(DEFAULT_MAX_DEPTH, "value"),
        "a" * MAX_CANONICAL_BYTES,
        float("nan"),
        {"not-json"},
        "\ud800",
    ],
)
def test_ir_config_nested_extensions_keep_json_bounds(extension: object) -> None:
    document = {
        **MINIMAL[CommandConfig],
        "resources": [{**RESOURCE, "x-meta": extension}],
    }

    with pytest.raises(ValidationError):
        CommandConfig.model_validate(document)


SAFE_FAILURES: list[tuple[type[BaseModel], dict[str, object]]] = [
    (McpServer, {"type": "http", "url": [MARKER], "headers": {MARKER: 1}}),
    (McpServer, {"type": MARKER, "secret_env": {MARKER: MARKER}}),
    (CommandConfig, {**MINIMAL[CommandConfig], "cmd": [{MARKER: MARKER}]}),
    (CommandConfig, {**MINIMAL[CommandConfig], "env": {MARKER: MARKER}}),
    (
        CommandConfig,
        {
            **MINIMAL[CommandConfig],
            "resources": [{**RESOURCE, "name": MARKER + "/../"}],
        },
    ),
    (Workspace, {"repo": "", MARKER: MARKER}),
    (NodeLimits, {"timeout": MARKER}),
    (NetworkPolicy, {"x-" + MARKER: {MARKER: float("nan")}}),
]


@pytest.mark.parametrize(("model", "document"), SAFE_FAILURES)
def test_ir_config_errors_exclude_input_content(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    known = CONFIG_FIELDS

    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)
    safe = safe_field_errors(raised.value, known, SAFE_MESSAGE)

    assert MARKER not in repr(safe)
    assert SAFE_MESSAGE in repr(safe)
