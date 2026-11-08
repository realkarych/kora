import hashlib
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    CodexLaunchInputs,
    CodexLaunchProfile,
    NativeHomes,
    NativeProcessInputs,
    build_launch_inputs,
)
from kora.plugins.harness.codex.selected_startup import SelectedManagedHook

SelectedPatchCase = Literal[
    "selected-patch-allow",
    "selected-patch-deny",
    "selected-patch-exit-one",
    "selected-patch-timeout",
]
SelectedShellPatchCase = Literal[
    "selected-shell-patch-allow",
    "selected-shell-patch-protected",
    "selected-shell-patch-exit-one",
    "selected-shell-patch-timeout",
]
SelectedHookCase = (
    Literal[
        "selected-bound-bash",
        "selected-command-exit-seven",
        "selected-hook-deny",
        "selected-hook-no-reason",
        "selected-hook-exit-one",
        "selected-hook-timeout",
    ]
    | SelectedPatchCase
    | SelectedShellPatchCase
)
SelectedSqliteHomeCase = Literal[
    "selected-sqlite-home-default", "selected-sqlite-home-override"
]
SelectedBashStartupCase = Literal[
    "selected-bash-startup-default", "selected-bash-startup-override"
]
SelectedBashTraceCase = Literal[
    "selected-bash-trace-default",
    "selected-bash-trace-shellopts",
    "selected-bash-trace-command",
    "selected-bash-trace-ps4",
]
SelectedTlsCase = Literal[
    "selected-tls-default",
    "selected-tls-codex-ca",
    "selected-tls-cert-file",
    "selected-tls-cert-dir",
]
SelectedNoiseRegistryCase = Literal[
    "selected-noise-registry-default", "selected-noise-registry-override"
]
SelectedToolInventoryCase = Literal[
    "selected-tool-inventory-allow",
    "selected-tool-inventory-disabled",
    "selected-tool-search",
]
SelectedHookInventoryRefusal = Literal[
    "selected-startup-hook-missing", "selected-startup-hook-matcher"
]
SelectedLaunchCase = (
    SelectedToolInventoryCase
    | SelectedNoiseRegistryCase
    | SelectedHookCase
    | SelectedSqliteHomeCase
    | SelectedBashStartupCase
    | SelectedBashTraceCase
    | SelectedTlsCase
    | SelectedHookInventoryRefusal
    | Literal[
        "selected-command-metadata",
        "selected-allow",
        "selected-deny",
        "selected-key-absent",
        "selected-startup-ready",
        "selected-startup-bearer",
        "selected-startup-bash",
        "selected-turn-complete",
        "selected-turn-cut",
    ]
)
SELECTED_PATCH_CASES: frozenset[SelectedPatchCase] = frozenset(
    {
        "selected-patch-allow",
        "selected-patch-deny",
        "selected-patch-exit-one",
        "selected-patch-timeout",
    }
)
SELECTED_SHELL_PATCH_CASES: frozenset[SelectedShellPatchCase] = frozenset(
    {
        "selected-shell-patch-allow",
        "selected-shell-patch-protected",
        "selected-shell-patch-exit-one",
        "selected-shell-patch-timeout",
    }
)
SHELL_PATCH_POLICY_OUTCOMES = {
    "selected-shell-patch-allow": b"allowed\n",
    "selected-shell-patch-protected": b"argument_invalid\n",
}
SELECTED_HOOK_CASES: frozenset[SelectedHookCase] = frozenset(
    {
        *SELECTED_PATCH_CASES,
        *SELECTED_SHELL_PATCH_CASES,
        "selected-bound-bash",
        "selected-command-exit-seven",
        "selected-hook-deny",
        "selected-hook-no-reason",
        "selected-hook-exit-one",
        "selected-hook-timeout",
    }
)
SELECTED_HOOK_TIMEOUT_CASES: frozenset[SelectedHookCase] = frozenset(
    {"selected-hook-timeout", "selected-patch-timeout", "selected-shell-patch-timeout"}
)
SELECTED_SQLITE_HOME_CASES: frozenset[SelectedSqliteHomeCase] = frozenset(
    {"selected-sqlite-home-default", "selected-sqlite-home-override"}
)
SQLITE_HOME_CANARY = "sqlite-home-canary"
SELECTED_BASH_STARTUP_CASES: frozenset[SelectedBashStartupCase] = frozenset(
    {"selected-bash-startup-default", "selected-bash-startup-override"}
)
SELECTED_EXPLICIT_TRACE_CASES: frozenset[SelectedBashTraceCase] = frozenset(
    {"selected-bash-trace-command", "selected-bash-trace-ps4"}
)
SELECTED_BASH_TRACE_CASES: frozenset[SelectedBashTraceCase] = frozenset(
    {
        *SELECTED_EXPLICIT_TRACE_CASES,
        "selected-bash-trace-default",
        "selected-bash-trace-shellopts",
    }
)
SELECTED_TURN_CASES = frozenset({"selected-turn-complete", "selected-turn-cut"})
SELECTED_NOISE_REGISTRY_CASES: frozenset[SelectedNoiseRegistryCase] = frozenset(
    {"selected-noise-registry-default", "selected-noise-registry-override"}
)
SELECTED_TOOL_INVENTORY_CASES = frozenset(
    {
        "selected-tool-inventory-allow",
        "selected-tool-inventory-disabled",
        "selected-tool-search",
    }
)
SELECTED_TLS_OVERRIDES = {
    "selected-tls-codex-ca": "CODEX_CA_CERTIFICATE",
    "selected-tls-cert-file": "SSL_CERT_FILE",
    "selected-tls-cert-dir": "SSL_CERT_DIR",
}
SELECTED_TLS_CASES = frozenset({"selected-tls-default", *SELECTED_TLS_OVERRIDES})
SELECTED_HOOK_INVENTORY_REFUSALS: frozenset[SelectedHookInventoryRefusal] = frozenset(
    {"selected-startup-hook-missing", "selected-startup-hook-matcher"}
)
SELECTED_LAUNCH_CASES = frozenset(
    {
        *SELECTED_NOISE_REGISTRY_CASES,
        *SELECTED_TOOL_INVENTORY_CASES,
        *SELECTED_HOOK_CASES,
        *SELECTED_TURN_CASES,
        *SELECTED_SQLITE_HOME_CASES,
        *SELECTED_BASH_STARTUP_CASES,
        *SELECTED_BASH_TRACE_CASES,
        *SELECTED_TLS_CASES,
        *SELECTED_HOOK_INVENTORY_REFUSALS,
        "selected-command-metadata",
        "selected-allow",
        "selected-deny",
        "selected-key-absent",
        "selected-startup-ready",
        "selected-startup-bearer",
        "selected-startup-bash",
    }
)
SELECTED_AUTH_CASES = frozenset(
    {
        *SELECTED_TOOL_INVENTORY_CASES,
        *SELECTED_HOOK_CASES,
        *SELECTED_TURN_CASES,
        *SELECTED_SQLITE_HOME_CASES,
        *SELECTED_BASH_STARTUP_CASES,
        *SELECTED_BASH_TRACE_CASES,
        *SELECTED_TLS_CASES,
        "selected-command-metadata",
        "selected-allow",
        "selected-deny",
        "selected-startup-ready",
    }
)
SELECTED_REFUSALS = frozenset(
    {
        *SELECTED_NOISE_REGISTRY_CASES,
        *SELECTED_HOOK_INVENTORY_REFUSALS,
        "selected-tls-default",
        "selected-key-absent",
        "selected-startup-bearer",
        "selected-startup-bash",
    }
)
SELECTED_MODEL = "gpt-5.5"
PROJECT_MODEL_CANARY = "kora-checkout-model-canary"
PROJECT_DOC_CANARY = b"kora-checkout-instruction-canary"
SELECTED_DENIAL = "kora managed hook probe denied the selected command"
SHELL_PATCH_DENIAL = "kora managed hook probe denied the protected shell patch"
SHELL_PATCH_POLICY_RECEIPT = "shell-patch-policy-receipt"
SHELL_PATCH_WORKSPACE = "shell-patch-workspace"
SHELL_PATCH_COMMAND = (
    "apply_patch <<'KORA_PATCH'\n"
    "*** Begin Patch\n"
    "*** Add File: kora-probe.marker\n"
    "+direct patch marker\n"
    "*** End Patch\n"
    "KORA_PATCH"
)
HOOK_TIMEOUT_SOCKET = "selected-hook-timeout.sock"
BASH_WITNESS_SOCKET = "selected-bash.sock"
BASH_WITNESS_MODULE = "tests.plugins.harness.codex.selected_bash"
BASH_WITNESS_ARGV = ("/usr/local/bin/python3", "-I", "-m", BASH_WITNESS_MODULE)
SELECTED_COMMAND = (
    'test -z "${CODEX_API_KEY+x}" && '
    'test -n "${BASH_VERSION-}" && ! shopt -q login_shell && '
    """printf '%s\\n' "$(readlink -f -- "$0")" "$HOME" > kora-probe.marker"""
)

METADATA_MARKER = "kora-probe.metadata"
METADATA_ENV_CONTROL = "kora-executor-env-control"
METADATA_ENVIRONMENT = {
    "CODEX_THREAD_ID": "kora-stale-thread",
    "CODEX_TOOL_CALL_ID": "kora-stale-call",
    "KORA_PROBE_ENV_CONTROL": METADATA_ENV_CONTROL,
}
METADATA_COMMAND = (
    SELECTED_COMMAND + " && printf '%s\\n' "
    '"${CODEX_THREAD_ID-}" "${CODEX_TOOL_CALL_ID-}" "${KORA_PROBE_ENV_CONTROL-}" '
    f"> {METADATA_MARKER}"
)


def selected_command(case: str) -> str:
    if case in SELECTED_SHELL_PATCH_CASES:
        return SHELL_PATCH_COMMAND
    if case in SELECTED_EXPLICIT_TRACE_CASES:
        return "set -x; " + SELECTED_COMMAND
    if case == "selected-bound-bash":
        return " ".join(BASH_WITNESS_ARGV) + " && " + SELECTED_COMMAND
    if case == "selected-command-exit-seven":
        return SELECTED_COMMAND + " && exit 7"
    return METADATA_COMMAND if case == "selected-command-metadata" else SELECTED_COMMAND


def selected_managed_hook(case: str) -> SelectedManagedHook:
    short = case in (
        SELECTED_PATCH_CASES | SELECTED_SHELL_PATCH_CASES | SELECTED_HOOK_TIMEOUT_CASES
    )
    fingerprint = (
        "sha256:5f52a9101194f0b251dfc933d2b60b224d35886081a5bcd35d23d27a2c979b9c"
        if short
        else "sha256:ccdc59d5bf14a68a82a519397df8fd7f46c6b7daefe5a12198365b6d4aa434f6"
    )
    return SelectedManagedHook(
        Path("/opt/kora-probe"),
        "/usr/local/bin/python3 -I /opt/kora-probe/managed_hook.py",
        5 if short else 60,
        fingerprint,
    )


def role_homes(root: Path) -> NativeHomes:
    return NativeHomes(root / "home", root / "codex", root / "tmp")


def prepare_native_inputs(
    root: Path,
    workspace: Path,
    *,
    binary: Path,
    port: int,
    bash: Path = Path("/bin/bash"),
    bearer: str,
    https: bool = False,
    workload_names: Collection[str] = (),
    workload_secret_names: Collection[str] = (),
) -> CodexLaunchInputs:
    scheme = "https" if https else "http"
    return build_launch_inputs(
        CodexLaunchProfile(
            SELECTED_MODEL, f"{scheme}://127.0.0.1:{port}/v1", "danger-full-access"
        ),
        executable=binary,
        bash=bash,
        workspace=workspace,
        app_homes=role_homes(root / "app"),
        exec_homes=role_homes(root / "exec"),
        executor_digest=hashlib.sha256(bearer.encode("ascii")).hexdigest(),
        workload_names=workload_names,
        workload_secret_names=workload_secret_names,
    )


def write_native_configuration(inputs: NativeProcessInputs) -> None:
    for name in ("HOME", "CODEX_HOME", "TMPDIR"):
        Path(inputs.environment[name]).mkdir(mode=0o700, parents=True)
    (Path(inputs.environment["CODEX_HOME"]) / "config.toml").write_text(
        inputs.config, encoding="utf-8"
    )


def materialize_native_inputs(inputs: NativeProcessInputs) -> dict[str, str]:
    write_native_configuration(inputs)
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
