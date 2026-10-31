import gc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel import CheckedExpression, SchemaTypes
from kora.cel.types import Record
from kora.ir import CustomTypeContract, HumanInputNode, validate_approval_defaults
from kora.ir.approval_defaults import ApprovalDefaultChecks
from tests.ir.approval_default_cases import gate, shared_form
from tests.ir.inline_schema_cases import document_for


@pytest.mark.parametrize("invalid", [False, True])
def test_approval_default_work_reuses_deep_saved_schema_mapping(invalid: bool) -> None:
    one = shared_form(1, 1000, invalid)
    many = shared_form(1000, 1000, invalid)

    single = validate_approval_defaults(one)
    repeated = validate_approval_defaults(many)

    assert single.approval_defaults_valid is not invalid
    assert repeated.approval_defaults_valid is not invalid
    assert repeated.predicates_checked == repeated.expressions_checked == 1000
    assert repeated.schema_steps == single.schema_steps + 999
    assert single.schema_steps < 3 * 1000 + 20
    assert repeated.checker_steps == single.checker_steps * 1000


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_approval_default_mapping_of_repeated_builtin_form_is_constant(
    count: int,
) -> None:
    one = document_for(nodes={"gate": gate()})
    many = document_for(nodes={f"n{index}": gate() for index in range(count)})

    single = validate_approval_defaults(one)
    repeated = validate_approval_defaults(many)

    assert repeated.approval_defaults_valid
    assert repeated.schema_steps == single.schema_steps
    assert repeated.checker_steps == count * single.checker_steps
    assert repeated.predicates_checked == repeated.expressions_checked == count


def test_approval_default_deep_mapping_uses_no_python_recursion() -> None:
    document = shared_form(1, 5000)

    result = validate_approval_defaults(document)

    assert result.approval_defaults_valid
    assert result.schema_steps < 3 * 5000 + 20
    assert result.predicates_checked == result.expressions_checked == 1


@pytest.mark.parametrize("ending", ["valid", "invalid", "unresolved"])
def test_approval_default_releases_input_and_temporary_state_while_result_survives(
    ending: str,
) -> None:
    document = shared_form(3, 100, ending == "invalid")
    if ending == "unresolved":
        document = document_for(
            nodes={"gate": gate({"$ref": "urn:kora:artifact:example:Missing"})}
        )
    subject = next(iter(document.graph.nodes.values()))
    assert isinstance(subject, HumanInputNode)
    assert subject.config.form is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(subject),
        weakref.ref(subject.config),
        weakref.ref(subject.config.form),
    ]
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    del subject
    gc.collect()
    owned_types = SchemaTypes | ApprovalDefaultChecks | CheckedExpression | Record
    baseline = sum(isinstance(value, owned_types) for value in gc.get_objects())

    result = validate_approval_defaults(document)
    diagnostic_references = [weakref.ref(error) for error in result.diagnostics.errors]
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    remaining = sum(isinstance(value, owned_types) for value in gc.get_objects())
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert remaining == baseline
    assert bool(diagnostic_references) is (ending == "invalid")
    assert all(reference() is None for reference in diagnostic_references)
    assert result_reference() is None
