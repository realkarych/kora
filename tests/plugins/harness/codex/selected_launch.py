import hashlib
from pathlib import Path
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    CodexLaunchInputs,
    CodexLaunchProfile,
    NativeHomes,
    NativeProcessInputs,
    build_launch_inputs,
)

SelectedLaunchCase = Literal["selected-allow", "selected-deny", "selected-key-absent"]
SELECTED_LAUNCH_CASES = frozenset(
    {"selected-allow", "selected-deny", "selected-key-absent"}
)
SELECTED_DENIAL = "kora managed hook probe denied the selected command"
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
            "gpt-5.5", f"http://127.0.0.1:{port}/v1", "danger-full-access"
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
