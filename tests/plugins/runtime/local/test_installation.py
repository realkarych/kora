import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir.json import JsonObject
from kora.plugins.runtime.local.installation import (
    INSTALLATION_LIMIT,
    InstallationConfiguration,
    RunnerRefused,
    read_installation,
    validate_section,
)
from kora.plugins.runtime.local.supervisor import EXIT_REFUSED
from kora.runner.filesystem import open_local_directory
from tests.support.local_runtime import state_root
from tests.support.observability import run_python

VALID: Final[JsonObject] = {
    "format": "kora.runner/1",
    "harness": {
        "claude": {
            "credentials": {"ANTHROPIC_API_KEY": "secret://env/ANTHROPIC_API_KEY"}
        }
    },
    "policy": {"allow_deny": {}},
}

RECORDING_RUNNER: Final[str] = """
import json
import sys
from kora.plugins.runtime.local.supervisor import RunnerRefused, run

def compose(diagnostics, installation):
    with open(sys.argv[5], "w") as received:
        harness = dict(installation.harness)
        policy = dict(installation.policy)
        json.dump({"harness": harness, "policy": policy}, received)
    raise RunnerRefused("recorded")

sys.exit(run(sys.argv[1:5], compose))
"""


def read_root(root: Path, capture: Capture) -> InstallationConfiguration:
    directory = open_local_directory(root)
    try:
        return read_installation(directory, capture.diagnostics, root)
    finally:
        directory.close()


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o644])
def test_valid_installation(tmp_path: Path, kora_capture: Capture, mode: int) -> None:
    root = state_root(tmp_path)
    path = root / "runner.json"
    path.write_text(json.dumps(VALID))
    path.chmod(mode)

    installation = read_root(root, kora_capture)

    assert installation.harness == VALID["harness"]
    assert installation.policy == VALID["policy"]
    assert isinstance(installation.harness, MappingProxyType)
    assert isinstance(installation.policy, MappingProxyType)


def test_absent_installation(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)

    installation = read_root(root, kora_capture)

    assert installation == InstallationConfiguration.empty()
    assert installation.harness == {}
    assert installation.policy == {}
    assert not (root / "runner.json").exists()


INVALID_SECTIONS: Final[list[tuple[JsonObject, str]]] = [
    (
        {
            "harness": {
                "claude": {"credentials": {"ANTHROPIC_API_KEY": "private-marker"}}
            }
        },
        "installation_invalid",
    ),
    ({"policy": {"allow_deny": {"unknown": True}}}, "installation_invalid"),
    ({"unknown": {}}, "installation_malformed"),
    ({"format": "kora.runner/2"}, "installation_format"),
    ({"format": 1}, "installation_malformed"),
    ({"harness": {"fake": {}}}, "installation_provider"),
    ({"harness": {"unregistered": {}}}, "installation_provider"),
    ({"policy": {"fake": {}}}, "installation_provider"),
    ({"policy": {"unregistered": {}}}, "installation_provider"),
    ({"harness": []}, "installation_malformed"),
    ({"policy": []}, "installation_malformed"),
    ({"harness": {"claude": []}}, "installation_malformed"),
    ({"policy": {"allow_deny": None}}, "installation_malformed"),
    (
        {"harness": {f"provider_{index}": {} for index in range(17)}},
        "installation_malformed",
    ),
    (
        {"policy": {f"provider_{index}": {} for index in range(17)}},
        "installation_malformed",
    ),
]


@pytest.mark.parametrize(("changes", "reason"), INVALID_SECTIONS)
def test_invalid_installation_sections(
    tmp_path: Path, kora_capture: Capture, changes: Mapping[str, object], reason: str
) -> None:
    root = state_root(tmp_path)
    path = root / "runner.json"
    path.write_text(json.dumps({**VALID, **changes}))
    path.chmod(0o600)

    with pytest.raises(RunnerRefused) as refused:
        read_root(root, kora_capture)

    assert refused.value.args == (reason,)


@pytest.mark.parametrize(
    "content",
    [
        b"not-json-private-marker",
        b"[]",
        b"null",
        b"{}",
        b'{"format":"kora.runner/1","format":"kora.runner/1","harness":{},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{},"claude":{}},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{"x":1,"x":2}},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{"x":NaN}},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{"x":Infinity}},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{"x":-Infinity}},"policy":{}}',
        b'{"format":"kora.runner/1","harness":{"claude":{"x":1e999}},"policy":{}}',
        b"\xff",
        '{"format":"kora.runner/1","harness":{},"policy":{}}'.encode("utf-16"),
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_malformed_installation(
    tmp_path: Path, kora_capture: Capture, content: bytes
) -> None:
    root = state_root(tmp_path)
    path = root / "runner.json"
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(RunnerRefused) as refused:
        read_root(root, kora_capture)

    assert refused.value.args == ("installation_malformed",)


def write_padded(root: Path, size: int) -> None:
    document = json.dumps(VALID).encode()
    path = root / "runner.json"
    path.write_bytes(document + b" " * (size - len(document)))
    path.chmod(0o600)


def test_installation_at_size_limit(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    write_padded(root, INSTALLATION_LIMIT)

    installation = read_root(root, kora_capture)

    assert installation.harness == VALID["harness"]
    assert installation.policy == VALID["policy"]


def test_installation_over_size_limit(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    write_padded(root, INSTALLATION_LIMIT + 1)

    with pytest.raises(RunnerRefused) as refused:
        read_root(root, kora_capture)

    assert refused.value.args == ("installation_too_large",)


def test_runner_entry_composes_with_the_file_configuration(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    path = root / "runner.json"
    path.write_text(json.dumps(VALID))
    path.chmod(0o600)
    received = tmp_path / "received.json"

    result = run_python(
        [
            "-I",
            "-c",
            RECORDING_RUNNER,
            str(root),
            "attempt-1",
            "domain-1",
            "c" * 32,
            str(received),
        ],
        {"KORA_RUNNER_INSTALLATION": str(tmp_path / "elsewhere.json")},
    )

    assert result.code == EXIT_REFUSED
    assert json.loads(received.read_text()) == {
        "harness": VALID["harness"],
        "policy": VALID["policy"],
    }


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "dangling_symlink",
        "directory",
        "group_writable",
        "other_writable",
        "fifo",
    ],
)
def test_unreadable_installation(
    tmp_path: Path, kora_capture: Capture, kind: str
) -> None:
    root = state_root(tmp_path)
    path = root / "runner.json"
    if kind in {"symlink", "dangling_symlink"}:
        target = root / "target.json"
        if kind == "symlink":
            target.write_text(json.dumps(VALID))
            target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text(json.dumps(VALID))
        path.chmod(0o620 if kind == "group_writable" else 0o602)

    with pytest.raises(RunnerRefused) as refused:
        read_root(root, kora_capture)

    assert refused.value.args == ("installation_unreadable",)


def test_hard_linked_installation(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    target = root / "target.json"
    target.write_text(json.dumps(VALID))
    target.chmod(0o600)
    os.link(target, root / "runner.json")

    with pytest.raises(RunnerRefused) as refused:
        read_root(root, kora_capture)

    assert refused.value.args == ("installation_unreadable",)


RECURSIVE: Final[JsonObject] = {
    "type": "object",
    "additionalProperties": {"$ref": "#"},
}


def nested(depth: int) -> JsonObject:
    section: JsonObject = {}
    for _ in range(depth):
        section = {"inner": section}
    return section


@pytest.mark.parametrize(
    ("schema", "section", "reason"),
    [
        ({"type": "object", "minProperties": -1}, {}, "installation_provider"),
        (
            {"type": "object", "$ref": "https://example.invalid/schema.json"},
            {},
            "installation_invalid",
        ),
        (RECURSIVE, nested(900), "installation_invalid"),
    ],
)
def test_provider_schema_failures_are_refusals(
    schema: JsonObject, section: JsonObject, reason: str
) -> None:
    with pytest.raises(RunnerRefused) as refused:
        validate_section(schema, section)

    assert refused.value.args == (reason,)


@pytest.mark.parametrize(
    "kind",
    [
        "valid",
        "absent",
        "fake",
        "scenario",
        "env",
        "unknown",
        "outside",
        "parent",
        "dot",
        "empty_component",
        "relative",
        "root",
        "prefix",
        "schema",
        "namespace",
        "extra_namespace",
        "empty_namespace",
    ],
)
def test_installation_secrets_section_is_validated(
    tmp_path: Path, kora_capture: Capture, kind: str
) -> None:
    root = state_root(tmp_path)
    section: JsonObject = {
        "directory": str(root / "credentials"),
        "namespace_prefixes": {"local-runner": ["ANTHROPIC_"]},
    }
    expected = "installation_invalid"
    provider = "file"
    if kind in {"fake", "scenario", "env", "unknown"}:
        provider = kind
        expected = "installation_provider"
    elif kind in {
        "outside",
        "parent",
        "dot",
        "empty_component",
        "relative",
        "root",
        "prefix",
    }:
        section["directory"] = {
            "outside": str(tmp_path / "credentials"),
            "parent": str(root) + "/../credentials",
            "dot": str(root) + "/./credentials",
            "empty_component": str(root) + "//credentials",
            "relative": "credentials",
            "root": str(root),
            "prefix": str(root) + "-other/credentials",
        }[kind]
    elif kind == "schema":
        section["namespace_prefixes"] = []
    elif kind == "namespace":
        section["namespace_prefixes"] = {"graph": ["ANTHROPIC_"]}
    elif kind == "extra_namespace":
        section["namespace_prefixes"] = {
            "local-runner": ["ANTHROPIC_"],
            "graph": ["ANTHROPIC_"],
        }
    elif kind == "empty_namespace":
        section["namespace_prefixes"] = {}
    document: JsonObject = {**VALID}
    if kind != "absent":
        document["secrets"] = {provider: section}
    (root / "runner.json").write_text(json.dumps(document))
    (root / "runner.json").chmod(0o600)

    if kind in {"valid", "absent"}:
        installation = read_root(root, kora_capture)

        assert installation.secrets == ({"file": section} if kind == "valid" else {})
        assert isinstance(installation.secrets, MappingProxyType)
    else:
        with pytest.raises(RunnerRefused) as refused:
            read_root(root, kora_capture)

        assert refused.value.args == (expected,)
