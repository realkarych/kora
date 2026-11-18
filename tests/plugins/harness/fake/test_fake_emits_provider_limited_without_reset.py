from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.ir.json import JsonObject
from kora.protocols.runtime import SessionResultObservation
from tests.plugins.harness.fake.fake_support import result
from tests.runner.agent_support import hosted, make_world, run, script

LIMITED: JsonObject = {
    "retry_after": "2026-11-12T01:00:00Z",
    "scope": "quota",
    "vendor_group": "group",
    "model": "model",
    "binding": "binding",
}


@pytest.mark.parametrize("unknown", ["retry_after", "vendor_group"])
def test_fake_emits_provider_limited_without_reset(
    tmp_path: Path, kora_capture: Capture, unknown: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    data: JsonObject = {**LIMITED, unknown: None}
    event: JsonObject = {"kind": "event", "event": "provider_limited", "data": data}

    host, _ = run(hosted(world, script([event, result()])))
    observation = host.observation()
    records = world.records()
    world.close()

    limited = [record for record in records if record["kind"] == "provider_limited"]
    assert isinstance(observation, SessionResultObservation)
    assert len(limited) == 1
    assert limited[0]["data"] == data
