import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    CodexLaunchInputs,
    CodexLaunchProfile,
    NativeHomes,
    NativeProcessInputs,
    build_launch_inputs,
)

SelectedHookCase = Literal[
    "selected-hook-deny",
    "selected-hook-no-reason",
    "selected-hook-exit-one",
    "selected-hook-timeout",
]
SelectedLaunchCase = (
    SelectedHookCase
    | Literal[
        "selected-allow",
        "selected-deny",
        "selected-key-absent",
        "selected-startup-ready",
        "selected-startup-bearer",
        "selected-startup-bash",
    ]
)
SELECTED_HOOK_CASES: frozenset[SelectedHookCase] = frozenset(
    {
        "selected-hook-deny",
        "selected-hook-no-reason",
        "selected-hook-exit-one",
        "selected-hook-timeout",
    }
)
SELECTED_LAUNCH_CASES = frozenset(
    {
        *SELECTED_HOOK_CASES,
        "selected-allow",
        "selected-deny",
        "selected-key-absent",
        "selected-startup-ready",
        "selected-startup-bearer",
        "selected-startup-bash",
    }
)
SELECTED_AUTH_CASES = frozenset(
    {*SELECTED_HOOK_CASES, "selected-allow", "selected-deny", "selected-startup-ready"}
)
SELECTED_REFUSALS = frozenset(
    {"selected-key-absent", "selected-startup-bearer", "selected-startup-bash"}
)
SELECTED_MODEL = "gpt-5.5"
PROJECT_MODEL_CANARY = "kora-checkout-model-canary"
PROJECT_DOC_CANARY = b"kora-checkout-instruction-canary"
SELECTED_DENIAL = "kora managed hook probe denied the selected command"
HOOK_TIMEOUT_SOCKET = "selected-hook-timeout.sock"
SELECTED_COMMAND = (
    'test -z "${CODEX_API_KEY+x}" && '
    'test -n "${BASH_VERSION-}" && ! shopt -q login_shell && '
    """printf '%s\\n' "$(readlink -f -- "$0")" "$HOME" > kora-probe.marker"""
)


def role_homes(root: Path) -> NativeHomes:
    return NativeHomes(root / "home", root / "codex", root / "tmp")


def prepare_native_inputs(
    root: Path, workspace: Path, *, binary: Path, port: int, bearer: str
) -> CodexLaunchInputs:
    return build_launch_inputs(
        CodexLaunchProfile(
            SELECTED_MODEL, f"http://127.0.0.1:{port}/v1", "danger-full-access"
        ),
        executable=binary,
        bash=Path("/bin/bash"),
        workspace=workspace,
        app_homes=role_homes(root / "app"),
        exec_homes=role_homes(root / "exec"),
        executor_digest=hashlib.sha256(bearer.encode("ascii")).hexdigest(),
    )


def materialize_native_inputs(inputs: NativeProcessInputs) -> dict[str, str]:
    for name in ("HOME", "CODEX_HOME", "TMPDIR"):
        Path(inputs.environment[name]).mkdir(mode=0o700, parents=True)
    (Path(inputs.environment["CODEX_HOME"]) / "config.toml").write_text(
        inputs.config, encoding="utf-8"
    )
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        **inputs.environment,
    }


def plant_checkout_canaries(workspace: Path) -> None:
    dot_git = workspace / ".git"
    (dot_git / "objects").mkdir(parents=True)
    (dot_git / "refs" / "heads").mkdir(parents=True)
    (dot_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    project = workspace / ".codex"
    project.mkdir()
    (project / "config.toml").write_text(
        f'model = "{PROJECT_MODEL_CANARY}"\n', encoding="utf-8"
    )
    (workspace / "AGENTS.md").write_bytes(PROJECT_DOC_CANARY + b"\n")


def workspace_remains_untrusted(config: Path, workspace: Path) -> bool:
    settings = tomllib.loads(config.read_text(encoding="utf-8"))
    return (
        settings.get("projects") == {str(workspace): {"trust_level": "untrusted"}}
        and settings.get("project_doc_max_bytes") == 0
    )
