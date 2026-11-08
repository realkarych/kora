from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretValue

CANDIDATE_PATH: Final[str] = "/usr/bin:/bin"
EnvironmentValue = str | SecretValue
EnvironmentAssignments = Mapping[str, EnvironmentValue]


@dataclass(frozen=True, repr=False)
class CandidateRoleEnvironment:
    adapter: Mapping[str, str]
    harness_grants: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class BoundCandidateEnvironments:
    _roles: Mapping[str, EnvironmentAssignments]

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self._roles)

    def materialize(self, role: str) -> dict[str, str]:
        entries = self._roles.get(role)
        if entries is None:
            raise _environment_rejected()
        return _reveal_environment(entries)


def _environment_rejected() -> KoraProtocolError:
    return KoraProtocolError(
        "launch_rejected",
        "the candidate environment has conflicting owners",
        False,
        {},
    )


def _capture_environment(
    *,
    cwd: Path,
    attempt_token: SecretValue,
    workload: Mapping[str, str],
    secrets: Mapping[str, SecretValue],
    harness_credentials: Mapping[str, SecretValue],
    adapter: Mapping[str, str],
) -> dict[str, EnvironmentValue]:
    runtime: dict[str, EnvironmentValue] = {
        "PATH": CANDIDATE_PATH,
        "PWD": str(cwd),
        "KORA_ATTEMPT_TOKEN": attempt_token,
    }
    owners: tuple[EnvironmentAssignments, ...] = (
        runtime,
        workload,
        secrets,
        harness_credentials,
        adapter,
    )
    captured: dict[str, EnvironmentValue] = {}
    for owner in owners:
        for name, value in owner.items():
            if name in captured:
                raise _environment_rejected()
            captured[name] = value
    return captured


def _reveal_environment(entries: EnvironmentAssignments) -> dict[str, str]:
    return {
        name: value.reveal() if isinstance(value, SecretValue) else value
        for name, value in entries.items()
    }


def candidate_environment(
    *,
    cwd: Path,
    attempt_token: SecretValue,
    workload: Mapping[str, str],
    secrets: Mapping[str, SecretValue],
    harness_credentials: Mapping[str, SecretValue],
    adapter: Mapping[str, str],
) -> dict[str, str]:
    return _reveal_environment(
        _capture_environment(
            cwd=cwd,
            attempt_token=attempt_token,
            workload=workload,
            secrets=secrets,
            harness_credentials=harness_credentials,
            adapter=adapter,
        )
    )


def bind_candidate_environments(
    *,
    cwd: Path,
    attempt_token: SecretValue,
    workload: Mapping[str, str],
    secrets: Mapping[str, SecretValue],
    harness_credentials: Mapping[str, SecretValue],
    roles: Mapping[str, CandidateRoleEnvironment],
) -> BoundCandidateEnvironments:
    authored_text = dict(workload)
    authored_secrets = dict(secrets)
    credentials = dict(harness_credentials)
    recipes = tuple(
        (role, dict(recipe.adapter), recipe.harness_grants)
        for role, recipe in roles.items()
    )
    grant_names = tuple(name for _, _, grants in recipes for name in grants)
    granted = frozenset(grant_names)
    if (
        len(granted) != len(grant_names)
        or granted != credentials.keys()
        or not granted.isdisjoint(authored_text)
        or not granted.isdisjoint(authored_secrets)
        or any(not granted.isdisjoint(adapter) for _, adapter, _ in recipes)
    ):
        raise _environment_rejected()

    captured = {
        role: MappingProxyType(
            _capture_environment(
                cwd=cwd,
                attempt_token=attempt_token,
                workload=authored_text,
                secrets=authored_secrets,
                harness_credentials={name: credentials[name] for name in grants},
                adapter=adapter,
            )
        )
        for role, adapter, grants in recipes
    }
    return BoundCandidateEnvironments(MappingProxyType(captured))
