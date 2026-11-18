import json
import shlex
from pathlib import Path
from typing import Final

from tests.plugins.harness.claude.contained import assert_no_outbound
from tests.plugins.harness.claude.scripted_model import KEY, mapping, objects
from tests.plugins.harness.claude.test_session_isolation import run_scenario

PROMPT: Final[str] = "conformance"
SCRIPTED: Final[str] = "scripted-bytes\n"
SEEDED: Final[str] = "seeded-bytes\n"


def prepare_scenario(root: Path) -> tuple[Path, Path, Path]:
    workspace, account, markers = (
        root / name for name in ("workspace", "account", "markers")
    )
    for path in (workspace, account, markers):
        path.mkdir()
    (workspace / "seeded.txt").write_text(SEEDED)
    return workspace, account, markers


def event_data(report: dict[str, object], kind: str) -> list[dict[str, object]]:
    events = objects(report["events"])
    assert events is not None
    found: list[dict[str, object]] = []
    for value in events:
        event = mapping(value)
        assert event is not None
        if event["kind"] == kind:
            data = mapping(event["data"])
            assert data is not None
            found.append(data)
    return found


def assert_diagnostics(report: dict[str, object], outcome: str, target: Path) -> None:
    records = objects(report["diagnostics"])
    assert records is not None
    finished = [
        record
        for value in records
        if (record := mapping(value)) is not None
        and record.get("event") == "harness.claude.session_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["outcome"] == outcome
    rendered = json.dumps(records)
    for secret in (KEY, PROMPT, SCRIPTED, str(target)):
        assert json.dumps(secret)[1:-1] not in rendered


def test_scripted_claude_write_changes_the_file_when_allowed(tmp_path: Path) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    target = workspace / "target.txt"

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        [("Write", {"file_path": str(target), "content": SCRIPTED})],
    )

    assert_no_outbound(report)
    assert report["cause"] == "completed"
    assert report["error_class"] is None
    assert target.read_text() == SCRIPTED
    assert (workspace / "seeded.txt").read_text() == SEEDED
    calls = event_data(report, "tool_call")
    results = event_data(report, "tool_result")
    assert len(calls) == len(results) == 1
    assert calls[0]["tool"] == "Write"
    assert results[0]["tool_use_id"] == calls[0]["tool_use_id"]
    assert results[0]["is_error"] is False
    assert event_data(report, "permission_denied") == []
    assert_diagnostics(report, "completed", target)
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}


def test_scripted_claude_write_leaves_the_file_untouched_when_denied(
    tmp_path: Path,
) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    target = workspace / "target.txt"

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        [("Write", {"file_path": str(target), "content": SCRIPTED})],
        denied_tool="Write",
    )

    assert_no_outbound(report)
    assert report["cause"] == "completed"
    assert report["error_class"] is None
    assert not target.exists()
    denials = event_data(report, "permission_denied")
    assert len(denials) == 1
    assert denials[0]["source"] == "policy"
    assert denials[0]["tool"] == "Write"
    assert (workspace / "seeded.txt").read_text() == SEEDED
    assert_diagnostics(report, "completed", target)
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}


def test_scripted_claude_decision_failure_leaves_the_file_untouched(
    tmp_path: Path,
) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    target = workspace / "target.txt"

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        [("Write", {"file_path": str(target), "content": SCRIPTED})],
        policy="raising",
    )

    assert_no_outbound(report)
    assert not target.exists()
    denials = event_data(report, "permission_denied")
    assert len(denials) == 1
    assert denials[0]["source"] == "harness"
    assert denials[0]["tool"] == "Write"
    assert denials[0]["code"] == "policy_error"
    calls = event_data(report, "tool_call")
    assert len(calls) == 1
    assert calls[0]["tool"] == "Write"
    assert denials[0]["tool_use_id"] == calls[0]["tool_use_id"]
    assert report["cause"] == "policy_error"
    assert report["error_class"] == "config_error"
    assert "kora-decision-failure-1646" not in json.dumps(report)
    assert (workspace / "seeded.txt").read_text() == SEEDED
    assert_diagnostics(report, "policy_error", target)
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}


def test_scripted_claude_cancellation_closes_the_tool_child(tmp_path: Path) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    started = markers / "started"
    target = workspace / "target.txt"
    command = (
        f"touch {shlex.quote(str(started))}; sleep 20; "
        f"printf ran > {shlex.quote(str(target))}"
    )

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        [("Bash", {"command": command})],
        cancel_marker=str(started),
    )

    assert_no_outbound(report)
    calls = event_data(report, "tool_call")
    assert len(calls) == 1
    assert calls[0]["tool"] == "Bash"
    assert report["cause"] == "cancel"
    assert report["error_class"] == "cancelled"
    assert started.is_file()
    assert not target.exists()
    assert (workspace / "seeded.txt").read_text() == SEEDED
    assert_diagnostics(report, "cancel", target)
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}
