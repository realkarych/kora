import pytest
from pydantic import TypeAdapter, ValidationError

from kora.ir import (
    CronTrigger,
    GraphTrigger,
    ManualTrigger,
    SensorTrigger,
    Trigger,
    WebhookTrigger,
    export_ir,
)
from tests.ir.declaration_cases import MINIMAL, POPULATED

TRIGGERS = [ManualTrigger, CronTrigger, WebhookTrigger, SensorTrigger, GraphTrigger]
FOREIGN: dict[str, object] = {
    "expr": "",
    "timezone": "",
    "name": "",
    "auth": {"kind": "hmac", "secret": "TOKEN"},
    "params_from": "none",
    "poll": {"sensor": "http", "until": "true"},
    "interval": "0s",
    "graph": "",
    "on": "run_finished",
}


@pytest.mark.parametrize("model", TRIGGERS)
def test_ir_trigger_variants_reject_cross_variant_fields(
    model: type[ManualTrigger],
) -> None:
    adapter = TypeAdapter(Trigger)
    document = POPULATED[model]
    foreign = [name for name in FOREIGN if name not in model.model_fields]

    parsed = adapter.validate_python(document)
    for name in foreign:
        with pytest.raises(ValidationError):
            adapter.validate_python({**MINIMAL[model], name: FOREIGN[name]})

    assert type(parsed) is model
    assert export_ir(parsed) == document


@pytest.mark.parametrize("model", TRIGGERS)
def test_ir_trigger_operational_fields_are_required(model: type[ManualTrigger]) -> None:
    adapter = TypeAdapter(Trigger)
    document = MINIMAL[model]

    for field in document:
        incomplete = {name: item for name, item in document.items() if name != field}
        with pytest.raises(ValidationError):
            adapter.validate_python(incomplete)

    assert export_ir(adapter.validate_python(document)) == document


@pytest.mark.parametrize("kind", [None, False, "unknown"])
def test_ir_trigger_union_refuses_invalid_kind_tags(kind: object) -> None:
    adapter = TypeAdapter(list[Trigger])
    documents = [{"kind": "manual"}, {"kind": kind}]

    with pytest.raises(ValidationError):
        adapter.validate_python(documents)
