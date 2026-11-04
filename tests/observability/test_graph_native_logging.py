import pytest

from kora.db.graph_value_json import GraphValueRejected
from kora.engine.graph_native_outputs import build_graph_native_output_contract
from kora.observability import Level, LoggingSettings
from tests.engine.graph_native_cases import native_arguments, native_document
from tests.engine.graph_output_cases import LIMIT, child
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)


@pytest.mark.parametrize("mode", ["saved", "refused"])
def test_native_output_diagnostics_exclude_retained_content(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    marker = "private_output_contract_content"
    raw = native_document("AgentSession")
    node = child(raw, "graph", "nodes", "producer")
    child(node, "source")["file"] = marker + ".py"
    child(node, "config")["output_schema"] = {"description": marker}
    if mode == "refused":
        child(node, "outputs", "session")["schema"] = {"description": marker}
    arguments = native_arguments("AgentSession", raw=raw)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    if mode == "refused":
        with pytest.raises(GraphValueRejected) as failure:
            build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
        assert str(failure.value) == "unsupported graph value encoding"
    else:
        result = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
        assert result.digest
    output = finished_output(diagnostics, pipe)

    records = pipe.reader.records()
    assert any(record["event"] == "ir.output_schemas.completed" for record in records)
    assert marker not in output
    assert capsys.readouterr().out == ""
    assert not diagnostics.writer_is_alive()
