import json
from pathlib import Path
from typing import Final

from kora.conformance.capture import Capture
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.runner.agent_support import JournalProxy, hosted, make_world, run, script

MARKER: Final[str] = "kora-unregistered-agent-content-4d1c"
LEAKED_VALUE: Final[str] = "kora-registered-agent-value-9e2f"


CONTENT: Final[str] = f"{MARKER} {LEAKED_VALUE}"


def test_logging_redacts_content_and_safe_exception_chains(
    tmp_path: Path, kora_capture: Capture
) -> None:
    kora_capture.scope.admit(LEAKED_VALUE)
    worlds = [make_world(tmp_path / name, kora_capture) for name in ("a", "b", "c")]
    shell = tool_call("Bash", "call-1", arguments={"command": CONTENT})
    closing = JournalProxy(worlds[2].journal, "turn", "close")

    run(hosted(worlds[0], script([turn("m1", CONTENT), shell, result()])))
    run(hosted(worlds[1], script([turn("m1", CONTENT)])))
    run(hosted(worlds[2], script([turn("m1", CONTENT), result()]), closing))
    for world in worlds:
        world.close()
    records = kora_capture.finish()

    agent = [record for record in records if record.get("component") == "runner.agent"]
    events = [record.get("event") for record in agent]
    failures = [
        record.get("code")
        for record in agent
        if record.get("event") == "runner.agent.session_failed"
    ]
    text = "\n".join(json.dumps(record) for record in records)
    assert events.count("runner.agent.session_started") == 3
    assert events.count("runner.agent.session_finished") == 1
    assert "runner.agent.journal_gap" in events
    assert failures == ["script_end_invalid", "result_unsealed"]
    assert MARKER not in text
    assert LEAKED_VALUE not in text
    assert str(tmp_path) not in text
