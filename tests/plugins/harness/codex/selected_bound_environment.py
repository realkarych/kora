import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    PROVIDER_KEY,
    CodexLaunchInputs,
    LaunchConfigurationError,
    LaunchConfigurationFailure,
)
from kora.plugins.runtime.local.environment import (
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused, SecretScope, SecretValue
from tests.plugins.harness.codex.selected_launch import (
    prepare_native_inputs,
    write_native_configuration,
)

BoundCase = Literal["ready", "path", "grant", "reserved"]
ATTEMPT_CANARY = "bound-environment-attempt-token"
TEXT_NAME = "KORA_BOUND_AUTHORED_TEXT"
TEXT_VALUE = "bound-environment-authored-text"
WORKLOAD_HANDLE_NAME = "KORA_BOUND_AUTHORED_SECRET"
WORKLOAD_HANDLE_VALUE = "bound-environment-authored-secret"


class BoundEnvironmentError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class BoundLaunch:
    inputs: CodexLaunchInputs
    app: dict[str, str]
    executor: dict[str, str]
    expected_app: dict[str, str]
    expected_executor: dict[str, str]


@dataclass(repr=False)
class BoundEnvironmentProbe:
    case: BoundCase
    attempt_scope: SecretScope
    app_scope: SecretScope
    starts: int = field(default=0, init=False)
    unix_sockets: int = field(default=0, init=False)
    refused: bool = field(default=False, init=False)
    settled: bool = field(default=False, init=False)

    def install_witness(self) -> None:
        sys.addaudithook(self.observe_acquisition)

    def observe_acquisition(self, event: str, args: tuple[object, ...]) -> None:
        if event == "subprocess.Popen":
            self.starts += 1
        elif event == "socket.__new__" and len(args) == 4 and args[1] == socket.AF_UNIX:
            self.unix_sockets += 1

    def prepare(
        self,
        root: Path,
        workspace: Path,
        binary: Path,
        port: int,
        bearer: str,
        key: str,
    ) -> BoundLaunch | None:
        workload: Mapping[str, str] = MappingProxyType({TEXT_NAME: TEXT_VALUE})
        secret_name = "BASH_ENV" if self.case == "reserved" else WORKLOAD_HANDLE_NAME
        authored_scope = (
            self.app_scope if self.case == "reserved" else self.attempt_scope
        )
        authored: Mapping[str, SecretValue] = MappingProxyType(
            {secret_name: authored_scope.admit(WORKLOAD_HANDLE_VALUE)}
        )
        attempt_token = self.attempt_scope.admit(ATTEMPT_CANARY)
        grant = self.app_scope.admit(key)
        if self.case != "ready":
            self.app_scope.release()
        try:
            inputs = prepare_native_inputs(
                root,
                workspace,
                binary=binary,
                port=port,
                bearer=bearer,
                workload_names=workload.keys(),
                workload_secret_names=authored.keys(),
            )
            adapter = dict(inputs.exec_server.environment)
            if self.case == "path":
                adapter["PATH"] = "bound-environment-path-canary"
            if self.case == "grant":
                adapter[PROVIDER_KEY] = "bound-environment-grant-canary"
            binding = bind_candidate_environments(
                cwd=workspace,
                attempt_token=attempt_token,
                workload=workload,
                secrets=authored,
                harness_credentials={PROVIDER_KEY: grant},
                roles={
                    "app": CandidateRoleEnvironment(
                        inputs.app_server.environment, (PROVIDER_KEY,)
                    ),
                    "exec": CandidateRoleEnvironment(adapter, ()),
                },
            )
            if self.case != "ready":
                raise BoundEnvironmentError("selected_bound_preflight_not_refused")
            app = binding.materialize("app")
            executor = binding.materialize("exec")
        except LaunchConfigurationError as error:
            if (
                self.case != "reserved"
                or error.reason is not LaunchConfigurationFailure.ENVIRONMENT_CONFLICT
            ):
                raise BoundEnvironmentError("selected_bound_recipe_failed") from None
            self.refused = True
            return None
        except KoraProtocolError as error:
            if (
                self.case not in {"path", "grant"}
                or error.code != "launch_rejected"
                or error.message != "the candidate environment has conflicting owners"
                or error.retryable
                or error.details
                or error.__cause__ is not None
                or error.__context__ is not None
            ):
                raise BoundEnvironmentError("selected_bound_ownership_failed") from None
            self.refused = True
            return None
        except SecretRevealRefused:
            raise BoundEnvironmentError(
                "selected_bound_secret_reveal_refused"
            ) from None
        write_native_configuration(inputs.app_server)
        write_native_configuration(inputs.exec_server)
        common = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(workspace),
            "KORA_ATTEMPT_TOKEN": ATTEMPT_CANARY,
            TEXT_NAME: TEXT_VALUE,
            WORKLOAD_HANDLE_NAME: WORKLOAD_HANDLE_VALUE,
        }
        return BoundLaunch(
            inputs,
            app,
            executor,
            {**common, **inputs.app_server.environment, PROVIDER_KEY: key},
            {**common, **inputs.exec_server.environment},
        )
