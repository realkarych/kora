from dataclasses import replace
from typing import cast

import pytest

from kora.ir.structured_schema_input import (
    PreparedStructuredSchemaInput,
    StructuredSchemaPreparationRefusal,
)
from kora.ir.structured_schema_preparation import prepare_structured_schema_input
from tests.ir.structured_schema_cases import (
    HostileArray,
    HostileDictionary,
    HostileHeader,
    HostileHeaderString,
    builtin,
    contract,
    parameters,
    preparation_input,
    refusal,
)


def hostile_header(kind: str) -> object:
    if kind == "string":
        return HostileHeaderString("authored-header")
    if kind == "object":
        return HostileHeader()
    if kind == "dictionary":
        return HostileDictionary()
    if kind == "array":
        return HostileArray()
    raise ValueError("unknown hostile header case")


@pytest.mark.parametrize(
    "field", ["declared_type", "execution_semantics", "checker_binding"]
)
@pytest.mark.parametrize("kind", ["string", "object", "dictionary", "array"])
def test_preparation_checks_native_header_kinds_before_authored_operations(
    field: str, kind: str
) -> None:
    candidate = replace(preparation_input({}), **{field: hostile_header(kind)})

    reason = refusal(candidate)

    assert reason == (
        "unsupported_schema" if field == "declared_type" else "unsupported_binding"
    )


@pytest.mark.parametrize(
    "change", ["object", "dictionary", "array", "key", "value", "string-value"]
)
def test_preparation_checks_parameter_kinds_before_comparison(change: str) -> None:
    limits: object = parameters()
    if change in {"object", "dictionary", "array"}:
        limits = hostile_header(change)
    elif change == "key":
        limits = {
            HostileHeaderString(key) if key == "max_input_bytes" else key: value
            for key, value in parameters().items()
        }
    else:
        values: dict[str, object] = dict(parameters())
        values["max_input_bytes"] = (
            HostileHeaderString("1048576")
            if change == "string-value"
            else HostileHeader()
        )
        limits = values
    candidate = replace(preparation_input({}), preparation_parameters=limits)

    reason = refusal(candidate)

    assert reason == "unsupported_binding"


@pytest.mark.parametrize(
    "change",
    [
        "record-list",
        "record",
        "name",
        "source",
        "source-kind",
        "source-path",
        "source-segment",
        "source-location",
        "behavior",
        "behavior-key",
        "behavior-value",
    ],
)
def test_preparation_checks_resolved_record_native_kinds_before_callbacks(
    change: str,
) -> None:
    record = contract({})
    records: object = [builtin(), record]
    saved_source = cast("dict[str, object]", record["source"])
    if change == "record-list":
        records = HostileArray()
    elif change == "record":
        records = [builtin(), HostileDictionary()]
    elif change == "name":
        record["name"] = HostileHeaderString("example/Declared")
    elif change == "source":
        record["source"] = HostileDictionary()
    elif change == "source-kind":
        saved_source["kind"] = HostileHeaderString("type_schema")
    elif change == "source-path":
        saved_source["path"] = HostileArray()
    elif change == "source-segment":
        saved_source["path"] = [HostileHeader()]
    elif change == "source-location":
        saved_source["location"] = HostileDictionary()
    elif change == "behavior":
        record["behavior"] = HostileDictionary()
    elif change == "behavior-key":
        record["behavior"] = {HostileHeaderString("validation"): "example.v1"}
    else:
        cast("dict[str, object]", record["behavior"])["validation"] = (
            HostileHeaderString("example.v1")
        )
    candidate = replace(preparation_input({}), type_contracts=records)

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


def test_preparation_rejects_forged_dto_without_metaclass_equality() -> None:
    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            raise AssertionError("authored DTO comparison invoked")

    class Authored(metaclass=AuthoredMeta):
        def __getattr__(self, name: str) -> object:
            raise AssertionError("authored DTO field read invoked")

    candidate = Authored()

    reason = refusal(candidate)

    assert reason == "unsupported_binding"


@pytest.mark.parametrize("field", ["builtin_revision", "descriptor"])
def test_preparation_requires_custom_records_to_keep_builtin_fields_null(
    field: str,
) -> None:
    record = contract({})
    record[field] = "1" if field == "builtin_revision" else {}
    candidate = preparation_input({}, records=[builtin(), record])

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize("change", ["collection", "source-kind", "extra-field"])
def test_preparation_requires_exact_inline_schema_role(change: str) -> None:
    inline: object = {
        "source": {"kind": "inline_schema", "path": [], "location": None},
        "schema": True,
    }
    if change == "collection":
        inline = HostileArray()
    elif change == "source-kind":
        cast("dict[str, object]", cast("dict[str, object]", inline)["source"])[
            "kind"
        ] = "type_schema"
    else:
        cast("dict[str, object]", inline)["reference_targets"] = []
    candidate = preparation_input({}, inline=inline)

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize("sign", [-1, 1], ids=["negative", "positive"])
@pytest.mark.parametrize("bits", [14_000, 14_001], ids=["at", "over"])
def test_preparation_checks_retained_annotation_integer_bits(
    sign: int, bits: int
) -> None:
    candidate = preparation_input({"annotation": sign * ((1 << bits) - 1)})

    result = prepare_structured_schema_input(candidate)

    if bits == 14_000:
        assert isinstance(result, PreparedStructuredSchemaInput)
    else:
        assert isinstance(result, StructuredSchemaPreparationRefusal)
        assert result.reason == "unsupported_schema"
