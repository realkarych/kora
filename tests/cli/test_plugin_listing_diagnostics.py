import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from kora.ir.json import JsonObjectField
from tests.support.provider_tooling import build_fixture_wheel, install_tooling


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_listing_diagnostic_loss_does_not_hold_command_exit(
    tmp_path: Path, provider_tooling_wheelhouse: Path, sink: str
) -> None:
    entries = {
        f"policy.item_{index:03}": (f"kora_tooling_fixture.inventory:item_{index:03}")
        for index in range(1096)
    }
    fixture = build_fixture_wheel(tmp_path, entries, inventory=1096)
    installed = install_tooling(tmp_path, provider_tooling_wheelhouse, fixture)

    completed = subprocess.run(
        [str(installed.python), "-I", "-m", "kora_tooling_fixture.sink_probe", sink],
        cwd=installed.outside,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = TypeAdapter(JsonObjectField).validate_json(completed.stdout)
    assert payload["providers"] == 1100
    assert payload["failures"] == 0
    assert payload["released"] is True
    dropped = payload["dropped"]
    sink_failures = payload["sink_failures"]
    assert isinstance(dropped, int)
    assert dropped > 0
    assert isinstance(sink_failures, int)
    if sink == "broken":
        assert sink_failures > 0
    else:
        primed = payload["primed_in_flight_bytes"]
        assert isinstance(primed, int)
        assert primed > 0
        assert sink_failures == 0
        assert payload["drained"] is False
        assert payload["queued_before_shutdown"] == 1024
        capacity_loss = payload["queue_capacity_loss"]
        assert isinstance(capacity_loss, int)
        assert capacity_loss > 0
    assert payload["rejected"] == 0
    assert payload["queue_records"] == 0
    assert payload["queue_bytes"] == 0
    in_flight = payload["in_flight_bytes"]
    assert isinstance(in_flight, int)
    assert 0 <= in_flight <= 65536
    if payload["drained"]:
        assert in_flight == 0
