import re
from pathlib import Path

import pytest

from kora.plugins.harness.codex.launch_configuration import (
    HOME_ENV_NAMES,
    OWNED_ENV_NAMES,
    CodexLaunchProfile,
    LaunchConfigurationError,
    LaunchConfigurationFailure,
    NativeHomes,
    build_launch_inputs,
)
from kora.plugins.harness.codex.reservations import RESERVED_ENV


def test_codex_reserved_env_is_complete_and_well_formed(tmp_path: Path) -> None:
    profile = CodexLaunchProfile(
        "fixture/model:2026", "https://responses.example/v1", "read-only"
    )
    bash = Path("/usr/bin/bash")
    app_homes = NativeHomes(
        tmp_path / "app-home", tmp_path / "app-codex", tmp_path / "app-tmp"
    )
    exec_homes = NativeHomes(
        tmp_path / "exec-home", tmp_path / "exec-codex", tmp_path / "exec-tmp"
    )
    required = {
        "CODEX_*",
        "OPENAI_*",
        "KORA_*",
        "RUST_*",
        "BASH_FUNC_*",
        "LD_*",
        "DYLD_*",
        "PATH",
        "PWD",
        "TRACEPARENT",
        "TRACESTATE",
        "ENV",
    }

    inputs = build_launch_inputs(
        profile,
        executable=Path("/installed/codex"),
        bash=bash,
        workspace=tmp_path / "workspace",
        app_homes=app_homes,
        exec_homes=exec_homes,
        executor_digest="ab" * 32,
    )
    declared = set(RESERVED_ENV)

    refusals: list[tuple[str, LaunchConfigurationFailure]] = []
    for name in sorted(OWNED_ENV_NAMES):
        for workload, secrets in (((name,), ()), ((), (name,))):
            with pytest.raises(LaunchConfigurationError) as rejected:
                build_launch_inputs(
                    profile,
                    executable=Path("/installed/codex"),
                    bash=bash,
                    workspace=tmp_path / "workspace",
                    app_homes=app_homes,
                    exec_homes=exec_homes,
                    executor_digest="ab" * 32,
                    workload_names=workload,
                    workload_secret_names=secrets,
                )

            refusals.append((name, rejected.value.reason))

    assert inputs.app_server.environment.keys() <= declared
    assert inputs.exec_server.environment.keys() <= declared
    assert HOME_ENV_NAMES == app_homes.assignments(bash).keys()
    assert HOME_ENV_NAMES == exec_homes.assignments(bash).keys()
    assert required <= declared
    assert len(declared) == len(RESERVED_ENV)
    assert RESERVED_ENV == tuple(sorted(declared))
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\*?", name) for name in declared)
    assert all(
        reason is LaunchConfigurationFailure.ENVIRONMENT_CONFLICT
        for _, reason in refusals
    )
    assert OWNED_ENV_NAMES <= declared
    assert {name for name, _ in refusals} == OWNED_ENV_NAMES
