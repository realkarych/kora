import asyncio
from pathlib import Path

from kora.conformance.capture import (
    CaptureOverflow,
    captured,
)
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir.node import ToolPolicy
from kora.observability import LoggingSettings
from kora.plugins.harness.codex.policy_input import policy_request
from kora.plugins.harness.codex.pre_tool_use import HookCall
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.errors import KoraProtocolError
from kora.protocols.policy import PolicyLayers
from kora.protocols.vcs import WorkspaceRef
from tests.plugins.harness.codex.selected_launch import (
    SHELL_PATCH_POLICY_OUTCOMES,
    SHELL_PATCH_POLICY_RECEIPT,
    SHELL_PATCH_WORKSPACE,
)

WORKSPACE_PATH_LIMIT = 512


def decide_shell_patch(call: HookCall, control: Path, case: str) -> bool | None:
    protected = case == "selected-shell-patch-protected"
    reason = "argument_invalid" if protected else "allowed"
    try:
        with (control / SHELL_PATCH_WORKSPACE).open("rb") as binding:
            raw = binding.read(WORKSPACE_PATH_LIMIT + 1)
        if len(raw) > WORKSPACE_PATH_LIMIT:
            return None
        root = raw.decode()
        if not Path(root).is_absolute():
            return None
        workspace = WorkspaceRef(
            attempt_id="shell-patch-probe",
            root=root,
            repo="fixture/repo",
            commit="c0ffee",
            base_commit="c0ffee",
            branch=None,
        )
        request = policy_request(call, workspace=workspace, node_path="graph.node")
        policy = ToolPolicy()
        protected_paths = ("kora-probe.marker",) if protected else ()
        with captured(
            CONFORMANCE_COMPONENT,
            LoggingSettings(),
            "shell-patch-probe",
            utc_now,
            deadline_seconds=1.0,
            max_bytes=4096,
        ) as capture:
            decision = asyncio.run(
                AllowDenyPolicy().decide(
                    request,
                    PolicyLayers(policy, policy),
                    protected_paths,
                    context_for(capture),
                )
            )
            records = capture.finish()
        denied = [
            (record.get("reason"), record.get("cause"))
            for record in records
            if record.get("event") == "policy.allow_deny.denied"
        ]
        if (decision.allow, decision.reason) != (not protected, reason) or denied != (
            [(reason, "rule")] if protected else []
        ):
            return None
        with (control / SHELL_PATCH_POLICY_RECEIPT).open("xb") as receipt:
            receipt.write(SHELL_PATCH_POLICY_OUTCOMES[case])
    except (
        CaptureOverflow,
        KoraProtocolError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return None
    return decision.allow
