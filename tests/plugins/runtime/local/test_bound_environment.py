from pathlib import Path
from typing import Final, Literal

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.environment import (
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused, SecretScope

__all__ = ["kora_capture"]

APP: Final[str] = "app-server"
EXEC: Final[str] = "exec-server"
PROVIDER_KEY: Final[str] = "CODEX_API_KEY"
TOKEN: Final[str] = bytes(range(16)).hex()


def assert_owner_refusal(error: KoraProtocolError) -> None:
    assert error.code == "launch_rejected"
    assert error.message == "the candidate environment has conflicting owners"
    assert error.args == (error.code, error.message)
    assert not error.retryable
    assert error.details == {}
    assert error.__context__ is None
    assert error.__cause__ is None


def test_bound_environment_checks_later_role_before_reveal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    app_scope = SecretScope(kora_capture.diagnostics)
    credentials = {PROVIDER_KEY: app_scope.admit("released-provider-key")}
    app_scope.release()
    roles = {
        APP: CandidateRoleEnvironment({}, (PROVIDER_KEY,)),
        EXEC: CandidateRoleEnvironment({"PATH": "/unexpected"}, ()),
    }
    token = kora_capture.scope.admit(TOKEN)

    with pytest.raises(KoraProtocolError) as rejected:
        bind_candidate_environments(
            cwd=tmp_path,
            attempt_token=token,
            workload={},
            secrets={},
            harness_credentials=credentials,
            roles=roles,
        )

    assert_owner_refusal(rejected.value)
    assert not kora_capture.scope.released


@pytest.mark.parametrize(
    ("app_grants", "exec_grants", "available"),
    [
        ((PROVIDER_KEY,), (), ()),
        ((PROVIDER_KEY,), (), (PROVIDER_KEY, "UNEXPECTED")),
        ((PROVIDER_KEY, PROVIDER_KEY), (), (PROVIDER_KEY,)),
        ((PROVIDER_KEY,), (PROVIDER_KEY,), (PROVIDER_KEY,)),
    ],
    ids=("missing", "unexpected", "repeated", "shared"),
)
def test_bound_environment_requires_exact_distinct_grants(
    tmp_path: Path,
    kora_capture: Capture,
    app_grants: tuple[str, ...],
    exec_grants: tuple[str, ...],
    available: tuple[str, ...],
) -> None:
    released = SecretScope(kora_capture.diagnostics)
    token = released.admit(TOKEN)
    credentials = {
        name: released.admit("released-provider-value") for name in available
    }
    released.release()
    roles = {
        APP: CandidateRoleEnvironment({}, app_grants),
        EXEC: CandidateRoleEnvironment({}, exec_grants),
    }

    with pytest.raises(KoraProtocolError) as rejected:
        bind_candidate_environments(
            cwd=tmp_path,
            attempt_token=token,
            workload={},
            secrets={},
            harness_credentials=credentials,
            roles=roles,
        )

    assert_owner_refusal(rejected.value)


@pytest.mark.parametrize("owner", ["text", "secret", "exec"])
def test_bound_environment_reserves_grants_across_roles(
    tmp_path: Path, kora_capture: Capture, owner: Literal["text", "secret", "exec"]
) -> None:
    released = SecretScope(kora_capture.diagnostics)
    token = released.admit(TOKEN)
    credentials = {PROVIDER_KEY: released.admit("released-provider-key")}
    workload = {PROVIDER_KEY: "authored-text"} if owner == "text" else {}
    workload_secrets = (
        {PROVIDER_KEY: released.admit("authored-secret")} if owner == "secret" else {}
    )
    exec_adapter = {PROVIDER_KEY: "unexpected-exec-key"} if owner == "exec" else {}
    released.release()
    roles = {
        APP: CandidateRoleEnvironment({}, (PROVIDER_KEY,)),
        EXEC: CandidateRoleEnvironment(exec_adapter, ()),
    }

    with pytest.raises(KoraProtocolError) as rejected:
        bind_candidate_environments(
            cwd=tmp_path,
            attempt_token=token,
            workload=workload,
            secrets=workload_secrets,
            harness_credentials=credentials,
            roles=roles,
        )

    assert_owner_refusal(rejected.value)


def test_bound_environment_materializes_only_the_requested_role(
    tmp_path: Path, kora_capture: Capture
) -> None:
    app_scope = SecretScope(kora_capture.diagnostics)
    token = kora_capture.scope.admit(TOKEN)
    workload = {"WORKLOAD_TEXT": "captured-text"}
    workload_secrets = {"WORKLOAD_SECRET": kora_capture.scope.admit("captured-secret")}
    credentials = {PROVIDER_KEY: app_scope.admit("synthetic-provider-key")}
    app_adapter = {"NATIVE_HOME": "app-home"}
    exec_adapter = {"NATIVE_HOME": "exec-home"}
    roles = {
        APP: CandidateRoleEnvironment(app_adapter, (PROVIDER_KEY,)),
        EXEC: CandidateRoleEnvironment(exec_adapter, ()),
    }
    expected = {
        "PATH": "/usr/bin:/bin",
        "PWD": str(tmp_path),
        "KORA_ATTEMPT_TOKEN": TOKEN,
        "WORKLOAD_TEXT": "captured-text",
        "WORKLOAD_SECRET": "captured-secret",
    }

    try:
        binding = bind_candidate_environments(
            cwd=tmp_path,
            attempt_token=token,
            workload=workload,
            secrets=workload_secrets,
            harness_credentials=credentials,
            roles=roles,
        )
        workload["WORKLOAD_TEXT"] = "later-text"
        workload_secrets.clear()
        credentials.clear()
        app_adapter.clear()
        exec_adapter["NATIVE_HOME"] = "later-home"
        roles.clear()
        app = binding.materialize(APP)
        exec_result = binding.materialize(EXEC)
        binding.materialize(EXEC)["WORKLOAD_TEXT"] = "later-result"
        app_scope.release()
        after_release = binding.materialize(EXEC)
        with pytest.raises(SecretRevealRefused) as rejected:
            binding.materialize(APP)
        with pytest.raises(KoraProtocolError) as unknown_role:
            binding.materialize("unknown-role")
    finally:
        app_scope.release()

    assert list(app.items()) == list(
        {
            **expected,
            PROVIDER_KEY: "synthetic-provider-key",
            "NATIVE_HOME": "app-home",
        }.items()
    )
    assert list(exec_result.items()) == list(
        {**expected, "NATIVE_HOME": "exec-home"}.items()
    )
    assert after_release == exec_result
    assert str(rejected.value) == "the secret's scope is released"
    assert_owner_refusal(unknown_role.value)
    assert not kora_capture.scope.released
