import pytest

from kora.db.graph_value_json import GraphValueRejected
from kora.engine.graph_outputs import GraphOutputContract, build_graph_output_contract
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from kora.observability import Level, LoggingSettings
from tests.engine.graph_output_cases import (
    LIMIT,
    child,
    output_arguments,
    output_document,
)
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)


@pytest.mark.parametrize("mode", ["saved", "refused", "invalid"])
def test_graph_output_diagnostics_exclude_retained_content(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    marker = "private_output_contract_content"
    raw = output_document()
    node = child(raw, "graph", "nodes", "producer")
    child(node, "source")["file"] = marker + ".py"
    child(node, "config")["output_schema"] = {"description": marker}
    if mode == "refused":
        child(node, "config", "output_schema")["pattern"] = "a"
    elif mode == "invalid":
        child(node, "outputs", "value")["schema"] = {"type": "integer"}
    arguments = output_arguments(raw)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    if mode == "invalid":
        with pytest.raises(GraphValueRejected) as failure:
            build_graph_output_contract(*arguments, max_encoded_bytes=LIMIT)
        assert str(failure.value) == "invalid graph value representation"
    else:
        result = build_graph_output_contract(*arguments, max_encoded_bytes=LIMIT)
        assert isinstance(
            result,
            GraphOutputContract
            if mode == "saved"
            else StructuredSchemaPreparationRefusal,
        )
    output = finished_output(diagnostics, pipe)

    records = pipe.reader.records()
    assert any(record["event"] == "ir.output_schemas.completed" for record in records)
    assert marker not in output
    assert capsys.readouterr().out == ""
    assert not diagnostics.writer_is_alive()
