from collections.abc import Mapping
from pathlib import Path
from typing import Final

from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretValue

CANDIDATE_PATH: Final[str] = "/usr/bin:/bin"


def candidate_environment(
    *,
    cwd: Path,
    attempt_token: SecretValue,
    workload: Mapping[str, str],
    secrets: Mapping[str, SecretValue],
    harness_credentials: Mapping[str, SecretValue],
    adapter: Mapping[str, str],
) -> dict[str, str]:
    runtime: dict[str, str | SecretValue] = {
        "PATH": CANDIDATE_PATH,
        "PWD": str(cwd),
        "KORA_ATTEMPT_TOKEN": attempt_token,
    }
    owners: tuple[Mapping[str, str | SecretValue], ...] = (
        runtime,
        workload,
        secrets,
        harness_credentials,
        adapter,
    )
    captured: dict[str, str | SecretValue] = {}
    for owner in owners:
        for name, value in owner.items():
            if name in captured:
                raise KoraProtocolError(
                    "launch_rejected",
                    "the candidate environment has conflicting owners",
                    False,
                    {},
                )
            captured[name] = value
    return {
        name: value.reveal() if isinstance(value, SecretValue) else value
        for name, value in captured.items()
    }
