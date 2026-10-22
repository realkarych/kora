import functools
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest

from kora.ir.json import JsonObject
from kora.observability import LoggingSettings, ProcessDiagnostics
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import (
    ProtocolName,
    ProviderMetadata,
    ProviderMetadataError,
)
from tests.protocols.conftest import StartCapture
from tests.protocols.entry_points import (
    entry,
    implementation,
    metadata_factory,
    returning,
    source,
)

RUNTIME: ProtocolName = ProtocolName.RUNTIME
SENTINEL: str = "sentinel-0d9e4b61"


def nested_schema(depth: int) -> dict[str, object]:
    schema: dict[str, object] = {"type": "object"}
    for _ in range(depth - 1):
        schema = {"type": "object", "properties": schema}
    return schema


def metadata_with(**overrides: object) -> Callable[[], object]:
    values: dict[str, object] = {
        "protocol": RUNTIME,
        "name": "alpha",
        "implementation": implementation,
        "config_schema": {"type": "object"},
        "version": "1.0",
    }
    values.update(overrides)

    def factory() -> object:
        return ProviderMetadata(
            protocol=cast("ProtocolName", values["protocol"]),
            name=cast("str", values["name"]),
            implementation=cast("Callable[..., object]", values["implementation"]),
            config_schema=cast("JsonObject", values["config_schema"]),
            version=cast("str", values["version"]),
        )

    return factory


RULE_CASES: list[tuple[str, Callable[[], object]]] = [
    ("protocol", metadata_with(protocol="runtime")),
    ("name", metadata_with(name="Alpha")),
    ("implementation", metadata_with(implementation="not callable")),
    ("config_schema.json_object", metadata_with(config_schema=["type"])),
    ("config_schema.type", metadata_with(config_schema={"type": "string"})),
    ("config_schema.json_key", metadata_with(config_schema={"type": "object", 1: 2})),
    (
        "config_schema.json_float",
        metadata_with(config_schema={"type": "object", "x": math.nan}),
    ),
    (
        "config_schema.json_scalar",
        metadata_with(config_schema={"type": "object", "x": {1, 2}}),
    ),
    ("config_schema.json_depth", metadata_with(config_schema=nested_schema(17))),
    ("version", metadata_with(version="")),
    ("version", metadata_with(version="v" * 65)),
    ("version", metadata_with(version="1.0\n")),
]


@pytest.fixture
def diagnostics(start_capture: StartCapture) -> ProcessDiagnostics:
    return start_capture(LoggingSettings()).diagnostics


def load_alpha(load: Callable[[], object], diagnostics: ProcessDiagnostics) -> object:
    return load_provider(
        RUNTIME, "alpha", diagnostics, source(entry("runtime.alpha", load))
    )


def refusal(
    load: Callable[[], object], diagnostics: ProcessDiagnostics
) -> KoraProtocolError:
    with pytest.raises(KoraProtocolError) as raised:
        load_alpha(load, diagnostics)
    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


@pytest.mark.parametrize(("rule", "factory"), RULE_CASES)
def test_provider_metadata_is_validated(
    rule: str, factory: Callable[[], object], diagnostics: ProcessDiagnostics
) -> None:
    load = returning(factory)

    error = refusal(load, diagnostics)

    assert error.code == "provider_metadata_invalid"
    assert error.details == {"protocol": "runtime", "name": "alpha", "rule": rule}


def test_provider_metadata_accepts_the_deepest_allowed_schema(
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(metadata_with(config_schema=nested_schema(16)))

    loaded = load_alpha(load, diagnostics)

    assert isinstance(loaded, ProviderMetadata)


def test_provider_entry_point_must_return_a_metadata_factory(
    diagnostics: ProcessDiagnostics,
) -> None:
    not_callable = returning("a string")
    wrong_result = returning(returning({"protocol": "runtime"}))

    not_callable_error = refusal(not_callable, diagnostics)
    wrong_result_error = refusal(wrong_result, diagnostics)

    assert not_callable_error.code == "provider_metadata_invalid"
    assert not_callable_error.details["rule"] == "factory"
    assert wrong_result_error.code == "provider_metadata_invalid"
    assert wrong_result_error.details["rule"] == "factory_result"


def test_provider_metadata_must_match_its_entry_point(
    diagnostics: ProcessDiagnostics,
) -> None:
    other_name = returning(metadata_factory(RUNTIME, "beta"))
    other_protocol = returning(metadata_factory(ProtocolName.HARNESS, "alpha"))

    name_error = refusal(other_name, diagnostics)
    protocol_error = refusal(other_protocol, diagnostics)

    assert name_error.code == "provider_metadata_mismatch"
    assert protocol_error.code == "provider_metadata_mismatch"


def test_provider_metadata_keeps_its_own_schema_copy() -> None:
    properties: JsonObject = {"depth": {"type": "integer"}}
    schema: JsonObject = {"type": "object", "properties": properties}

    metadata = ProviderMetadata(RUNTIME, "alpha", implementation, schema, "1.0")
    properties["depth"] = {"type": "string"}
    schema["type"] = "array"

    assert metadata.config_schema == {
        "type": "object",
        "properties": {"depth": {"type": "integer"}},
    }


def test_unprefixed_entry_point_gets_a_hint(diagnostics: ProcessDiagnostics) -> None:
    legacy = source(entry("alpha", returning(metadata_factory(RUNTIME, "alpha"))))

    with pytest.raises(KoraProtocolError) as raised:
        load_provider(RUNTIME, "alpha", diagnostics, legacy)

    assert raised.value.code == "provider_not_installed"
    assert raised.value.details["hint"] == "entry points are named <protocol>.<name>"


def test_implementation_from_a_reserved_module_is_refused(
    diagnostics: ProcessDiagnostics,
) -> None:
    reserved = type("Implementation", (), {"__module__": "kora.testing.x"})

    def factory() -> ProviderMetadata:
        return ProviderMetadata(RUNTIME, "alpha", reserved, {"type": "object"}, "1.0")

    error = refusal(returning(factory), diagnostics)

    assert error.code == "provider_reserved"


@dataclass(frozen=True)
class UncheckedMetadata(ProviderMetadata):
    def __post_init__(self) -> None:
        return None


class AttributeRaising:
    def __call__(self, configuration: object) -> object:
        return configuration

    def __getattribute__(self, name: str) -> object:
        if name == "__module__":
            raise RuntimeError(SENTINEL)
        return object.__getattribute__(self, name)


def reserved_partial_factory() -> ProviderMetadata:
    reserved = type("Implementation", (), {"__module__": "kora.testing.x"})
    wrapped = functools.partial(reserved)
    return ProviderMetadata(RUNTIME, "alpha", wrapped, {"type": "object"}, "1.0")


def attribute_raising_factory() -> ProviderMetadata:
    return ProviderMetadata(
        RUNTIME, "alpha", AttributeRaising(), {"type": "object"}, "1.0"
    )


def unchecked_factory() -> ProviderMetadata:
    return UncheckedMetadata(
        RUNTIME, "alpha", implementation, cast("JsonObject", "not a schema"), "v\n"
    )


class UnhashableRuleError(ProviderMetadataError):
    def __init__(self) -> None:
        super().__init__(SENTINEL)
        self.rule = cast("str", [SENTINEL])


def provider_rule_factory() -> ProviderMetadata:
    raise ProviderMetadataError(SENTINEL)


def unhashable_rule_factory() -> ProviderMetadata:
    raise UnhashableRuleError


def long_type_factory() -> ProviderMetadata:
    kind = type("Failure", (RuntimeError,), {"__qualname__": "x" * 300})
    raise kind(SENTINEL)


def test_provider_metadata_subclass_is_refused(
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(unchecked_factory)

    error = refusal(load, diagnostics)

    assert error.code == "provider_metadata_invalid"
    assert error.details["rule"] == "factory_result"


@pytest.mark.parametrize(
    ("factory", "type_name"),
    [
        (provider_rule_factory, "kora.protocols.metadata.ProviderMetadataError"),
        (unhashable_rule_factory, f"{__name__}.UnhashableRuleError"),
    ],
)
def test_provider_metadata_rule_text_from_a_provider_is_not_trusted(
    factory: Callable[[], ProviderMetadata],
    type_name: str,
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(factory)

    error = refusal(load, diagnostics)

    assert error.code == "provider_load_failed"
    assert error.details["exception_type"] == type_name
    assert SENTINEL not in json.dumps(error.details)


def test_provider_exception_type_name_is_bounded(
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(long_type_factory)

    error = refusal(load, diagnostics)

    assert error.details["exception_type"] == "builtins.RuntimeError"


def test_reserved_implementation_behind_a_partial_is_refused(
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(reserved_partial_factory)

    error = refusal(load, diagnostics)

    assert error.code == "provider_reserved"


def test_implementation_attribute_failure_is_structured(
    diagnostics: ProcessDiagnostics,
) -> None:
    load = returning(attribute_raising_factory)

    error = refusal(load, diagnostics)

    assert error.code == "provider_load_failed"
    assert SENTINEL not in json.dumps(error.details)


def test_unparsable_entry_point_value_is_refused_before_import(
    diagnostics: ProcessDiagnostics,
) -> None:
    imported: list[str] = []

    def load() -> object:
        imported.append("imported")
        return metadata_factory(RUNTIME, "alpha")

    malformed = source(entry("runtime.alpha", load, module=None))

    with pytest.raises(KoraProtocolError) as raised:
        load_provider(RUNTIME, "alpha", diagnostics, malformed)

    assert raised.value.code == "provider_metadata_invalid"
    assert raised.value.details["rule"] == "entry_point_value"
    assert imported == []
