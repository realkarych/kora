import pytest

from kora.db.graph_value_json import GraphValueRejected
from kora.engine.graph_output_inventory import build_graph_output_inventory
from kora.observability import Level, LoggingSettings
from tests.engine.graph_inventory_cases import inventory_arguments
from tests.engine.graph_output_cases import LIMIT, child, output_document
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)


@pytest.mark.parametrize("mode", ["saved", "refused"])
def test_inventory_diagnostics_exclude_retained_content(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    marker = "private_inventory_contract_content"
    raw = output_document()
    node = child(raw, "graph", "nodes", "producer")
    child(node, "source")["file"] = marker + ".py"
    child(node, "config")["output_schema"] = {"description": marker}
    if mode == "refused":
        child(node, "outputs")["z_later"] = {"type": "List[StructuredOutput]"}
    arguments = inventory_arguments(raw)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    if mode == "refused":
        with pytest.raises(GraphValueRejected) as failure:
            build_graph_output_inventory(*arguments, max_encoded_bytes=LIMIT)
        assert str(failure.value) == "unsupported graph value encoding"
    else:
        result = build_graph_output_inventory(*arguments, max_encoded_bytes=LIMIT)
        assert isinstance(result, str)
    output = finished_output(diagnostics, pipe)

    assert any(
        record["event"] == "ir.output_schemas.completed"
        for record in pipe.reader.records()
    )
    assert marker not in output
    assert capsys.readouterr().out == ""
    assert not diagnostics.writer_is_alive()
