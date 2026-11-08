import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast
from urllib.parse import unquote_to_bytes

from kora.plugins.harness.codex.app_server_rpc import AppServerRpc, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_startup import (
    StartupIdentity,
    initialize_app_server,
)
from kora.plugins.harness.codex.app_server_turn import reject_premature_completion
from kora.plugins.harness.codex.launch_configuration import (
    CODEX_VERSION,
    MODEL_NAME,
    MODEL_PROVIDER,
    valid_path,
)

LOOPBACK_ENDPOINT = re.compile(r"ws://127\.0\.0\.1:([1-9][0-9]{0,4})")
ENVIRONMENT_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
CAPABILITY_TOKEN = re.compile(r"[a-zA-Z0-9_-]{43,128}")
THREAD_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
FILE_URI_PATH = re.compile(r"/[!-~]*")
INVALID_FILE_URI_PATH = re.compile(r"""[?#"<>\`{}\\]|%(?![0-9A-Fa-f]{2})|%2[fF]""")


class SelectedStartupFailure(StrEnum):
    INVALID_EXPECTATION = "selected_startup_invalid_expectation"
    INVALID_BEARER = "selected_startup_invalid_bearer"
    LOCAL_STATUS = "selected_startup_local_status"
    ADD_ACKNOWLEDGEMENT = "selected_startup_add_acknowledgement"
    EXECUTOR_INFO_REFUSED = "selected_startup_executor_info_refused"
    EXECUTOR_SHELL = "selected_startup_executor_shell"
    EXECUTOR_CWD = "selected_startup_executor_cwd"
    EXECUTOR_STATUS = "selected_startup_executor_status"
    THREAD_ACKNOWLEDGEMENT = "selected_startup_thread_acknowledgement"


class SelectedStartupError(ValueError):
    def __init__(self, reason: SelectedStartupFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class SelectedEnvironment:
    environment_id: str
    endpoint: str = field(repr=False)
    bash: Path = field(repr=False)
    workspace: Path = field(repr=False)
    model: str = field(repr=False)

    def __post_init__(self) -> None:
        endpoint = LOOPBACK_ENDPOINT.fullmatch(self.endpoint)
        if (
            endpoint is None
            or int(endpoint[1]) > 65_535
            or ENVIRONMENT_ID.fullmatch(self.environment_id) is None
            or self.environment_id == "local"
            or not valid_path(self.bash)
            or not valid_path(self.workspace)
            or MODEL_NAME.fullmatch(self.model) is None
        ):
            raise SelectedStartupError(SelectedStartupFailure.INVALID_EXPECTATION)


def fields(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def matches_file_uri(uri: object, expected: Path) -> bool:
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return False
    encoded = uri.removeprefix("file://")
    return (
        FILE_URI_PATH.fullmatch(encoded) is not None
        and not encoded.startswith("//")
        and INVALID_FILE_URI_PATH.search(encoded) is None
        and unquote_to_bytes(encoded) == str(expected).encode("utf-8")
    )


def verify_executor_info(value: object, expected: SelectedEnvironment) -> None:
    info = fields(value)
    shell = fields(info.get("shell"))
    if shell.get("name") != "bash" or shell.get("path") != str(expected.bash):
        raise SelectedStartupError(SelectedStartupFailure.EXECUTOR_SHELL)
    if not matches_file_uri(info.get("cwd"), expected.workspace):
        raise SelectedStartupError(SelectedStartupFailure.EXECUTOR_CWD)


def verify_thread(value: object, expected: SelectedEnvironment) -> str:
    response = fields(value)
    thread = fields(response.get("thread"))
    thread_id = thread.get("id")
    workspace = str(expected.workspace)
    selection = {
        "environmentId": expected.environment_id,
        "cwd": workspace,
        "runtimeWorkspaceRoots": [workspace],
    }
    if (
        not isinstance(thread_id, str)
        or THREAD_ID.fullmatch(thread_id) is None
        or response.get("runtimeWorkspaceRoots") != [workspace]
        or response.get("approvalPolicy") != "never"
        or any(
            entry.get("cwd") != workspace
            or entry.get("modelProvider") != MODEL_PROVIDER
            or entry.get("model") != expected.model
            for entry in (response, thread)
        )
        or thread.get("environments") != [selection]
        or thread.get("ephemeral") is not True
        or thread.get("turns") != []
        or not {"forkedFromId", "parentThreadId"}.issubset(thread)
        or thread.get("forkedFromId") is not None
        or thread.get("parentThreadId") is not None
    ):
        raise SelectedStartupError(SelectedStartupFailure.THREAD_ACKNOWLEDGEMENT)
    return thread_id


async def prepare_selected_thread(
    rpc: AppServerRpc,
    identity: StartupIdentity,
    expected: SelectedEnvironment,
    bearer: str,
    *,
    deadline: float,
    before_write: Callable[[], None] | None = None,
) -> str:
    if identity.version != CODEX_VERSION:
        raise SelectedStartupError(SelectedStartupFailure.INVALID_EXPECTATION)
    if CAPABILITY_TOKEN.fullmatch(bearer) is None:
        raise SelectedStartupError(SelectedStartupFailure.INVALID_BEARER)

    async def call(method: str, params: dict[str, object]) -> object:
        return await rpc.call(
            method,
            params,
            deadline=deadline,
            observe=reject_premature_completion,
            before_write=before_write,
        )

    await initialize_app_server(
        rpc,
        identity,
        deadline=deadline,
        experimental_api=True,
        observe=reject_premature_completion,
        before_write=before_write,
    )
    local = fields(await call("environment/status", {"environmentId": "local"}))
    if local.get("status") != "unknown":
        raise SelectedStartupError(SelectedStartupFailure.LOCAL_STATUS)
    environment_id = expected.environment_id
    acknowledgement = await call(
        "environment/add",
        {
            "environmentId": environment_id,
            "execServerUrl": expected.endpoint,
            "authBearerToken": bearer,
        },
    )
    if acknowledgement != {}:
        raise SelectedStartupError(SelectedStartupFailure.ADD_ACKNOWLEDGEMENT)
    selected: dict[str, object] = {"environmentId": environment_id}
    try:
        info = await call("environment/info", selected)
    except RpcError as error:
        if error.reason is RpcFailure.REMOTE_ERROR:
            raise SelectedStartupError(
                SelectedStartupFailure.EXECUTOR_INFO_REFUSED
            ) from None
        raise
    verify_executor_info(info, expected)
    status = fields(await call("environment/status", selected))
    if status.get("status") != "ready" or status.get("error") is not None:
        raise SelectedStartupError(SelectedStartupFailure.EXECUTOR_STATUS)
    workspace = str(expected.workspace)
    response = await call(
        "thread/start",
        {
            "cwd": workspace,
            "runtimeWorkspaceRoots": [workspace],
            "environments": [{"environmentId": environment_id, "cwd": workspace}],
            "ephemeral": True,
        },
    )
    thread_id = verify_thread(response, expected)
    rpc.check_deadline(deadline)
    return thread_id
