from typing import Literal, cast

import pytest
from pydantic import MISSING, BaseModel, Field, StrictBool, StrictStr, ValidationError
from pydantic_core import PydanticCustomError

import kora.ir as ir
from kora.ir.record import IrRecord
from tests.ir.document_cases import INVALID, MINIMAL, POPULATED, document
from tests.ir.support import EXAMPLES, deep_data, load_yaml


def test_ir_document_models_accept_examples() -> None:
    documents = [load_yaml(path) for path in sorted(EXAMPLES.glob("*.ir.yaml"))]

    exported = [
        ir.export_ir(ir.GraphDocument.model_validate(value)) for value in documents
    ]

    assert exported == documents
    assert len(exported) >= 2


def test_ir_document_preserves_omission_and_wire_aliases() -> None:
    value = {"$schema": "not a normalized URI", **document(), "x-meta": False}

    parsed = ir.GraphDocument.model_validate(value)
    exported = ir.export_ir(parsed)

    assert exported == value
    assert parsed.meta is MISSING
    assert list(exported) == [
        "$schema",
        "ir_version",
        "type_contracts",
        "graph",
        "runtime",
        "x-meta",
    ]
    assert ir.render_ir_json(parsed).startswith(
        '{"$schema":"not a normalized URI","ir_version":"1.0",'
    )
    assert "unknown_root_key_count" not in exported


def test_ir_root_unknown_keys_preserve_warning_evidence() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    value = {
        **document(),
        "untrusted secret name": cycle,
        "schema_": object(),
        "x-meta": False,
    }

    count = ir.inspect_ir_root(value)
    parsed = ir.GraphDocument.model_validate(value)
    invalid = {**value, "graph": False}
    with pytest.raises(ValidationError):
        ir.GraphDocument.model_validate(invalid)

    assert count == 2
    assert ir.inspect_ir_root(invalid) == 2
    assert parsed.unknown_root_key_count == 2
    assert ir.export_ir(parsed) == {**document(), "x-meta": False}
    assert (
        "unknown_root_key_count"
        not in ir.GraphDocument.model_json_schema()["properties"]
    )


def test_provenance_snapshots_remain_partial_informational_data() -> None:
    value = {
        "compiler": "",
        "source_digest": "sha256:" + "a" * 64,
        "presets": {"x-preset": {"prompt": "", "unknown": False}, "": {}},
        "defaults": {"global": {"model": ""}, "graph": {}},
        "x-data": {"nested": [False, None]},
        "arbitrary": [None, False],
    }

    parsed = ir.Provenance.model_validate(value)
    exported = ir.export_ir(parsed)
    rendered = ir.render_ir_json(parsed)
    cast("dict[str, object]", value["defaults"])["mutated"] = True
    cast("dict[str, object]", exported["defaults"])["exported"] = True

    assert "mutated" not in cast("dict[str, object]", ir.export_ir(parsed)["defaults"])
    assert "exported" not in cast("dict[str, object]", ir.export_ir(parsed)["defaults"])
    assert '"arbitrary":[null,false]' in rendered
    with pytest.raises(ValidationError):
        ir.Provenance.model_validate({"compiler": False})
    with pytest.raises(PydanticCustomError):
        ir.export_ir(
            cast(
                "ir.Provenance",
                ir.SourceLocation.model_validate({"file": "graph.py", "line": 1}),
            )
        )


@pytest.mark.parametrize("depth", [17, 64, 128, 255, 256, 512, 1024])
def test_document_and_provenance_keep_deep_data(depth: int) -> None:
    data = deep_data(depth)
    value = {**document(), "meta": {"data": data}}
    graph = cast("dict[str, object]", value["graph"])
    graph["params"] = deep_data(depth)

    parsed = ir.GraphDocument.model_validate(value)
    exported = ir.export_ir(parsed)
    rendered = ir.render_ir_json(parsed)

    assert exported["ir_version"] == "1.0"
    assert rendered.count('"x"') == 2 * ((depth + 1) // 2)


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize("name", ["Text", "ns/Text", "x-meta", "Text\n", "text", ""])
def test_document_type_contract_map_schema_matches_admission(
    mode: str, name: str
) -> None:
    import re

    value = {**document(), "type_contracts": {name: {"builtin_revision": "1"}}}
    schema = ir.GraphDocument.model_json_schema(
        mode=cast("Literal['validation', 'serialization']", mode)
    )
    property_schema = cast("dict[str, dict[str, object]]", schema["properties"])[
        "type_contracts"
    ]
    names = cast("dict[str, str]", property_schema["propertyNames"])
    valid = name in ("Text", "ns/Text")

    matches = re.search(names["pattern"], name) is not None
    if valid:
        parsed = ir.GraphDocument.model_validate(value)
        assert ir.export_ir(parsed) == value
    else:
        with pytest.raises(ValidationError):
            ir.GraphDocument.model_validate(value)

    assert matches is valid
    assert "patternProperties" not in property_schema
    assert "additionalProperties" in property_schema


@pytest.mark.parametrize("model", list(MINIMAL))
def test_document_fixtures_round_trip_and_refuse(model: type[BaseModel]) -> None:
    from tests.measurements.ir_document_workload import parsed_root

    values = [MINIMAL[model], POPULATED[model]]

    exported = [ir.export_ir(parsed_root(model, value)) for value in values]
    with pytest.raises(ValidationError):
        model.model_validate(INVALID[model])

    assert exported[0] == values[0]
    expected = {key: item for key, item in values[1].items() if key != "ignored"}
    assert exported[1] == expected


def test_document_measurement_manifest_and_worker_inventory() -> None:
    from tests.measurements.ir_config_lifetime import CONFIGURATIONS
    from tests.measurements.ir_document_workload import (
        graph_inventory,
        materialized_document,
    )
    from tests.measurements.test_ir_models_cost import WORKLOADS

    workloads = [row for name, row in WORKLOADS.items() if name.startswith("document_")]
    flat = graph_inventory(materialized_document(1000))
    wide = graph_inventory(materialized_document(1000, wide=True))
    nested = graph_inventory(materialized_document(1000, nested=True))

    assert len(CONFIGURATIONS) == 53
    assert len(workloads) == 64
    assert sum(row[3] for row in workloads) == 1160
    assert flat["nodes"] == wide["nodes"] == nested["nodes"] == 1000
    assert flat["control_references"] == 999
    assert wide["control_references"] == 1165
    assert nested["control_references"] == 997
    assert nested["kinds"] == {
        "agent": 130,
        "command": 130,
        "human_input": 130,
        "wait_event": 130,
        "timer": 128,
        "branch": 128,
        "fan_out": 112,
        "subgraph": 112,
    }
    assert nested["composition_references"] == 224
    assert nested["producer_references"] == 0


@pytest.mark.parametrize("value", [None, False, "", []])
def test_document_requires_an_object(value: object) -> None:
    with pytest.raises(ValidationError):
        ir.GraphDocument.model_validate(value)


@pytest.mark.parametrize("model", list(MINIMAL))
def test_document_models_preserve_omission_and_refuse_null_fields(
    model: type[BaseModel],
) -> None:
    values = MINIMAL[model]
    fields = model.model_fields

    for name, field in fields.items():
        if name == "extensions":
            continue
        alias = field.alias if field.alias is not None else name
        with pytest.raises(ValidationError):
            model.model_validate({**values, alias: None})


def test_provenance_open_extras_refuse_alias_amplification_and_mutated_data() -> None:
    shared: dict[str, object] = {}
    value = {"arbitrary": {"first": shared, "second": shared}}

    with pytest.raises(ValidationError):
        ir.Provenance.model_validate(value)
    parsed = ir.Provenance.model_validate({"arbitrary": {"data": False}})
    extras = cast("dict[str, object]", parsed.model_extra)
    extras["arbitrary"] = {"runtime": object()}
    with pytest.raises(PydanticCustomError):
        ir.export_ir(parsed)


def test_populated_document_fixtures_cover_optional_fields_and_small_collections() -> (
    None
):
    from tests.ir.document_cases import NODE_MODELS
    from tests.measurements.ir_document_workload import parsed_root

    for model, value in POPULATED.items():
        pending: list[object] = [value]
        identities: set[int] = set()
        while pending:
            item = pending.pop()
            if isinstance(item, dict | list):
                assert id(cast("object", item)) not in identities
                identities.add(id(cast("object", item)))
                if isinstance(item, list):
                    assert len(cast("list[object]", item)) == 2
                    pending.extend(cast("list[object]", item))
                else:
                    pending.extend(cast("dict[str, object]", item).values())
        records: list[object] = [parsed_root(model, value)]
        while records:
            record = records.pop()
            if isinstance(record, BaseModel):
                allowed_absent: set[str] = (
                    {"duration"}
                    if isinstance(record, ir.TimerConfig)
                    else {"event"}
                    if isinstance(record, ir.WaitEventConfig)
                    else set()
                )
                for name in type(record).model_fields:
                    if name != "extensions" and name not in allowed_absent:
                        assert name in record.model_fields_set, (
                            model.__name__,
                            type(record).__name__,
                            name,
                        )
                    field_value = getattr(record, name)
                    named_maps = {
                        "inputs",
                        "outputs",
                        "nodes",
                        "subgraphs",
                        "mcp_servers",
                        "env",
                        "secret_env",
                        "headers",
                        "aliases",
                        "approval_channels",
                        "presets",
                        "type_contracts",
                    }
                    if name in named_maps and isinstance(field_value, dict):
                        expected = (
                            1
                            if name == "outputs"
                            and isinstance(record, ir.HumanInputNode | ir.WaitEventNode)
                            else 2
                        )
                        assert len(cast("dict[str, object]", field_value)) == expected
                    records.append(cast("object", field_value))
            elif isinstance(record, dict):
                records.extend(cast("dict[str, object]", record).values())
            elif isinstance(record, list | tuple):
                records.extend(cast("list[object] | tuple[object, ...]", record))
        if model in NODE_MODELS:
            assert len(cast("dict[str, object]", value["inputs"])) == 2
        for field in ("nodes", "subgraphs", "type_contracts"):
            if field in value:
                assert len(cast("dict[str, object]", value[field])) == 2


def test_provenance_x_members_use_open_data_bounds() -> None:
    payload = "a" * 1048577
    value = {**document(), "meta": {"x-data": payload}}

    parsed = ir.GraphDocument.model_validate(value)
    provenance = ir.Provenance.model_validate({"x-data": payload})
    with pytest.raises(ValidationError):
        ir.GraphDocument.model_validate({**document(), "x-data": payload})

    assert ir.export_ir(provenance) == {"x-data": payload}
    assert ir.export_ir(parsed)["meta"] == {"x-data": payload}
    assert ir.render_ir_json(provenance) == '{"x-data":"' + payload + '"}'


def test_document_canonical_order_matches_independent_exact_bytes() -> None:
    from copy import deepcopy

    node: dict[str, object] = {
        "config": {
            "workspace": {"repo": ""},
            "mcp_servers": {
                "zeta": {"url": "z", "type": "http"},
                "alpha": {"url": "a", "type": "http"},
            },
            "prompt": "",
            "harness": "claude",
        },
        "outputs": {"session": {"type": "AgentSession"}},
        "kind": "agent",
    }
    value: dict[str, object] = {
        "secrets": {"aliases": {"B": "secret://env/b", "A": "secret://env/a"}},
        "runtime": {"vcs": "git", "runtime": "local"},
        "graph": {
            "namespace": "example",
            "id": "graph",
            "nodes": {"zeta": deepcopy(node), "alpha": deepcopy(node)},
            "description": "",
        },
        "type_contracts": {
            "StructuredOutput": {"builtin_revision": "1"},
            "AgentSession": {"builtin_revision": "1"},
        },
        "meta": {"compiler": "test"},
        "ir_version": "1.0",
        "$schema": "raw",
    }
    expected_node = (
        '{"kind":"agent","outputs":{"session":{"type":"AgentSession"}},'
        '"config":{"harness":"claude","prompt":"","mcp_servers":{'
        '"zeta":{"type":"http","url":"z"},"alpha":{"type":"http","url":"a"}},'
        '"workspace":{"repo":""}}}'
    )
    expected = (
        '{"$schema":"raw","ir_version":"1.0","meta":{"compiler":"test"},'
        '"type_contracts":{"StructuredOutput":{"builtin_revision":"1"},'
        '"AgentSession":{"builtin_revision":"1"}},"graph":{"description":"",'
        '"nodes":{"zeta":' + expected_node + ',"alpha":' + expected_node + "},"
        '"id":"graph","namespace":"example"},"runtime":{"runtime":"local",'
        '"vcs":"git"},"secrets":{"aliases":{"B":"secret://env/b","A":"secret://env/a"}}}'
    )

    parsed = ir.GraphDocument.model_validate(value)
    exported = ir.export_ir(parsed)
    graph = cast("dict[str, object]", exported["graph"])
    nodes = cast("dict[str, dict[str, object]]", graph["nodes"])
    config = cast("dict[str, object]", nodes["zeta"]["config"])

    assert list(exported) == [
        "$schema",
        "ir_version",
        "meta",
        "type_contracts",
        "graph",
        "runtime",
        "secrets",
    ]
    assert list(nodes) == ["zeta", "alpha"]
    assert list(cast("dict[str, object]", config["mcp_servers"])) == ["zeta", "alpha"]
    assert ir.render_ir_json(parsed) == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_document_export_keeps_distinct_authored_fields_for_same_record_class(
    reverse: bool,
) -> None:
    value = document()
    graph = cast("dict[str, object]", value["graph"])
    graph["nodes"] = {
        "duration": {"kind": "timer", "config": {"duration": "0s"}},
        "absolute": {
            "kind": "timer",
            "config": {"at": "2024-02-29t23:59:59.120000-00:00"},
        },
    }
    nodes = cast("dict[str, object]", graph["nodes"])
    if reverse:
        graph["nodes"] = dict(reversed(list(nodes.items())))
    duration = '"duration":{"kind":"timer","config":{"duration":"0s"}}'
    absolute = (
        '"absolute":{"kind":"timer","config":{"at":"2024-02-29t23:59:59.120000-00:00"}}'
    )
    entries = absolute + "," + duration if reverse else duration + "," + absolute
    expected = (
        '{"ir_version":"1.0","type_contracts":{},"graph":{"nodes":{' + entries + "},"
        '"id":"graph","namespace":"example"},"runtime":{"runtime":"local","vcs":"git"}}'
    )

    parsed = ir.GraphDocument.model_validate(value)

    assert ir.export_ir(parsed) == value
    assert ir.render_ir_json(parsed) == expected


class ExportAliasedRecord(IrRecord):
    value_: StrictStr = Field(alias="wire")
    enabled: StrictBool | MISSING = MISSING


class ExportConcreteRecord(ExportAliasedRecord):
    value_: StrictStr = Field(alias="concrete")
    extra_: StrictStr = Field(alias="extra")


class ExportRecordCollection(IrRecord):
    records: tuple[IrRecord, ...]


@pytest.mark.parametrize("reverse", [False, True])
def test_export_preserves_concrete_aliases_omissions_and_reused_instances(
    reverse: bool,
) -> None:
    omitted = ExportAliasedRecord.model_validate({"wire": ""})
    authored = ExportAliasedRecord.model_validate({"wire": "", "enabled": False})
    concrete = ExportConcreteRecord.model_validate({"concrete": "value", "extra": ""})
    values = [omitted, authored, authored, concrete]
    expected_values = [
        '{"wire":""}',
        '{"wire":"","enabled":false}',
        '{"wire":"","enabled":false}',
        '{"concrete":"value","extra":""}',
    ]
    if reverse:
        values.reverse()
        expected_values.reverse()
    source = ExportRecordCollection.model_validate({"records": values})
    expected = '{"records":[' + ",".join(expected_values) + "]}"

    exported = ir.export_ir(source)
    rendered = ir.render_ir_json(source)

    assert rendered == expected
    records = cast("list[object]", exported["records"])
    repeated = (1, 2)
    assert records[repeated[0]] == records[repeated[1]]
    assert records[repeated[0]] is not records[repeated[1]]
