import json
import shlex
from pathlib import Path
from typing import Final, cast, get_args

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.observability import LoggingSettings
from kora.plugins.harness.claude.options import ClaudeSessionOptions
from kora.plugins.harness.claude.session import COMPONENT
from kora.protocols.time import SystemClock
from tests.plugins.harness.claude.contained import assert_no_outbound
from tests.plugins.harness.claude.scripted_assertions import (
    SCRIPTED,
    SEEDED,
    assert_diagnostics,
    event_data,
    prepare_scenario,
)
from tests.plugins.harness.claude.scripted_model import mapping, objects
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_replay_events import context_for
from tests.plugins.harness.claude.test_session_isolation import run_scenario

PENDING_KINDS = {"mcp_tool": "#1765 MCP servers are refused in the first slice"}
PENDING_LEGS = {
    "other_tool-IR019-IR028-validate-deploy": (
        "work items 3, 11 and 14 coverage validation"
    ),
    "codex": "#772 Codex policy coverage",
}
MODES = cast(
    "tuple[str, ...]",
    get_args(ClaudeSessionOptions.model_fields["permission_mode"].annotation),
)


def covered_kinds() -> list[str]:
    with captured(COMPONENT, LoggingSettings(), "policy-kinds", utc_now) as capture:
        adapter = make_adapter(Path("/work"), context_for(capture, SystemClock()))
        return sorted(adapter.capabilities().policy_coverage - PENDING_KINDS.keys())


KINDS = covered_kinds()


def test_policy_coverage_has_owned_pending_legs(claude_capture: Capture) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))

    coverage = adapter.capabilities().policy_coverage

    assert coverage == set(KINDS) | PENDING_KINDS.keys()
    assert all(
        "#" in owner or "work items " in owner
        for owner in (*PENDING_KINDS.values(), *PENDING_LEGS.values())
    )


def entries(report: dict[str, object], key: str) -> list[dict[str, object]]:
    values = objects(report[key])
    assert values is not None
    result: list[dict[str, object]] = []
    for value in values:
        entry = mapping(value)
        assert entry is not None
        result.append(entry)
    return result


def assert_call(
    report: dict[str, object],
    identity: str,
    tool: str,
    *,
    allowed: bool,
    harness_denied: bool = False,
    subagent: bool = False,
    reason: str | None = None,
    stream_tool: str | None = None,
) -> None:
    decisions = [
        entry
        for entry in entries(report, "decisions")
        if entry["tool_use_id"] == identity
    ]
    assert len(decisions) == 1
    assert decisions[0]["tool"] == tool
    assert decisions[0]["allow"] is allowed
    assert decisions[0]["subagent"] is subagent
    if reason is not None:
        assert decisions[0]["reason"] == reason
    calls = [
        entry
        for entry in event_data(report, "tool_call")
        if entry["tool_use_id"] == identity
    ]
    results = [
        entry
        for entry in event_data(report, "tool_result")
        if entry["tool_use_id"] == identity
    ]
    denials = [
        entry
        for entry in event_data(report, "permission_denied")
        if entry["tool_use_id"] == identity
    ]
    assert len(calls) == len(results) == 1
    assert calls[0]["tool"] == (tool if stream_tool is None else stream_tool)
    assert results[0]["tool_use_id"] == calls[0]["tool_use_id"]
    assert results[0]["is_error"] is (not allowed or harness_denied)
    if allowed and not harness_denied:
        assert denials == []
    else:
        assert len(denials) == 1
        assert denials[0]["tool"] == tool
        assert denials[0]["source"] == ("harness" if allowed else "policy")
        if allowed:
            assert denials[0]["code"] == "harness_rule"
        else:
            assert reason is not None
            assert denials[0]["code"] == ("denied" if ".deny." in reason else reason)
            if ".deny." in reason:
                assert denials[0]["pattern"] == reason


def assert_totals(
    report: dict[str, object], identities: set[str], denied: set[str]
) -> None:
    for kind in ("tool_call", "tool_result"):
        found = sorted(str(entry["tool_use_id"]) for entry in event_data(report, kind))
        assert found == sorted(identities)
    decided = sorted(
        str(entry["tool_use_id"]) for entry in entries(report, "decisions")
    )
    assert decided == sorted(identities)
    refused = sorted(
        str(entry["tool_use_id"]) for entry in event_data(report, "permission_denied")
    )
    assert refused == sorted(denied)


def scripted_ids(prefix: str, count: int) -> set[str]:
    return {f"{prefix}{index}" for index in range(count)}


def assert_session(
    report: dict[str, object], target: Path, seeds: dict[Path, bytes]
) -> None:
    assert_no_outbound(report)
    assert report["cause"] == "completed"
    assert report["error_class"] is None
    assert report["unauthorized_requests"] == 0
    assert not report["policy_bypass"]
    assert report["leftover"] == []
    assert set(objects(report["clients"]) or []) == {"127.0.0.1"}
    for path, content in seeds.items():
        assert path.read_bytes() == content
    assert_diagnostics(report, "completed", target)
    recorder = mapping(report["recorder"])
    assert recorder is not None
    argv = objects(recorder["argv"])
    assert argv is not None
    assert not any(
        isinstance(value, str)
        and value.split("=", 1)[0]
        in {
            "--allowedTools",
            "--disallowedTools",
            "--allowed-tools",
            "--disallowed-tools",
        }
        for value in argv
    )


CHANGED: Final[str] = "changed-bytes\n"
CHANGED_SOURCE: Final[str] = "changed = 2"


def notebook_text() -> str:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "id": "seeded-cell",
                    "metadata": {},
                    "outputs": [],
                    "source": ["original = 1\n"],
                }
            ],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                }
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def seed_files(workspace: Path) -> dict[Path, bytes]:
    notebook = workspace / "seeded.ipynb"
    notebook.write_text(notebook_text())
    return {path: path.read_bytes() for path in (workspace / "seeded.txt", notebook)}


def seed_editable(workspace: Path) -> tuple[Path, Path]:
    text, notebook = workspace / "editable.txt", workspace / "editable.ipynb"
    text.write_text(SEEDED)
    notebook.write_text(notebook_text())
    return text, notebook


def notebook_source(notebook: Path) -> str:
    document = mapping(json.loads(notebook.read_text()))
    assert document is not None
    cells = objects(document["cells"])
    assert cells is not None
    cell = mapping(cells[0])
    assert cell is not None
    source = cell["source"]
    if isinstance(source, str):
        return source
    lines = objects(source)
    assert lines is not None
    return "".join(str(line) for line in lines)


def edit_calls(
    workspace: Path, stem: str = "seeded"
) -> list[tuple[str, dict[str, object]]]:
    return [
        ("Read", {"file_path": str(workspace / f"{stem}.txt")}),
        (
            "Edit",
            {
                "file_path": str(workspace / f"{stem}.txt"),
                "old_string": "seeded-bytes",
                "new_string": "changed-bytes",
            },
        ),
    ]


def notebook_calls(workspace: Path, stem: str) -> list[tuple[str, dict[str, object]]]:
    return [
        ("Read", {"file_path": str(workspace / f"{stem}.ipynb")}),
        (
            "NotebookEdit",
            {
                "notebook_path": str(workspace / f"{stem}.ipynb"),
                "cell_id": "seeded-cell",
                "new_source": CHANGED_SOURCE,
                "cell_type": "code",
                "edit_mode": "replace",
            },
        ),
    ]


def web_requests(report: dict[str, object]) -> int:
    requests = objects(report["server_tool_requests"])
    assert requests is not None
    return sum("web_search" in (objects(request) or []) for request in requests)


@pytest.mark.parametrize(
    ("kind", "mode"),
    [(kind, mode) for kind in KINDS for mode in MODES],
    ids=[f"{kind}-{mode}" for kind in KINDS for mode in MODES],
)
def test_required_policy_is_enforced_or_rejected(
    tmp_path: Path, kind: str, mode: str
) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    seeds = seed_files(workspace)
    editable_text, editable_notebook = seed_editable(workspace)
    allowed = workspace / "allowed.txt"
    denied = workspace / "denied.txt"
    calls: list[tuple[str, dict[str, object]]] = [
        ("Write", {"file_path": str(allowed), "content": SCRIPTED}),
        ("Write", {"file_path": str(denied), "content": SCRIPTED}),
    ]
    subagent_calls: list[tuple[str, dict[str, object]]] = []
    deny = ["Write(denied.txt)"]
    if kind == "shell":
        calls = [
            ("Bash", {"command": f"touch {shlex.quote(str(path))}"})
            for path in (allowed, denied)
        ]
        deny = ["Bash(*denied*)"]
    elif kind == "file_edit":
        calls.extend(edit_calls(workspace))
        calls.extend(notebook_calls(workspace, "seeded"))
        calls.extend(edit_calls(workspace, "editable"))
        calls.extend(notebook_calls(workspace, "editable"))
        deny.extend(["Edit(seeded.txt)", "NotebookEdit(seeded.ipynb)"])
    elif kind == "subagent":
        subagent_calls = calls
        calls = [
            (
                "Task",
                {
                    "description": "Write fixture files",
                    "prompt": "Write the two fixture files",
                    "subagent_type": "general-purpose",
                },
            )
        ]
    else:
        assert kind == "hosted_tool"
        calls = [("WebSearch", {"query": "fixture policy coverage"})]
        deny = []

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        calls,
        policy="allow_deny",
        permission_mode=mode,
        subagent_calls=subagent_calls,
        layers={"namespace": {"deny": deny}},
    )
    denied_report = (
        run_scenario(
            tmp_path,
            workspace,
            account,
            markers,
            calls,
            policy="allow_deny",
            permission_mode=mode,
            layers={"namespace": {"deny": ["WebSearch"]}},
        )
        if kind == "hosted_tool"
        else report
    )

    assert_session(report, allowed, seeds)
    assert_diagnostics(report, "completed", denied)
    if kind == "hosted_tool":
        assert_session(denied_report, denied, seeds)
        assert_call(report, "toolu_scripted_0", "WebSearch", allowed=True)
        assert_call(
            denied_report,
            "toolu_scripted_0",
            "WebSearch",
            allowed=False,
            reason="namespace.deny.0",
        )
        assert web_requests(report) >= 1
        assert web_requests(denied_report) == 0
        assert_totals(report, {"toolu_scripted_0"}, set())
        assert_totals(denied_report, {"toolu_scripted_0"}, {"toolu_scripted_0"})
        return
    prefix = "toolu_scripted_subagent_" if kind == "subagent" else "toolu_scripted_"
    identities = scripted_ids("toolu_scripted_", len(calls)) | scripted_ids(
        "toolu_scripted_subagent_", len(subagent_calls)
    )
    refused = {prefix + "1"}
    if kind == "file_edit":
        refused |= {"toolu_scripted_3", "toolu_scripted_5"}
    if mode == "plan":
        refused.add(prefix + "0")
        if kind == "file_edit":
            refused |= {"toolu_scripted_7", "toolu_scripted_9"}
    assert_totals(report, identities, refused)
    tool = "Bash" if kind == "shell" else "Write"
    assert_call(
        report,
        prefix + "0",
        tool,
        allowed=True,
        harness_denied=mode == "plan",
        subagent=kind == "subagent",
    )
    assert_call(
        report,
        prefix + "1",
        tool,
        allowed=False,
        subagent=kind == "subagent",
        reason="namespace.deny.0",
    )
    assert not denied.exists()
    if mode == "plan":
        assert not allowed.exists()
    elif kind == "shell":
        assert allowed.is_file()
    else:
        assert allowed.read_bytes() == SCRIPTED.encode()
    if kind == "subagent":
        assert_call(
            report, "toolu_scripted_0", "Agent", allowed=True, stream_tool="Task"
        )
    if kind == "file_edit":
        assert_call(report, "toolu_scripted_2", "Read", allowed=True)
        assert_call(report, "toolu_scripted_4", "Read", allowed=True)
        assert_call(
            report, "toolu_scripted_3", "Edit", allowed=False, reason="namespace.deny.1"
        )
        assert_call(
            report,
            "toolu_scripted_5",
            "NotebookEdit",
            allowed=False,
            reason="namespace.deny.2",
        )
        assert_call(report, "toolu_scripted_6", "Read", allowed=True)
        assert_call(report, "toolu_scripted_8", "Read", allowed=True)
        for identity, tool_name in (
            ("toolu_scripted_7", "Edit"),
            ("toolu_scripted_9", "NotebookEdit"),
        ):
            assert_call(
                report, identity, tool_name, allowed=True, harness_denied=mode == "plan"
            )
        if mode == "plan":
            assert editable_text.read_text() == SEEDED
            assert notebook_source(editable_notebook) == "original = 1\n"
        else:
            assert editable_text.read_text() == CHANGED
            assert notebook_source(editable_notebook) == CHANGED_SOURCE


@pytest.mark.parametrize("mode", ["bypass", "dont_ask"])
@pytest.mark.parametrize(
    "form",
    [
        "allow_listed_command_reaches_callback",
        "protected_path_edit",
        "shell_indirection",
    ],
)
def test_required_policy_is_enforced_or_rejected_for_policy_forms(
    tmp_path: Path, mode: str, form: str
) -> None:
    workspace, account, markers = prepare_scenario(tmp_path)
    seeds = seed_files(workspace)
    target = workspace / "allowed.txt"
    protected_paths: tuple[str, ...] = ()
    calls: list[tuple[str, dict[str, object]]]
    if form == "protected_path_edit":
        calls = edit_calls(workspace)
        allow = ["Read", "Edit"]
        protected_paths = ("seeded.txt",)
        tool, identity, reason = "Edit", "toolu_scripted_1", "protected_path"
    elif form == "shell_indirection":
        target = workspace / "denied.txt"
        calls = [
            (
                "Bash",
                {
                    "command": "bash -c "
                    + shlex.quote("touch " + shlex.quote(str(target)))
                },
            )
        ]
        allow = ["Bash(bash *)"]
        tool, identity, reason = "Bash", "toolu_scripted_0", "not_allowed"
    else:
        calls = [("Bash", {"command": "touch " + shlex.quote(str(target))})]
        allow = ["Bash(touch *)"]
        tool, identity, reason = "Bash", "toolu_scripted_0", "allowed"

    report = run_scenario(
        tmp_path,
        workspace,
        account,
        markers,
        calls,
        policy="allow_deny",
        permission_mode=mode,
        layers={"node": {"allow": allow}},
        protected_paths=protected_paths,
    )

    assert_session(report, target, seeds)
    assert_call(report, identity, tool, allowed=reason == "allowed", reason=reason)
    assert_totals(
        report,
        scripted_ids("toolu_scripted_", len(calls)),
        set() if reason == "allowed" else {identity},
    )
    assert target.is_file() is (reason == "allowed")
    if form == "protected_path_edit":
        assert_call(report, "toolu_scripted_0", "Read", allowed=True)
        assert_diagnostics(report, "completed", workspace / "seeded.txt")
