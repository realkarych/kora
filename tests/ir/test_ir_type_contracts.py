import pytest
from pydantic import TypeAdapter, ValidationError

from kora.ir import BuiltinTypeContract, CustomTypeContract, TypeContract, export_ir
from kora.ir.scalars import TypeContractName
from tests.ir.declaration_cases import BEHAVIOR, DIALECT


def test_ir_type_contract_declarations_are_structural() -> None:
    adapter = TypeAdapter(TypeContract)
    builtin = {"builtin_revision": "uninstalled/revision"}
    custom = {
        "schema": {
            "$schema": DIALECT,
            "$ref": "https://not-resolved.example/schema",
            "properties": {"allowed": True, "blocked": False},
        },
        "parent": "unknown/Parent",
        "behavior": {
            "validation": "import:untrusted",
            "digest_fields": "file:///private/key",
            "summary": "unavailable",
        },
    }

    parsed = [adapter.validate_python(document) for document in (builtin, custom)]

    assert type(parsed[0]) is BuiltinTypeContract
    assert type(parsed[1]) is CustomTypeContract
    assert [export_ir(record) for record in parsed] == [builtin, custom]


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"$schema": None},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        True,
        [],
    ],
)
def test_ir_custom_contract_requires_literal_object_dialect(schema: object) -> None:
    document = {"schema": schema, "parent": None, "behavior": BEHAVIOR}

    with pytest.raises(ValidationError):
        CustomTypeContract.model_validate(document)


def test_ir_custom_contract_parent_is_required_nullable() -> None:
    document = {"schema": {"$schema": DIALECT}, "behavior": BEHAVIOR}

    with pytest.raises(ValidationError):
        CustomTypeContract.model_validate(document)
    parsed = CustomTypeContract.model_validate({**document, "parent": None})

    assert export_ir(parsed) == {**document, "parent": None}


@pytest.mark.parametrize("name", ["x-meta", "Text\n", "custom/Text\n", "List[Text]"])
def test_ir_type_contract_map_applies_name_rules_to_every_entry(name: str) -> None:
    adapter = TypeAdapter(dict[TypeContractName, TypeContract])
    document = {name: {"builtin_revision": "1"}}

    with pytest.raises(ValidationError):
        adapter.validate_python(document)


def test_ir_type_contract_map_preserves_order_and_refuses_noncontract_values() -> None:
    adapter = TypeAdapter(dict[TypeContractName, TypeContract])
    document = {
        "custom/Second": {"builtin_revision": "2"},
        "First": {"builtin_revision": "1"},
    }

    parsed = adapter.validate_python(document)
    with pytest.raises(ValidationError):
        adapter.validate_python({"Text": False})

    assert list(parsed) == ["custom/Second", "First"]
    assert {name: export_ir(value) for name, value in parsed.items()} == document
