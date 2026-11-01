import itertools
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.environment import candidate_environment
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused, SecretScope, SecretValue

__all__ = ["kora_capture"]

Owner = Literal["workload", "secrets", "credentials", "adapter"]
OWNERS: Final[tuple[Owner, ...]] = ("workload", "secrets", "credentials", "adapter")
TOKEN: Final[str] = bytes(range(16)).hex()
COLLISIONS: Final[tuple[tuple[Owner | None, Owner, str], ...]] = (
    *(
        (left, right, "PRIVATE_NAME")
        for left, right in itertools.combinations(OWNERS, 2)
    ),
    *(
        (None, owner, name)
        for owner in OWNERS
        for name in ("PATH", "PWD", "KORA_ATTEMPT_TOKEN")
    ),
)


@dataclass
class Inputs:
    token: SecretValue
    workload: dict[str, str] = field(default_factory=dict[str, str])
    secrets: dict[str, SecretValue] = field(default_factory=dict[str, SecretValue])
    credentials: dict[str, SecretValue] = field(default_factory=dict[str, SecretValue])
    adapter: dict[str, str] = field(default_factory=dict[str, str])

    def assign(self, owner: Owner, name: str, value: str, scope: SecretScope) -> None:
        if owner == "workload":
            self.workload[name] = value
        elif owner == "secrets":
            self.secrets[name] = scope.admit(value)
        elif owner == "credentials":
            self.credentials[name] = scope.admit(value)
        else:
            self.adapter[name] = value

    def assemble(self, cwd: Path) -> dict[str, str]:
        return candidate_environment(
            cwd=cwd,
            attempt_token=self.token,
            workload=self.workload,
            secrets=self.secrets,
            harness_credentials=self.credentials,
            adapter=self.adapter,
        )


def test_local_candidate_environment_reaches_real_process(tmp_path: Path) -> None:
    probe = Path(__file__).parents[3] / "support" / "candidate_environment_probe.py"
    supervisor_environment = {
        "SUPERVISOR_CANARY": "supervisor-only-value",
        "PATH": "/supervisor-path",
        "PWD": "/supervisor-cwd",
        "LC_ALL": "C",
    }

    result = subprocess.run(
        [sys.executable, "-I", str(probe), "assemble", str(tmp_path)],
        env=supervisor_environment,
        check=False,
        capture_output=True,
        timeout=40,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"candidate environment verified\n"
    assert result.stderr == b""


@pytest.mark.parametrize(("left", "right", "name"), COLLISIONS)
@pytest.mark.parametrize("equal_values", [False, True])
def test_local_candidate_environment_refuses_duplicate_owners(
    tmp_path: Path,
    kora_capture: Capture,
    left: Owner | None,
    right: Owner,
    name: str,
    equal_values: bool,
) -> None:
    inputs = Inputs(kora_capture.scope.admit(TOKEN))
    controls = {
        "PATH": "/usr/bin:/bin",
        "PWD": str(tmp_path),
        "KORA_ATTEMPT_TOKEN": TOKEN,
    }
    original = controls[name] if left is None else "private-original-value"
    if left is not None:
        inputs.assign(left, name, original, kora_capture.scope)
    inputs.assign(
        right,
        name,
        original if equal_values else "private-conflicting-value",
        kora_capture.scope,
    )
    before = (
        dict(inputs.workload),
        dict(inputs.secrets),
        dict(inputs.credentials),
        dict(inputs.adapter),
    )

    with pytest.raises(KoraProtocolError) as rejected:
        inputs.assemble(tmp_path)

    error = rejected.value
    assert error.code == "launch_rejected"
    assert error.message == "the candidate environment has conflicting owners"
    assert error.args == (error.code, error.message)
    assert not error.retryable
    assert error.details == {}
    assert error.__context__ is None
    assert error.__cause__ is None
    assert before == (
        inputs.workload,
        inputs.secrets,
        inputs.credentials,
        inputs.adapter,
    )
    assert not kora_capture.scope.released


def test_local_candidate_environment_checks_ownership_before_reveal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    released = SecretScope(kora_capture.diagnostics)
    inputs = Inputs(released.admit(TOKEN))
    inputs.assign("secrets", "PRIVATE_NAME", "private-value", released)
    inputs.assign("adapter", "PRIVATE_NAME", "other-value", released)
    released.release()

    with pytest.raises(KoraProtocolError) as rejected:
        inputs.assemble(tmp_path)

    assert rejected.value.code == "launch_rejected"


@pytest.mark.parametrize("owner", ["token", "secrets", "credentials"])
def test_local_candidate_environment_preserves_secret_scope(
    tmp_path: Path,
    kora_capture: Capture,
    owner: Literal["token", "secrets", "credentials"],
) -> None:
    released = SecretScope(kora_capture.diagnostics)
    inputs = Inputs(kora_capture.scope.admit(TOKEN))
    if owner == "token":
        inputs.token = released.admit("released-token-value")
    else:
        inputs.assign(owner, "PRIVATE_NAME", "private-released-value", released)
    released.release()
    registrations = kora_capture.scope.registered_count()

    with pytest.raises(SecretRevealRefused) as rejected:
        inputs.assemble(tmp_path)

    assert str(rejected.value) == "the secret's scope is released"
    assert released.released
    assert not kora_capture.scope.released
    assert kora_capture.scope.registered_count() == registrations
