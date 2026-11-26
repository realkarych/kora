import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from kora.api.graph_deployment_limits import READ
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.tables import graph, graph_version, run
from kora.engine.graph_version_identity import (
    GraphVersionPin,
    read_graph_version_identity,
)
from kora.ir.json import JsonObject, parse_json_object
from kora.models.errors import ApiErrorEnvelope, ValidationDetails
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, NAMESPACE, preparer
from tests.api.support import api_capture, application, deployment_services
from tests.cli.test_deploy_command import bearer, closed_origin, kora
from tests.client.support import running_app
from tests.engine.graph_output_cases import child
from tests.support.observability import ProcessResult, run_python
from tests.support.provider_tooling import create_virtual_environment, run_setup

__all__ = ["api_capture", "preparer"]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/walking-skeleton/graph.py"
SOURCE_LIMIT = 65_536
PRIVATE_FORMAT = "private-format-marker"
CHECK_SOURCE_CLOSURE = """
from os import fstat, listdir, stat
from pathlib import Path
from sys import argv

from kora.dsl.compiler import CompiledGraph, CompileFindings, compile_graph

def descriptors_for(path):
    target = stat(path)
    owned = []
    for name in listdir("/dev/fd"):
        try:
            opened = fstat(int(name))
        except OSError:
            continue
        if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
            owned.append(name)
    return owned

for index, source in enumerate(map(Path, argv[1:])):
    outcome = CompiledGraph if index == 0 else CompileFindings
    assert descriptors_for(source) == []
    assert isinstance(compile_graph(source), outcome)
    assert descriptors_for(source) == []
"""
CHECK_BASE_INSTALL = """
from importlib.util import find_spec
from sys import argv

from kora.__main__ import main

assert find_spec("cel") is None
argv[1:] = ["compile", argv[1], "--format", "json"]
main()
"""


async def compile_json(path: Path) -> ProcessResult:
    return await kora(["compile", str(path), "--format", "json"], {})


def findings_from(result: ProcessResult) -> ValidationDetails:
    (details,) = (row for row in result.records() if "errors" in row)
    return ValidationDetails.model_validate(details)


def write_source(path: Path, source: bytes) -> Path:
    path.write_bytes(source)
    return path


async def saved_documents(database: Database) -> list[JsonObject]:
    documents: list[JsonObject] = []
    async with database.read_transaction() as reader:
        parent = (await reader.execute(select(graph))).one()
        versions = (
            await reader.execute(select(graph_version).order_by(graph_version.c.number))
        ).all()
        for version in versions:
            acquired = await acquire_graph_version(
                reader,
                parent.namespace_id,
                parent.id,
                version.id,
                max_encoded_bytes=READ.identity.max_encoded_bytes,
            )
            assert acquired is not None
            identity = read_graph_version_identity(
                acquired,
                GraphVersionPin(
                    parent.id, version.id, version.execution_contract_digest
                ),
                limits=READ.identity,
            )
            documents.append(parse_json_object(identity.values.ir))
    return documents


@pytest.mark.asyncio
async def test_cli_compile_deploys_and_starts_the_checked_example(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    token = await bearer(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, "claude")
    )
    original = EXAMPLE.read_bytes()
    same_source = write_source(tmp_path / "same.py", original)
    modified = write_source(
        tmp_path / "modified.py", original.replace(b'"checked"', b'"checked again"')
    )
    mismatch = write_source(
        tmp_path / "mismatch.py", original.replace(b'"claude"', b'"fake"')
    )

    first = await compile_json(EXAMPLE)
    repeated = await compile_json(same_source)
    changed = await compile_json(modified)
    wrong_harness = await compile_json(mismatch)
    first_ir = write_source(tmp_path / "first.ir.json", first.stdout)
    repeated_ir = write_source(tmp_path / "equal.ir.json", repeated.stdout)
    modified_ir = write_source(tmp_path / "modified.ir.json", changed.stdout)
    mismatch_ir = write_source(tmp_path / "mismatch.ir.json", wrong_harness.stdout)
    async with running_app(app) as origin:
        environment = {"KORA_URL": origin, "KORA_TOKEN": token}
        published = await kora(["deploy", str(first_ir), "--json"], environment)
        reused = await kora(["deploy", str(repeated_ir), "--json"], environment)
        started = await kora(
            ["trigger", GRAPH, "--namespace", NAMESPACE, "--json"], environment
        )
        republished = await kora(["deploy", str(modified_ir), "--json"], environment)
        refused = await kora(["deploy", str(mismatch_ir), "--json"], environment)
    documents = await saved_documents(database)
    async with database.read_transaction() as reader:
        saved_run = (await reader.execute(select(run))).one()

    assert (first.code, repeated.code, changed.code, wrong_harness.code) == (0, 0, 0, 0)
    assert first.stdout == repeated.stdout
    assert first.stdout.endswith(b"\n")
    assert not first.stdout.endswith(b"\n\n")
    assert (
        published.code,
        reused.code,
        started.code,
        republished.code,
        refused.code,
    ) == (0, 0, 0, 0, 1)
    initial = DeployResponse.model_validate_json(published.stdout)
    replay = DeployResponse.model_validate_json(reused.stdout)
    updated = DeployResponse.model_validate_json(republished.stdout)
    assert (initial.number, initial.reused, replay.number, replay.reused) == (
        1,
        False,
        1,
        True,
    )
    assert replay.graph_version_id == initial.graph_version_id
    assert replay.deployment_digest == initial.deployment_digest
    assert (updated.number, updated.reused) == (2, False)
    assert updated.graph_version_id != initial.graph_version_id
    assert updated.ir_digest != initial.ir_digest
    accepted = AcceptedRun.model_validate_json(started.stdout)
    assert (accepted.graph_version_id, accepted.status) == (
        initial.graph_version_id,
        "pending",
    )
    assert (saved_run.id, saved_run.graph_version_id, saved_run.status) == (
        accepted.run_id.removeprefix("run_"),
        initial.graph_version_id.removeprefix("gv_"),
        "pending",
    )
    assert documents == [
        parse_json_object(json.loads(first.stdout)),
        parse_json_object(json.loads(changed.stdout)),
    ]
    assert_skeleton(documents[0], original)
    assert child(documents[1], "graph", "nodes", "gate", "config")["cmd"] == [
        "printf",
        "checked again",
    ]
    error = ApiErrorEnvelope.model_validate_json(refused.stdout).error
    assert error.code == "API003"
    installation = ValidationDetails.model_validate(error.details)
    (finding,) = installation.errors
    assert (finding.code, finding.path) == (
        "IR018",
        ("graph", "nodes", "agent", "config", "harness"),
    )
    assert finding.location is not None
    assert (finding.location.file, finding.location.line) == ("graph.py", 8)


def assert_skeleton(document: JsonObject, source: bytes) -> None:
    assert document["ir_version"] == "1.0"
    assert (
        child(document, "meta")["source_digest"]
        == f"sha256:{sha256(source).hexdigest()}"
    )
    assert document["type_contracts"] == {
        name: {"builtin_revision": "1"}
        for name in ("AgentSession", "CommandResult", "StructuredOutput")
    }
    assert document["runtime"] == {"runtime": "local", "vcs": "git"}
    body = child(document, "graph")
    assert (body["id"], body["namespace"]) == (GRAPH, NAMESPACE)
    assert body["params"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert body["triggers"] == [{"kind": "manual"}]
    nodes = child(body, "nodes")
    assert set(nodes) == {"agent", "gate", "approve"}
    agent = child(nodes, "agent")
    assert agent["kind"] == "agent"
    assert agent["config"] == {
        "harness": "claude",
        "prompt": "Summarize the repository.",
        "workspace": {"repo": "", "rev": "main"},
    }
    assert agent["outputs"] == {
        "session": {"type": "AgentSession"},
        "summary": {"type": "StructuredOutput"},
    }
    gate = child(nodes, "gate")
    assert (gate["kind"], gate["depends_on"]) == ("command", ["agent"])
    assert gate["config"] == {
        "cmd": ["printf", "checked"],
        "workspace": {"repo": "", "rev": "main"},
    }
    assert gate["outputs"] == {"result": {"type": "CommandResult"}}
    approval = child(nodes, "approve")
    assert approval["kind"] == "human_input"
    assert approval["config"] == {
        "channel": "cli",
        "timeout": "1h",
        "on_expiry": "fail",
    }
    assert approval["inputs"] == {
        "gate": {"type": "CommandResult", "from": "gate", "output": "result"}
    }
    assert approval["outputs"] == {"response": {"type": "StructuredOutput"}}


@pytest.mark.parametrize(
    ("fragment", "replacement", "code"),
    [
        (b'harness="claude",', b'harness="claude", harness="fake",', "DSL016"),
        (b'command("gate"', b'command("agent"', "DSL002"),
        (b"check = node.command", b"prepare = node.command", "DSL016"),
        (b"prepare = node.agent", b"node = node.agent", "DSL016"),
        (b"def skeleton()", b"def graph()", "DSL016"),
        (b"prepare >> check", b"check >> prepare", "DSL003"),
        (b"prepare >> check", b"prepare >>> check", "DSL016"),
        (b"gate=check", b"gate=prepare", "DSL003"),
        (b'cmd=["printf", "checked"]', b'args=["printf"]', "DSL011"),
        (b'timeout="1h"', b'timeout="not-a-duration"', "DSL016"),
        (b'"checked"', b'"{resource_" "root}"', "DSL016"),
        (b'"checked"', b'"{" "{private}}"', "DSL016"),
    ],
)
def test_cli_compile_refuses_outside_the_early_grammar(
    tmp_path: Path, fragment: bytes, replacement: bytes, code: str
) -> None:
    original = EXAMPLE.read_bytes()
    line = original[: original.index(fragment)].count(b"\n") + 1
    graph_path = write_source(
        tmp_path / "private-source-marker.py",
        original.replace(fragment, replacement),
    )

    result = run_python(
        ["-I", "-m", "kora", "compile", str(graph_path), "--format", "json"], {}
    )

    assert result.code != 0
    assert result.stdout == b""
    details = findings_from(result)
    (finding,) = details.errors
    assert finding.code == code
    assert finding.location is not None
    assert (finding.location.file, finding.location.line) == ("graph.py", line)
    assert b"private-source-marker" not in result.stderr
    assert str(tmp_path).encode() not in result.stderr
    assert (details.warnings, details.errors_omitted, details.warnings_omitted) == (
        (),
        0,
        0,
    )


@pytest.mark.parametrize(
    "invalid_source",
    [
        b"\xef\xbb\xbf",
        b"# coding: utf-8\n",
        b"\xff",
        b"\x00",
        b"\xed\xa0\x80",
    ],
)
@pytest.mark.asyncio
async def test_cli_compile_refuses_invalid_source_bytes(
    tmp_path: Path, invalid_source: bytes
) -> None:
    path = write_source(tmp_path / "raw.py", invalid_source + EXAMPLE.read_bytes())

    result = await compile_json(path)

    assert result.code != 0
    assert result.stdout == b""
    assert [finding.code for finding in findings_from(result).errors] == ["DSL016"]


@pytest.mark.asyncio
async def test_cli_compile_enforces_source_byte_and_token_limits(
    tmp_path: Path,
) -> None:
    original = EXAMPLE.read_bytes()
    maximum = original.ljust(SOURCE_LIMIT, b" ")
    within = write_source(tmp_path / "within.py", maximum)
    oversized = write_source(tmp_path / "oversized.py", maximum + b" ")
    wide = write_source(
        tmp_path / "wide.py",
        original.replace(b'["printf", "checked"]', b"[" + b'"arg",' * 128 + b"]"),
    )

    allowed = await compile_json(within)
    byte_refusal = await compile_json(oversized)
    token_refusal = await compile_json(wide)

    assert allowed.code == 0
    for result in (byte_refusal, token_refusal):
        assert result.code != 0
        assert result.stdout == b""
        assert [item.code for item in findings_from(result).errors] == ["DSL016"]
    assert "64 KiB limit" in findings_from(byte_refusal).errors[0].message
    assert "256-token limit" in findings_from(token_refusal).errors[0].message


@pytest.mark.asyncio
async def test_cli_compile_refuses_nonregular_files_without_a_fifo_writer(
    tmp_path: Path,
) -> None:
    alias = tmp_path / "alias.py"
    alias.symlink_to(EXAMPLE)
    pipe = tmp_path / "pipe.py"
    os.mkfifo(pipe)

    symlink = await compile_json(alias)
    fifo = await compile_json(pipe)

    for result in (symlink, fifo):
        assert result.code != 0
        assert result.stdout == b""
        assert [finding.code for finding in findings_from(result).errors] == ["DSL016"]


def test_cli_compile_does_not_execute_or_log_source(tmp_path: Path) -> None:
    marker = tmp_path / "private-side-effect-marker"
    source = (
        EXAMPLE.read_bytes()
        .replace(b'"checked"', b'"private-argv-marker"')
        .replace(b"Summarize the repository.", b"private-prompt-marker")
    )
    path = write_source(tmp_path / "private-source.py", source)
    invalid = write_source(
        tmp_path / "private-invalid.py",
        source + f'open("{marker}", "w").close()\n'.encode(),
    )
    environment = {
        "KORA_URL": closed_origin(),
        "KORA_TOKEN": "private-credential-marker",
        "KORA_LOG_LEVEL": "DEBUG",
    }

    valid = run_python(
        ["-I", "-m", "kora", "compile", str(path), "--format", "json"], environment
    )
    refused = run_python(
        ["-I", "-m", "kora", "compile", str(invalid), "--format", "json"], environment
    )

    assert (valid.code, refused.stdout) == (0, b"")
    assert refused.code != 0
    assert not marker.exists()
    assert set(tmp_path.iterdir()) == {path, invalid}
    assert b"private-argv-marker" in valid.stdout
    assert b"private-prompt-marker" in valid.stdout
    assert b"private-" not in valid.stderr + refused.stderr
    assert sha256(source).hexdigest().encode() not in valid.stderr + refused.stderr
    assert str(tmp_path).encode() not in valid.stderr + refused.stderr
    assert any(row.get("event") == "cli.compile.finished" for row in valid.records())
    assert any(row.get("event") == "cli.compile.finished" for row in refused.records())


def test_cli_compile_closes_source_after_success_and_refusal(tmp_path: Path) -> None:
    invalid = write_source(tmp_path / "invalid.py", b"raise RuntimeError\n")
    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)

    result = run_python(
        ["-I", "-c", CHECK_SOURCE_CLOSURE, str(EXAMPLE), str(invalid), str(fifo)], {}
    )

    assert (result.code, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("literal", [rb'"\q"', rb'"\x00"', rb'"\ud800"', b'r"prefix"'])
def test_cli_compile_refuses_escaped_or_prefixed_literals_with_any_warning_filter(
    tmp_path: Path, literal: bytes
) -> None:
    path = write_source(
        tmp_path / "escape.py", EXAMPLE.read_bytes().replace(b'"checked"', literal)
    )
    arguments = ["-m", "kora", "compile", str(path), "--format", "json"]

    default = run_python(arguments, {})
    strict = run_python(arguments, {"PYTHONWARNINGS": "error"})

    assert default.code == strict.code != 0
    assert default.stdout == strict.stdout == b""
    assert findings_from(default) == findings_from(strict)
    assert [item.code for item in findings_from(strict).errors] == ["DSL016"]


@pytest.mark.asyncio
async def test_cli_compile_requires_explicit_json_without_echoing_the_value() -> None:
    arguments = ["compile", str(EXAMPLE)]

    omitted = await kora(arguments, {})
    refused = await kora(arguments + ["--format", PRIVATE_FORMAT], {})

    assert omitted.code == refused.code != 0
    assert omitted.stdout == refused.stdout == b""
    assert PRIVATE_FORMAT.encode() not in refused.stderr
    assert b"json" in omitted.stderr
    assert b"json" in refused.stderr


def test_cli_compile_runs_without_cel_extra(
    provider_tooling_wheelhouse: Path, tmp_path: Path
) -> None:
    python = create_virtual_environment(tmp_path / "base")
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            str(provider_tooling_wheelhouse),
            "--python",
            str(python),
            "kora",
        ]
    )

    result = subprocess.run(
        [str(python), "-I", "-c", CHECK_BASE_INSTALL, str(EXAMPLE)],
        env={"PATH": os.defpath},
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert_skeleton(parse_json_object(json.loads(result.stdout)), EXAMPLE.read_bytes())
