import json

import pytest
from pydantic import ValidationError

from kora.ir.cost import MAX_MONEY, MAX_SUM, MAX_TOKEN
from kora.models.worker_usage import AcceptedUsageReport, AcceptedUsageRow, UsageReport
from tests.engine.test_usage_acceptance import coverage, usage
from tests.ir.test_cost_models import usage_payload


@pytest.mark.parametrize("private_field", ["usd_estimate", "same_basis_usd_floor"])
def test_usage_request_refuses_server_money_fields(private_field: str) -> None:
    payload: dict[str, object] = {
        "lease_epoch": 1,
        "dispatcher_id": "grant",
        "coverage": coverage(),
        "rows": [usage_payload(**{private_field: "1"})],
    }

    with pytest.raises(ValidationError):
        UsageReport.model_validate(payload)


def test_usage_request_bounds_rows_before_member_validation() -> None:
    payload: dict[str, object] = {
        "lease_epoch": 1,
        "dispatcher_id": "grant",
        "coverage": coverage(),
        "rows": [{}] * 65,
    }

    with pytest.raises(ValidationError) as refused:
        UsageReport.model_validate(payload)

    assert [error["loc"] for error in refused.value.errors()] == [("rows",)]


def test_usage_response_preserves_nullable_estimate_and_source_identity() -> None:
    row = usage(source={"nul\0": [1, 1.0, -0.0]})
    response = AcceptedUsageReport(
        rows=(AcceptedUsageRow(**row.model_dump(), usd_estimate=None),),
        coverage=coverage(),
    )

    restored = AcceptedUsageReport.model_validate_json(response.model_dump_json())
    encoded = restored.model_dump(mode="json")

    assert encoded["rows"][0]["usd_estimate"] is None
    assert encoded["rows"][0]["source"] == {"nul\0": [1, 1.0, -0.0]}
    assert "same_basis_usd_floor" not in encoded["rows"][0]
    assert restored.model_dump_json() == response.model_dump_json()


def test_usage_response_complete_bound_includes_all_rows_and_coverage() -> None:
    source = {"value": "é" * 4090}
    row = usage(
        model="😀" * 256,
        observation_id="😀" * 256,
        observation_sequence=MAX_SUM,
        input_tokens=MAX_TOKEN,
        cached_input_tokens=MAX_TOKEN,
        cache_creation_tokens=MAX_TOKEN,
        output_tokens=MAX_TOKEN,
        reasoning_tokens=MAX_TOKEN,
        cache_creation_by_ttl={"5m": MAX_TOKEN, "1h": 0},
        reported_usd=str(MAX_MONEY),
        source=source,
    )
    response = AcceptedUsageReport(
        rows=tuple(
            AcceptedUsageRow.model_validate(
                row.model_dump()
                | {
                    "model": "😀" * 255 + chr(0x1F300 + index),
                    "usd_estimate": str(MAX_MONEY),
                }
            )
            for index in range(64)
        ),
        coverage=coverage(MAX_SUM, observation_id="😀" * 256),
    )
    derived_allowance = 64 * (6 * 8192 + 2 * 12 * 256 + 4096) + 8192

    encoded = json.dumps(
        response.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":")
    ).encode()

    assert len(encoded) <= derived_allowance < 4 * 1024 * 1024
    assert AcceptedUsageReport.model_validate_json(encoded) == response
