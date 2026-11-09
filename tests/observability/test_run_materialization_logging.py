from traceback import walk_tb
from typing import cast

import pytest

from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.run_materialization import (
    MaterializationFailure,
    MaterializationRejected,
    prepare_run_materialization,
)
from kora.observability import Level, LoggingSettings
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    MARKER,
    complete_inputs,
    prepared_case,
    transport,
)
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)


def test_run_materialization_diagnostics_and_refusals_exclude_content(
    configure: Configure, pipe: Pipe
) -> None:
    scope = GraphResourceScope("secret_scope", "secret_graph", "secret_version")
    raw, envelope, refs, owners = complete_inputs(scope, resources=False)
    prepared, pin = prepared_case(raw, envelope, scope)
    checked = check_detached_execution_contract(
        transport(prepared, scope), pin, scope, refs, owners, limits=LIMITS
    )
    unissued = CheckedExecutionContract.__new__(CheckedExecutionContract)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )
    exact = len(checked.canonical_ir)

    prepare_run_materialization(checked, max_ir_bytes=exact, max_top_level_nodes=3)
    with pytest.raises(MaterializationRejected) as binding:
        prepare_run_materialization(unissued, max_ir_bytes=exact, max_top_level_nodes=3)
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        with pytest.raises(MaterializationRejected) as capacity:
            prepare_run_materialization(
                checked, max_ir_bytes=cast(int, MARKER), max_top_level_nodes=3
            )
    output = finished_output(diagnostics, pipe)

    assert binding.value.reason is MaterializationFailure.BINDING
    assert capacity.value.reason is MaterializationFailure.CAPACITY
    assert capacity.value.__cause__ is capacity.value.__context__ is None
    assert [
        set(frame.f_locals)
        for frame, _ in walk_tb(capacity.value.__traceback__)
        if frame.f_code.co_name == "prepare_run_materialization"
    ] == [{"result"}]
    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.run_materialization.prepared"
    ]
    assert [record["outcome"] for record in records] == [
        "accepted",
        "binding",
        "capacity",
    ]
    for private in (
        MARKER,
        "producer",
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        pin.execution_contract_digest,
    ):
        assert private not in output
    assert not diagnostics.writer_is_alive()
