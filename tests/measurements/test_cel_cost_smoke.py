import json
import sys
from typing import cast

import pytest

from tests.cel_eval.children import ROOT, full_path
from tests.measurements.cel_cost_smoke import (
    CASE_NAMES,
    PRELUDE,
    smoke_case,
    validate_smoke,
)
from tests.measurements.cel_measurement_process import run_process


@pytest.mark.parametrize("name", CASE_NAMES)
def test_cel_cost_smoke_uses_real_calls_and_releases_handles(name: str) -> None:
    case = smoke_case(name)
    checked = case.checked()

    result = run_process(
        [sys.executable, "-I", "-c", PRELUDE, json.dumps(full_path(ROOT)), name]
    )
    payload = validate_smoke(result.stdout, case)

    assert result.code == 0
    assert not result.timed_out
    assert not result.overflow
    assert not result.stderr
    assert payload["pid"] == result.pid
    assert checked.digest == payload["rendering_sha256"]
    samples = payload["samples"]
    assert isinstance(samples, list)
    assert len(cast(list[object], samples)) == 4
    altered: dict[str, object] = {**payload, "resources": {}}
    with pytest.raises(ValueError, match="smoke"):
        validate_smoke(json.dumps(altered).encode(), case)
