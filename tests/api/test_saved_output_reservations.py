import gc
import sys
import weakref
from dataclasses import replace
from types import FrameType

import pytest

from kora.api.graph_deployment_limits import EXECUTION, READ
from kora.api.graph_execution_preparation import (
    PreparedLogicalExecutionContract,
    prepare_logical_execution_contract,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_execution_contract import (
    execution_contract_read_reservation,
    structured_output_preparation_reservation,
)
from kora.ir.standard_output_preparation import (
    PreparedStandardOutput,
    prepare_standard_output,
)
from kora.ir.standard_schema_source import Sources
from kora.protocols.effective_artifact_type import (
    EffectiveArtifactType,
    prepare_effective_artifact_type,
)
from tests.api.test_graph_execution_preparation import borrowed, source
from tests.support.output_bindings import publication_binding


def test_saved_output_preparation_reservation_covers_both_policies() -> None:
    ir_text, _ = source(ordered=True)
    exact = replace(EXECUTION, max_retained_bytes=2_119_616_025)
    short = replace(exact, max_retained_bytes=exact.max_retained_bytes - 1)
    binding = publication_binding()

    retained, _ = execution_contract_read_reservation(READ)
    scratch, work = structured_output_preparation_reservation(64)
    with borrowed() as (runner, evaluator):
        prepared = runner.run(
            prepare_logical_execution_contract(
                ir_text, evaluator, binding=binding, limits=exact
            )
        )
        with pytest.raises(GraphValueRejected) as failure:
            runner.run(
                prepare_logical_execution_contract(
                    ir_text, evaluator, binding=binding, limits=short
                )
            )

    assert retained == 783_551_424
    assert scratch == 15_988_128
    assert work == 273_588_480
    assert isinstance(prepared, PreparedLogicalExecutionContract)
    assert failure.value.reason is GraphValueFailure.BUDGET


def test_schema_source_and_composition_holders_do_not_span_inventory_passes() -> None:
    ir_text, _ = source(ordered=True)
    binding = publication_binding()
    previous: set[int] = set()
    sources: list[weakref.ReferenceType[Sources]] = []
    passes: list[bool] = []
    holders = (PreparedStandardOutput, EffectiveArtifactType)

    def observe(frame: FrameType, event: str, result: object) -> None:
        if event == "call" and frame.f_code is prepare_standard_output.__code__:
            passes.append(
                not any(
                    isinstance(value, holders) and id(value) in previous
                    for value in gc.get_objects()
                )
            )
            assert all(reference() is None for reference in sources)
        elif event == "return" and frame.f_code in (
            prepare_standard_output.__code__,
            prepare_effective_artifact_type.__code__,
        ):
            if isinstance(result, holders):
                previous.add(id(result))
        elif event == "return" and frame.f_code is Sources.__init__.__code__:
            value: object = frame.f_locals["self"]
            assert isinstance(value, Sources)
            sources.append(weakref.ref(value))

    incumbent = sys.getprofile()
    with borrowed() as (runner, evaluator):
        try:
            sys.setprofile(observe)
            prepared = runner.run(
                prepare_logical_execution_contract(
                    ir_text, evaluator, binding=binding, limits=EXECUTION
                )
            )
        finally:
            sys.setprofile(incumbent)

    assert isinstance(prepared, PreparedLogicalExecutionContract)
    assert passes == [True, True, True]
    assert len(previous) >= 2
    assert len(sources) == 6
    assert all(reference() is None for reference in sources)
    assert not any(
        isinstance(value, holders) and id(value) in previous
        for value in gc.get_objects()
    )
