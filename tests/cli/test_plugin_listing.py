import json
import subprocess
from importlib.metadata import EntryPoint, distribution
from pathlib import Path
from typing import cast

import pytest

from kora.cli.conformance import CommandResult
from tests.support.observability import run_python
from tests.support.provider_tooling import (
    build_fixture_wheel,
    install_tooling,
)

DOCUMENTED_BUILTINS: frozenset[tuple[str, str]] = frozenset(
    {
        ("authentication", "static_token"),
        ("artifact_storage", "local"),
        ("artifact_type", "vcs_ref"),
        ("artifact_type", "command_result"),
        ("artifact_type", "agent_session"),
        ("artifact_type", "structured_output"),
        ("policy", "allow_deny"),
        ("secrets", "env"),
        ("vcs", "git"),
    }
)
TEST_DOUBLE_PACKAGES: tuple[tuple[str, str], ...] = (
    ("kora", "testing"),
    ("kora", "conformance"),
)


def builtin_entry_points() -> list[EntryPoint]:
    return list(distribution("kora").entry_points.select(group="kora.provider"))


def provider_key(entry: EntryPoint) -> tuple[str, str]:
    protocol, _, name = entry.name.partition(".")
    return (protocol, name)


def builtin_providers() -> list[tuple[str, str]]:
    return sorted(provider_key(entry) for entry in builtin_entry_points())


def is_test_double(entry: EntryPoint) -> bool:
    packages = entry.module.split(".")
    return (
        provider_key(entry)[1] == "fake"
        or "fake" in packages
        or tuple(packages[:2]) in TEST_DOUBLE_PACKAGES
    )


def listed(providers: list[dict[str, object]]) -> list[tuple[str, str]]:
    return [(str(entry["protocol"]), str(entry["name"])) for entry in providers]


@pytest.mark.parametrize("schema", [False, True])
def test_provider_listing_exposes_installed_core_metadata(schema: bool) -> None:
    arguments = ["-I", "-m", "kora", "plugins", "list", "--json"]
    if schema:
        arguments.append("--schema")
    environment = {"KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG"}

    result = run_python(arguments, environment)
    payload: object = json.loads(result.stdout) if result.stdout else None

    assert result.code == 0
    assert isinstance(payload, dict)
    output = cast(dict[str, object], payload)
    assert set(output) == {"providers", "failures"}
    assert output["failures"] == []
    providers = cast(list[dict[str, object]], output["providers"])
    assert not [entry.name for entry in builtin_entry_points() if is_test_double(entry)]
    assert DOCUMENTED_BUILTINS <= set(listed(providers))
    assert listed(providers) == builtin_providers()
    for entry in providers:
        assert set(entry) == {"protocol", "name", "version"} | (
            {"config_schema"} if schema else set()
        )
        assert entry["version"]
        if schema:
            assert isinstance(entry["config_schema"], dict)
    records = result.records()
    assert len(records) == len(builtin_providers())
    assert all(
        record["event"] == "protocols.loading.provider_loaded" for record in records
    )
    assert all(record["component"] == "protocols.loading" for record in records)


@pytest.mark.parametrize("schema", [False, True])
def test_provider_listing_reports_failures(
    tmp_path: Path, provider_tooling_wheelhouse: Path, schema: bool
) -> None:
    fixture = build_fixture_wheel(
        tmp_path,
        {
            "policy.external": "kora_tooling_fixture.noisy_import:provider_metadata",
            "policy.allow_deny": "kora.testing:must_not_import",
            "secrets.broken": "kora_tooling_fixture.providers:broken",
            "secrets.import_broken": "kora_tooling_fixture.failing_import:constructor",
            "runtime.fake": "kora.testing:must_not_import",
            "unprefixed": "kora_tooling_fixture.providers:constructor",
        },
    )
    installed = install_tooling(tmp_path, provider_tooling_wheelhouse, fixture)
    arguments = ["plugins", "list", "--json"] + (["--schema"] if schema else [])
    environment = {
        "KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG",
        "PYTHONPATH": str(tmp_path),
        "PYTHONHOME": str(tmp_path),
        "KORA_TOKEN": "private-deployment-credential-248",
        "PYTEST_ADDOPTS": "private-pytest-setting-248",
        "OPENAI_API_KEY": "private-provider-credential-248",
    }

    result = installed.run(*arguments, environment=environment)
    human = installed.run("plugins", "list", environment=environment)
    payload = cast(dict[str, object], json.loads(result.stdout))
    probe = subprocess.run(
        [
            str(installed.python),
            "-I",
            "-c",
            "import importlib.util; "
            "assert importlib.util.find_spec('pytest') is None; "
            "assert importlib.util.find_spec('jsonschema') is not None; "
            "assert importlib.util.find_spec('referencing') is not None",
        ],
        check=False,
        capture_output=True,
        cwd=installed.outside,
    )

    assert result.code == 1, result.stderr
    assert human.code == 1, human.stderr
    assert b"policy.external 1.0.0" in human.stdout
    assert b"provider_ambiguous" in human.stdout
    assert b"private-" not in human.stdout + human.stderr
    assert probe.returncode == 0, probe.stderr
    providers = cast(list[dict[str, object]], payload["providers"])
    expected = sorted(
        {*builtin_providers(), ("policy", "external")} - {("policy", "allow_deny")}
    )
    assert DOCUMENTED_BUILTINS - {("policy", "allow_deny")} <= set(listed(providers))
    assert listed(providers) == expected
    failures = cast(list[dict[str, object]], payload["failures"])
    assert len(failures) == 5
    assert {item["code"] for item in failures} == {
        "provider_name_invalid",
        "provider_reserved",
        "provider_load_failed",
        "provider_ambiguous",
    }
    assert b"private-" not in result.stdout + result.stderr
    assert len(result.records()) == len(expected) + len(failures)
    assert sum(
        record["event"] == "protocols.loading.provider_loaded"
        for record in result.records()
    ) == len(expected)
    assert all(("config_schema" in item) is schema for item in providers)


@pytest.mark.parametrize("provider", ["saturated", "largest_schema"])
def test_installed_provider_supports_listing_without_conformance_extra(
    tmp_path: Path, provider_tooling_wheelhouse: Path, provider: str
) -> None:
    name = "largest" if provider == "largest_schema" else provider
    fixture = build_fixture_wheel(
        tmp_path, {f"policy.{name}": f"kora_tooling_fixture.providers:{provider}"}
    )
    installed = install_tooling(tmp_path, provider_tooling_wheelhouse, fixture)
    environment = {"KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG"}

    result = installed.run(
        "plugins", "list", "--json", "--schema", environment=environment
    )
    payload = cast(dict[str, object], json.loads(result.stdout))
    human = installed.run("plugins", "list")

    assert result.code == 0, result.stderr
    assert human.code == 0, human.stderr
    assert payload["failures"] == []
    providers = cast(list[dict[str, object]], payload["providers"])
    selected = next(item for item in providers if item["name"] == name)
    assert len(result.records()) == len(builtin_providers()) + 1
    assert b"private-provider-output-248" not in result.stdout + result.stderr
    assert f"policy.{name} 1.0.0".encode() in human.stdout
    if provider == "largest_schema":
        encoded = json.dumps(selected["config_schema"], separators=(",", ":")).encode()
        assert len(encoded) == 1048576

    unavailable = installed.run(
        "conformance",
        "policy",
        name,
        "--fixtures",
        "kora_tooling_fixture.fixtures_policy",
        environment={"HOME": str(tmp_path)},
    )
    refusal = CommandResult.model_validate_json(unavailable.stdout)
    assert unavailable.code == 1
    assert refusal.reason == "refused"
    assert refusal.cleanup is None
    assert unavailable.stderr == b""


@pytest.mark.parametrize(
    ("name", "provider"), [("retained", "retained_file"), ("changed", "changed_socket")]
)
def test_listing_preserves_unexpected_files_and_rejects_changed_socket(
    tmp_path: Path, provider_tooling_wheelhouse: Path, name: str, provider: str
) -> None:
    fixture = build_fixture_wheel(
        tmp_path, {f"policy.{name}": f"kora_tooling_fixture.providers:{provider}"}
    )
    installed = install_tooling(tmp_path, provider_tooling_wheelhouse, fixture)

    result = installed.run("plugins", "list", "--json")
    prefix = "kora: provider discovery failed: state_retained:"
    roots = [
        Path(line.removeprefix(prefix))
        for line in result.stderr_lines()
        if line.startswith(prefix)
    ]

    assert result.code == 1
    assert result.stdout == b""
    assert len(roots) == 1
    root = roots[0]
    try:
        assert root.is_absolute()
        assert root.name.startswith("kora-list-")
        if name == "retained":
            assert sorted(item.name for item in root.iterdir()) == ["cwd"]
            assert (
                root / "cwd" / "provider-owned.txt"
            ).read_text() == "private-provider-output-248"
        else:
            assert sorted(item.name for item in root.iterdir()) == ["cwd", "e", "r"]
            assert (root / "r").stat().st_mode & 0o777 == 0o666
        assert b"cli.provider_listing_retained" in result.stderr
    finally:
        if name == "retained":
            (root / "cwd" / "provider-owned.txt").unlink()
        else:
            (root / "r").unlink()
            (root / "e").unlink()
        (root / "cwd").rmdir()
        root.rmdir()
