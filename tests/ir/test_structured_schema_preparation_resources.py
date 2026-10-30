import gc
import os
import weakref
from dataclasses import fields, replace
from typing import cast

import pytest

import kora.ir.structured_schema_input as input_module
import kora.ir.structured_schema_locations as locations_module
import kora.ir.structured_schema_preparation as preparation_module
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from kora.ir.structured_schema_preparation import prepare_structured_schema_input
from kora.ir.structured_schema_work import (
    PreparationMeter,
    PreparationRefused,
    copy_input_vector,
)
from tests.ir.structured_schema_cases import (
    HostileHeader,
    builtin,
    closed_documents,
    contract,
    normalized_input,
    parameters,
    preparation_input,
    prepared,
    reachable_container_ids,
    refusal,
)
from tests.support.observability import run_python


@pytest.mark.parametrize("count", [0, 1, 3], ids=["empty", "singleton", "several"])
def test_generated_input_vector_copy_has_exact_owned_index_reservations(
    count: int,
) -> None:
    original = list(range(count))
    expected = tuple(original)
    meter = PreparationMeter()

    copied = copy_input_vector(original, meter)
    original.clear()

    assert copied == expected
    assert meter.work == 1 + count
    assert meter.auxiliary_entries == count
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("dimension", ["work", "entries"])
@pytest.mark.parametrize("delta", [0, 1], ids=["exact", "one-short"])
def test_generated_input_vector_copy_reserves_before_exceeding_capacity(
    dimension: str, delta: int
) -> None:
    original = [0, 1, 2]
    meter = PreparationMeter(
        work=parameters()["max_preparation_work"] - 4 + delta
        if dimension == "work"
        else 0,
        auxiliary_entries=parameters()["max_auxiliary_entries"] - 3 + delta
        if dimension == "entries"
        else 0,
    )
    before = (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries)

    if delta:
        with pytest.raises(PreparationRefused) as rejected:
            copy_input_vector(original, meter)

        assert rejected.value.reason == "preparation_exhausted"
        assert (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries) == before
        assert original == [0, 1, 2]
        return
    copied = copy_input_vector(original, meter)

    assert copied == (0, 1, 2)
    assert (
        meter.work if dimension == "work" else meter.auxiliary_entries
    ) == parameters()[
        "max_preparation_work" if dimension == "work" else "max_auxiliary_entries"
    ]
    assert original == [0, 1, 2]


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_preparation_accounts_generated_wrappers_in_the_complete_byte_limit(
    delta: int,
) -> None:
    schema: dict[str, object] = {"annotation": ""}
    baseline = prepared(preparation_input(schema))
    target = parameters()["max_input_bytes"] + delta
    schema["annotation"] = "x" * (target - baseline.accounting.input_bytes)
    candidate = preparation_input(schema)

    if delta > 0:
        reason = refusal(candidate)

        assert reason == "preparation_exhausted"
        return
    result = prepared(candidate)

    assert result.accounting.input_bytes == target


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_preparation_accounts_generated_wrappers_in_the_complete_node_limit(
    delta: int,
) -> None:
    schema: dict[str, object] = {"annotation": []}
    baseline = prepared(preparation_input(schema))
    target = parameters()["max_input_nodes"] + delta
    schema["annotation"] = [None] * (target - baseline.accounting.input_nodes)
    candidate = preparation_input(schema)

    if delta > 0:
        reason = refusal(candidate)

        assert reason == "preparation_exhausted"
        return
    result = prepared(candidate)

    assert result.accounting.input_nodes == target


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_preparation_accounts_generated_wrappers_in_container_depth(delta: int) -> None:
    target = parameters()["max_input_depth"] + delta
    annotation: object = []
    for _ in range(target - 5):
        annotation = [annotation]
    candidate = preparation_input({"annotation": annotation})

    if delta > 0:
        reason = refusal(candidate)

        assert reason == "preparation_exhausted"
        return
    result = prepared(candidate)

    assert result.accounting.input_depth == target


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_preparation_bounds_the_complete_original_document_closure(delta: int) -> None:
    count = parameters()["max_schema_documents"] + delta
    records = [builtin()]
    parent = "StructuredOutput"
    for index in range(count - 1):
        name = f"example/Type{index:02d}"
        records.append(contract(True, name, parent))
        parent = name
    if delta > 0:
        records[0]["schema"] = HostileHeader()
    candidate = preparation_input({}, records=records, declared_type=parent)

    if delta > 0:
        reason = refusal(candidate)

        assert reason == "preparation_exhausted"
        return
    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert result.accounting.documents == count
    assert result.accounting.positions == count
    assert normalized["constraint_documents"] == list(range(count))


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_preparation_bounds_all_schema_positions_including_boolean_children(
    delta: int,
) -> None:
    count = parameters()["max_schema_positions"] + delta
    candidate = preparation_input({"allOf": [True] * (count - 2)})

    if delta > 0:
        reason = refusal(candidate)

        assert reason == "preparation_exhausted"
        return
    result = prepared(candidate)

    assert result.accounting.positions == count


def test_preparation_does_not_reset_work_between_original_documents() -> None:
    branch = {f"{index:04d}" + "x" * 396: None for index in range(512)}
    one = prepared(preparation_input({"annotation": branch}))
    candidate = closed_documents([{"annotation": branch}] * 4)

    reason = refusal(candidate)

    assert one.accounting.work * 4 > parameters()["max_preparation_work"]
    assert one.accounting.input_bytes * 4 < parameters()["max_input_bytes"]
    assert one.accounting.input_nodes * 4 < parameters()["max_input_nodes"]
    assert reason == "preparation_exhausted"


def test_preparation_does_not_reset_auxiliary_entries_between_documents() -> None:
    branch = {f"key_{index:04d}": None for index in range(1_500)}
    one = prepared(preparation_input({"annotation": branch}))
    candidate = closed_documents([{"annotation": branch}] * 4)

    reason = refusal(candidate)

    assert one.accounting.auxiliary_entries * 4 > parameters()["max_auxiliary_entries"]
    assert one.accounting.input_nodes * 4 < parameters()["max_input_nodes"]
    assert one.accounting.input_bytes * 4 < parameters()["max_input_bytes"]
    assert reason == "preparation_exhausted"


def test_preparation_does_not_reset_numeric_scratch_between_documents() -> None:
    integer = (1 << parameters()["max_numeric_bits"]) - 1
    one = prepared(preparation_input({"annotation": integer}))
    candidate = closed_documents([{"annotation": integer}] * 12)

    reason = refusal(candidate)

    assert one.accounting.auxiliary_bytes * 12 > parameters()["max_auxiliary_bytes"]
    assert one.accounting.work * 12 < parameters()["max_preparation_work"]
    assert one.accounting.input_nodes * 12 < parameters()["max_input_nodes"]
    assert reason == "preparation_exhausted"


@pytest.mark.parametrize("invalid", [False, True], ids=["success", "refusal"])
def test_preparation_releases_call_state_without_retaining_caller_containers(
    invalid: bool,
) -> None:
    schema: dict[str, object] = {"annotation": ["saved", {"value": 0}]}
    if invalid:
        schema["pattern"] = "unsupported"
    candidate = preparation_input(schema)
    original_ids = reachable_container_ids(candidate)

    results = [prepare_structured_schema_input(candidate) for _ in range(2)]
    retained_ids = set[int]().union(
        *(reachable_container_ids(result) for result in results)
    )
    global_ids = set[int]().union(
        *(
            reachable_container_ids(vars(module))
            for module in (input_module, locations_module, preparation_module)
        )
    )

    assert original_ids.isdisjoint(retained_ids)
    assert original_ids.isdisjoint(global_ids)
    if invalid:
        assert all(
            [field.name for field in fields(result)] == ["reason"] for result in results
        )


@pytest.mark.parametrize("phase", ["header", "snapshot"])
def test_preparation_refusal_releases_caller_objects_after_its_exception_scope(
    phase: str,
) -> None:
    original = HostileHeader()
    retained = weakref.ref(original)
    candidate = (
        replace(preparation_input({}), checker_binding=original)
        if phase == "header"
        else preparation_input({"annotation": original})
    )

    result = prepare_structured_schema_input(candidate)
    del candidate
    del original
    gc.collect()

    assert isinstance(result, StructuredSchemaPreparationRefusal)
    assert result.reason == (
        "unsupported_binding" if phase == "header" else "unsupported_schema"
    )
    assert retained() is None


def test_preparation_normalized_form_and_accounting_are_hash_seed_independent() -> None:
    program = "\n".join(
        (
            "import json",
            "import sys",
            "from dataclasses import asdict",
            "from kora.ir.structured_schema_input import PreparedStructuredSchemaInput",
            "import tests.ir.structured_schema_cases as cases",
            "result: PreparedStructuredSchemaInput = cases.prepared(",
            "    cases.preparation_input(",
            "        {'properties': {key: False for key in {'a', 'b', 'c'}}}",
            "    )",
            ")",
            "sys.stdout.write(json.dumps(",
            "    {'input': cases.normalized_input(result),",
            "     'accounting': asdict(result.accounting)},",
            "    sort_keys=True,",
            "))",
        )
    )
    environment = dict(os.environ)

    results = [
        run_python(["-c", program], environment | {"PYTHONHASHSEED": seed})
        for seed in ("0", "1")
    ]

    assert all(result.code == 0 for result in results)
    assert all(result.stderr == b"" for result in results)
    assert results[0].stdout == results[1].stdout
