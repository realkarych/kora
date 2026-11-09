import subprocess
import sys
import tomllib
from collections.abc import Collection
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

import pytest

from kora.plugins.harness.codex.launch_configuration import (
    CodexLaunchInputs,
    CodexLaunchProfile,
    ExecutorTransport,
    LaunchConfigurationError,
    LaunchConfigurationFailure,
    NativeHomes,
    build_launch_inputs,
)

PROBE = Path(__file__).parents[3] / "support" / "codex_launch_probe.py"
EXECUTABLE = Path("/installed/codex")
BASH = Path("/usr/bin/bash")
DIGEST = "ab" * 32


def homes(root: Path) -> NativeHomes:
    return NativeHomes(root / "home", root / "codex", root / "tmp")


def construct(
    root: Path,
    *,
    app: NativeHomes | None = None,
    executable: Path = EXECUTABLE,
    digest: str | None = DIGEST,
    transport: ExecutorTransport = ExecutorTransport.DIRECT_WEBSOCKET,
    workload: Collection[str] = (),
    workload_secrets: Collection[str] = (),
) -> CodexLaunchInputs:
    return build_launch_inputs(
        CodexLaunchProfile(
            "fixture/model:2026", "https://responses.example/v1", "read-only"
        ),
        executable=executable,
        bash=BASH,
        workspace=root / "workspace",
        app_homes=homes(root / "app") if app is None else app,
        exec_homes=homes(root / "exec"),
        executor_digest=digest,
        executor_transport=transport,
        workload_names=workload,
        workload_secret_names=workload_secrets,
    )


@pytest.mark.parametrize(
    ("transport", "digest", "endpoint_arguments"),
    [
        (
            ExecutorTransport.DIRECT_WEBSOCKET,
            DIGEST,
            (
                "ws://127.0.0.1:0",
                "--ws-auth",
                "capability-token",
                "--ws-token-sha256",
                DIGEST,
            ),
        ),
        (ExecutorTransport.PRIVATE_STDIO, None, ("stdio://",)),
    ],
    ids=("direct", "stdio"),
)
def test_codex_recipes_bind_distinct_roles_and_exact_native_controls(
    tmp_path: Path,
    transport: ExecutorTransport,
    digest: str | None,
    endpoint_arguments: tuple[str, ...],
) -> None:
    root = tmp_path / "absent"
    app = homes(root / "app")
    executor = homes(root / "exec")

    inputs = construct(
        root,
        transport=transport,
        digest=digest,
        workload=(
            "WORKLOAD_VAR",
            "CODEX_THREAD_ID_ALIAS",
            "CODEX_SQLITE_HOME_ALIAS",
            "BASH_ENV_ALIAS",
            "SHELLOPTS_ALIAS",
            "SSL_CERT_FILE_ALIAS",
            "HTTP_PROXY_ALIAS",
            "Http_Proxy",
        ),
        workload_secrets=(
            "WORK_SECRET",
            "CODEX_TOOL_CALL_ID_ALIAS",
            "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL_ALIAS",
            "PS4_ALIAS",
            "CODEX_CA_CERTIFICATE_ALIAS",
            "NO_PROXY_ALIAS",
            "No_Proxy",
        ),
    )
    app_config: object = tomllib.loads(inputs.app_server.config)
    exec_config: object = tomllib.loads(inputs.exec_server.config)

    expected_controls = {
        "approval_policy": "never",
        "allow_login_shell": False,
        "web_search": "disabled",
        "cli_auth_credentials_store": "file",
        "analytics": {"enabled": False},
        "features": {"hooks": True, "unified_exec": False},
        "shell_environment_policy": {
            "inherit": "all",
            "exclude": ["CODEX_API_KEY", "CODEX_EXEC_SERVER_URL"],
        },
    }
    assert exec_config == expected_controls
    assert app_config == {
        **expected_controls,
        "model": "fixture/model:2026",
        "model_provider": "kora_responses",
        "sandbox_mode": "read-only",
        "project_doc_max_bytes": 0,
        "projects": {str(root / "workspace"): {"trust_level": "untrusted"}},
        "model_providers": {
            "kora_responses": {
                "name": "kora Responses",
                "base_url": "https://responses.example/v1",
                "wire_api": "responses",
                "env_key": "CODEX_API_KEY",
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            },
        },
    }
    assert inputs.app_server.argv == (
        str(EXECUTABLE),
        "app-server",
        "--listen",
        "stdio://",
        "--strict-config",
    )
    assert inputs.exec_server.argv == (
        str(EXECUTABLE),
        "exec-server",
        "--strict-config",
        "--listen",
        *endpoint_arguments,
    )
    assert inputs.app_server.cwd == inputs.exec_server.cwd == root / "workspace"
    assert inputs.app_server.environment == {
        "HOME": str(app.home),
        "CODEX_HOME": str(app.codex_home),
        "TMPDIR": str(app.temporary),
        "SHELL": str(BASH),
        "CODEX_EXEC_SERVER_URL": "none",
    }
    assert inputs.exec_server.environment == {
        "HOME": str(executor.home),
        "CODEX_HOME": str(executor.codex_home),
        "TMPDIR": str(executor.temporary),
        "SHELL": str(BASH),
    }
    assert isinstance(inputs.app_server.environment, MappingProxyType)
    assert isinstance(inputs.exec_server.environment, MappingProxyType)
    assert not root.exists()


def test_codex_trust_config_quotes_one_exact_checkout(tmp_path: Path) -> None:
    root = tmp_path / 'quoted-"checkout-π-\\suffix'

    inputs = construct(root)
    config = tomllib.loads(inputs.app_server.config)
    trust: object = config.get("projects")

    assert trust == {str(root / "workspace"): {"trust_level": "untrusted"}}
    assert config.get("project_doc_max_bytes") == 0
    assert not root.exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://private-canary@example/v1",
        "https://example/v1?private-canary",
        "https://example/v1#private-canary",
        "https://example/private-canary\\@other/v1",
        "https://example/private-canary\r\n",
        'https://example/private-canary"\n[features]',
        "https://example:65536/private-canary",
        "https://example:0/private-canary",
        "https://127.1/private-canary",
        "https://example/../private-canary",
        "https://example/%2e%2e/private-canary",
    ],
    ids=(
        "userinfo",
        "query",
        "fragment",
        "backslash",
        "control",
        "toml",
        "out-of-range-port",
        "zero-port",
        "ambiguous-host",
        "parent",
        "encoded-parent",
    ),
)
def test_codex_provider_refuses_ambiguous_urls_without_input(url: str) -> None:
    with pytest.raises(LaunchConfigurationError) as caught:
        CodexLaunchProfile("fixture", url, "read-only")

    assert caught.value.reason is LaunchConfigurationFailure.INVALID_PROVIDER
    assert str(caught.value) == "codex_launch_invalid_provider"


def test_codex_provider_refuses_toml_model_injection() -> None:
    model = 'private-canary"\n[features]'

    with pytest.raises(LaunchConfigurationError) as caught:
        CodexLaunchProfile(model, "https://responses.example/v1", "read-only")

    assert caught.value.reason is LaunchConfigurationFailure.INVALID_PROVIDER
    assert str(caught.value) == "codex_launch_invalid_provider"


@pytest.mark.parametrize(
    "placement",
    ["relative", "control", "parent", "same-role", "cross-role", "within-workspace"],
)
def test_codex_launch_refuses_invalid_or_overlapping_placements(
    tmp_path: Path,
    placement: Literal[
        "relative", "control", "parent", "same-role", "cross-role", "within-workspace"
    ],
) -> None:
    root = tmp_path / "absent"
    app = homes(root / "app")
    paths = {
        "relative": Path("private-canary"),
        "control": root / "private-canary\0",
        "parent": root / ".." / "private-canary",
        "same-role": app.codex_home / "private-canary",
        "cross-role": root / "exec" / "home",
        "within-workspace": root / "workspace" / "private-canary",
    }

    with pytest.raises(LaunchConfigurationError) as caught:
        construct(
            root, app=NativeHomes(paths[placement], app.codex_home, app.temporary)
        )

    assert caught.value.reason is LaunchConfigurationFailure.INVALID_PLACEMENT
    assert str(caught.value) == "codex_launch_invalid_placement"
    assert not root.exists()


@pytest.mark.parametrize(
    ("transport", "digest", "reason"),
    [
        (
            ExecutorTransport.DIRECT_WEBSOCKET,
            "private-bearer-canary",
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST,
        ),
        (
            ExecutorTransport.DIRECT_WEBSOCKET,
            None,
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST,
        ),
        (
            ExecutorTransport.DIRECT_WEBSOCKET,
            "AB" * 32,
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST,
        ),
        (
            ExecutorTransport.PRIVATE_STDIO,
            DIGEST,
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST,
        ),
        (
            ExecutorTransport.PRIVATE_STDIO,
            "",
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST,
        ),
        (
            cast("ExecutorTransport", "private-stdio"),
            None,
            LaunchConfigurationFailure.INVALID_EXECUTOR_TRANSPORT,
        ),
    ],
    ids=(
        "raw-bearer",
        "missing-digest",
        "uppercase-digest",
        "stdio-digest",
        "stdio-empty",
        "raw-selector",
    ),
)
def test_codex_launch_refuses_mismatched_transport_inputs(
    tmp_path: Path,
    transport: ExecutorTransport,
    digest: str | None,
    reason: LaunchConfigurationFailure,
) -> None:
    root = tmp_path / "absent"

    with pytest.raises(LaunchConfigurationError) as caught:
        construct(root, digest=digest, transport=transport)

    assert caught.value.reason is reason
    assert str(caught.value) == reason.value
    assert not root.exists()


@pytest.mark.parametrize(
    "name",
    [
        "HOME",
        "CODEX_HOME",
        "TMPDIR",
        "SHELL",
        "CODEX_EXEC_SERVER_URL",
        "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL",
        "CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID",
        "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN",
        "CODEX_EXEC_SERVER_NOISE_CHATGPT_ACCOUNT_ID",
        "CODEX_API_KEY",
        "CODEX_SQLITE_HOME",
        "BASH_ENV",
        "SHELLOPTS",
        "PS4",
        "LD_PRELOAD",
        "LD_AUDIT",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "CODEX_THREAD_ID",
        "CODEX_TOOL_CALL_ID",
    ],
)
@pytest.mark.parametrize("owner", ["workload", "secret"])
@pytest.mark.parametrize(
    ("transport", "digest"),
    [
        (ExecutorTransport.DIRECT_WEBSOCKET, DIGEST),
        (ExecutorTransport.PRIVATE_STDIO, None),
    ],
    ids=("direct", "stdio"),
)
def test_codex_launch_refuses_another_owner_of_native_names(
    tmp_path: Path,
    name: str,
    owner: Literal["workload", "secret"],
    transport: ExecutorTransport,
    digest: str | None,
) -> None:
    workload = (name,) if owner == "workload" else ()
    workload_secrets = (name,) if owner == "secret" else ()

    with pytest.raises(LaunchConfigurationError) as caught:
        construct(
            tmp_path,
            workload=workload,
            workload_secrets=workload_secrets,
            transport=transport,
            digest=digest,
        )

    assert caught.value.reason is LaunchConfigurationFailure.ENVIRONMENT_CONFLICT
    assert str(caught.value) == "codex_launch_environment_conflict"


@pytest.mark.parametrize("owner", ["workload", "secret"])
@pytest.mark.parametrize(
    ("transport", "digest"),
    [
        (ExecutorTransport.DIRECT_WEBSOCKET, DIGEST),
        (ExecutorTransport.PRIVATE_STDIO, None),
    ],
    ids=("direct", "stdio"),
)
def test_codex_launch_refuses_scalar_owner_names(
    tmp_path: Path,
    owner: Literal["workload", "secret"],
    transport: ExecutorTransport,
    digest: str | None,
) -> None:
    workload = "CODEX_API_KEY" if owner == "workload" else ()
    workload_secrets = "CODEX_API_KEY" if owner == "secret" else ()

    with pytest.raises(LaunchConfigurationError) as caught:
        construct(
            tmp_path,
            workload=workload,
            workload_secrets=workload_secrets,
            transport=transport,
            digest=digest,
        )

    assert caught.value.reason is LaunchConfigurationFailure.INVALID_ENVIRONMENT_NAMES
    assert str(caught.value) == "codex_launch_invalid_environment_names"


def test_codex_launch_construction_observes_no_resource_operations(
    tmp_path: Path,
) -> None:
    command = [sys.executable, "-I", str(PROBE), str(tmp_path)]

    result = subprocess.run(command, check=False, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"pure Codex launch construction verified\n"
    assert result.stderr == b""
