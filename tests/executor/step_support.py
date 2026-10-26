import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.executor.failures import Phase
from kora.executor.guard import PhaseGuard
from kora.executor.launcher import ProcessLauncher, StepLauncher
from kora.executor.steps import AreaName, Areas, StepContext
from kora.protocols.time import SystemClock
from kora.protocols.vcs import Capture as CaptureKind
from kora.protocols.vcs import PlanStep

PYTHON: str = sys.executable


def areas(root: Path) -> Areas:
    for name in ("candidate", "control", "publication"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return Areas(root / "candidate", root / "control", root / "publication")


def step(
    code: str,
    cwd: AreaName = "control",
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: timedelta = timedelta(seconds=30),
    capture: CaptureKind | None = None,
    arguments: tuple[str, ...] = (),
) -> PlanStep:
    return PlanStep(
        argv=("python", "-I", "-c", code, *arguments),
        cwd=cwd,
        env=dict(env or {}),
        stdin=stdin,
        timeout=timeout,
        capture=capture,
    )


def context(
    capture: Capture,
    root: Path,
    phase: Phase = Phase.PREPARE,
    values: Mapping[str, str] | None = None,
    guard: PhaseGuard | None = None,
    launcher: StepLauncher | None = None,
) -> StepContext:
    located = areas(root)
    default_values = {
        "candidate_root": str(located.candidate),
        "control_root": str(located.control),
        "publication_root": str(located.publication),
    }
    return StepContext(
        phase=phase,
        areas=located,
        values=dict(values) if values is not None else default_values,
        tools={"python": PYTHON},
        launcher=launcher or ProcessLauncher(),
        guard=guard or PhaseGuard(SystemClock(), None),
        logger=capture.diagnostics.logger(CONFORMANCE_COMPONENT),
    )
