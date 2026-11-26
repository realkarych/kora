import json
import shlex
from http.client import HTTPConnection
from pathlib import Path
from socket import create_connection

import pytest

from tests.plugins.harness.claude.contained import assert_no_outbound
from tests.plugins.harness.claude.scripted_assertions import (
    SCRIPTED,
    SEEDED,
    assert_diagnostics,
    event_data,
    prepare_scenario,
)
from tests.plugins.harness.claude.scripted_model import (
    KEY,
    ScriptedState,
    objects,
    serve,
    stop_server,
)
from tests.plugins.harness.claude.test_session_isolation import run_scenario


def count_tokens(connection: HTTPConnection, api_key: str) -> int:
    connection.request(
        "POST", "/v1/messages/count_tokens", body=b"{}", headers={"x-api-key": api_key}
    )
    with connection.getresponse() as response:
        response.read()
        return response.status


def test_scripted_messages_counts_a_refusal_before_a_successful_request() -> None:
    state = ScriptedState([])
    server, thread, _ = serve(state)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    try:
        refused = count_tokens(connection, "incorrect-fixture-key")
        accepted = count_tokens(connection, KEY)
    finally:
        connection.close()
        stop_server(server, thread)
    bodies, clients, unauthorized = state.observed_requests()

    assert (refused, accepted) == (401, 200)
    assert bodies == ["{}", "{}"]
    assert clients == ["127.0.0.1", "127.0.0.1"]
    assert unauthorized == 1


@pytest.mark.parametrize("body_prefix", [b"", b"\xc3"])
def test_scripted_server_settles_a_request_that_is_waiting_for_its_body(
    body_prefix: bytes,
) -> None:
    state = ScriptedState([])
    server, thread, _ = serve(state)

    try:
        with create_connection(("127.0.0.1", server.server_port), timeout=5) as client:
            client.sendall(
                b"POST /v1/messages HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 2\r\n"
                b"Expect: 100-continue\r\n"
                b"x-api-key: incorrect-fixture-key\r\n\r\n" + body_prefix
            )
            with client.makefile("rb") as response:
                assert response.readline() == b"HTTP/1.1 100 Continue\r\n"
                assert response.readline() == b"\r\n"

                stop_server(server, thread)
                snapshot = state.observed_requests()

                assert snapshot == ([], ["127.0.0.1"], 1)
                assert response.read(1) == b""
    finally:
        if thread.is_alive():
            stop_server(server, thread)


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
