import hashlib
import operator
from copy import deepcopy
from typing import cast

import pytest
from pydantic import MISSING

from kora.ir.config import NodeLimits
from kora.ir.cost import PriceRates, PriceRevision
from kora.protocols.harness import HarnessValueRefused, SessionInputs, prompt_digest


def test_session_inputs_preserve_sealed_values() -> None:
    prompt = "  Cafe\u0301 — привет\n"
    rates: dict[str, str | None] = {
        "input": "1.25",
        "cached_input": None,
        "cache_creation_5m": "2",
        "cache_creation_1h": "3",
        "output": "4",
        "reasoning": "5",
    }
    prices = {"model-a": rates, "model-b": {**rates, "output": "6"}}
    source = {
        "id": "sealed-prices",
        "namespace_id": "test-namespace",
        "effective_from": "2026-01-01T00:00:00Z",
        "version": 1,
        "normalization_version": 1,
        "prices": prices,
    }
    expected = deepcopy(source)
    revision = PriceRevision.model_validate(source)
    limits = NodeLimits()

    inputs = SessionInputs(prompt, prompt_digest(prompt), revision.id, revision, limits)
    rates["input"] = "99"
    prices.clear()
    source["id"] = "changed"
    limits.extensions["x-vendor"] = {"turns": 7}
    inputs.limits.extensions["x-vendor"] = {"turns": 8}

    assert inputs.prompt.encode("utf-8") == prompt.encode("utf-8")
    assert (
        inputs.prompt_digest
        == "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    )
    assert inputs.price_table_id == "sealed-prices"
    assert inputs.price_table.model_dump(mode="json") == expected
    assert inputs.limits.model_dump(mode="json") == {}
    assert inputs.limits.max_events is MISSING
    assert inputs.limits.budget_usd is MISSING
    assert inputs.limits.timeout is MISSING
    assert inputs.limits.stall_after is MISSING
    with pytest.raises(TypeError):
        operator.setitem(
            cast("dict[str, PriceRates]", inputs.price_table.prices),
            "other",
            inputs.price_table.prices["model-a"],
        )


@pytest.mark.parametrize(
    "field", ["prompt", "prompt_digest", "price_table_id", "limits"]
)
def test_session_inputs_refuse_inconsistent_values(field: str) -> None:
    prompt = "SECRET-PROMPT" * (100_000 if field == "prompt" else 1)
    revision = PriceRevision.model_validate(
        {
            "id": "sealed-prices",
            "namespace_id": None,
            "effective_from": "2026-01-01T00:00:00Z",
            "version": 1,
            "normalization_version": 1,
            "prices": {},
        }
    )
    digest = (
        prompt_digest("other") if field == "prompt_digest" else prompt_digest(prompt)
    )
    identifier = "other" if field == "price_table_id" else revision.id
    limits = (
        NodeLimits.model_validate({"x-vendor": {"turns": 1}})
        if field == "limits"
        else NodeLimits()
    )

    with pytest.raises(HarnessValueRefused, match=field) as refusal:
        SessionInputs(prompt, digest, identifier, revision, limits)

    assert "SECRET-PROMPT" not in str(refusal.value)
    assert refusal.value.__cause__ is None
