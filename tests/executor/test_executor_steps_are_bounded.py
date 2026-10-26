import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.executor.captures import Captures
from kora.executor.failures import ExecutorError, Phase
from kora.executor.guard import PhaseGuard
from kora.executor.steps import StepContext, run_step
from kora.ir.node import ErrorClass
from kora.protocols.deadlines import LocalThresholds
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import SystemClock
from kora.protocols.vcs import CheckoutRequest, PlanStep
from tests.executor.step_support import PYTHON, context, step
from tests.plugins.vcs.git_support import REPO, make_world

ECHO: str = (
    "import json, os, sys\n"
    "print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ),"
    " 'stdin': sys.stdin.read(), 'executable': sys.executable}))"
)


def marker_step(marker: Path, **changes: object) -> PlanStep:
    plan_step = step(f"open({str(marker)!r}, 'w').write('ran')")
    return plan_step.model_copy(update=changes)


def refusal(subject: StepContext, plan_step: PlanStep) -> ExecutorError:
    with pytest.raises(ExecutorError) as raised:
        asyncio.run(run_step(plan_step, 0, subject, Captures()))
    return raised.value


def test_executor_steps_are_bounded(tmp_path: Path, kora_capture: Capture) -> None:
    subject = context(kora_capture, tmp_path)
    echo = step(
        ECHO,
        env={"ROOT": "{publication_root}", "LITERAL": "{not_a_placeholder}"},
        stdin=b"in {control_root}",
        capture="diff",
        arguments=("{publication_root}/x", "HEAD^{commit-ish}"),
    )

    captures, outcome = asyncio.run(run_step(echo, 0, subject, Captures()))

    assert captures.diff is not None
    document = json.loads(captures.diff)
    publication = str(subject.areas.publication)
    assert document["argv"] == [f"{publication}/x", "HEAD^{commit-ish}"]
    assert document["env"]["ROOT"] == publication
    assert document["env"]["LITERAL"] == "{not_a_placeholder}"
    assert {"PATH", "HOME", "USER"}.isdisjoint(document["env"])
    assert document["stdin"] == f"in {subject.areas.control}"
    assert document["executable"] == PYTHON
    assert outcome.succeeded


def test_refused_steps_execute_nothing(tmp_path: Path, kora_capture: Capture) -> None:
    marker = tmp_path / "marker"
    prepare = context(kora_capture, tmp_path / "prepare")
    export = context(kora_capture, tmp_path / "export", Phase.EXPORT)
    due = context(
        kora_capture,
        tmp_path / "due",
        guard=PhaseGuard(SystemClock(), LocalThresholds(0, 0)),
    )
    cases = [
        (prepare, marker_step(marker, argv=("python", "-c", "x", "{commit}"))),
        (export, marker_step(marker, cwd="candidate", env={"A": "{control_root}"})),
        (prepare, marker_step(marker, cwd="candidate")),
        (prepare, marker_step(marker, argv=("sh", "-c", "touch x"))),
        (due, marker_step(marker)),
    ]

    failures = [refusal(subject, plan_step) for subject, plan_step in cases]

    assert [failure.code for failure in failures] == [
        "placeholder_unavailable",
        "placeholder_refused",
        "step_area_refused",
        "tool_not_required",
        "deadline_due",
    ]
    assert failures[-1].failure_class is ErrorClass.TIMEOUT
    assert not marker.exists()


def test_timeouts_are_clipped_by_the_deadline(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = SystemClock()
    now = clock.monotonic_ns()
    clipped = LocalThresholds(now + 300_000_000, now + 300_000_000)
    subject = context(kora_capture, tmp_path, guard=PhaseGuard(clock, clipped))
    sleeper = step("import time; time.sleep(30)", timeout=timedelta(hours=1))

    started = time.monotonic()
    failure = refusal(subject, sleeper)
    elapsed = time.monotonic() - started

    assert failure.failure_class is ErrorClass.TIMEOUT
    assert elapsed < 10


def test_captures_are_bounded(tmp_path: Path, kora_capture: Capture) -> None:
    subject = context(kora_capture, tmp_path)
    flood = step("print('a' * 4096)", capture="commit")

    failure = refusal(subject, flood)

    assert failure.code == "capture_oversized"


@pytest.mark.parametrize(
    ("revision", "branch"),
    [
        ("--upload-pack=touch /tmp/kora-pwned", "work"),
        ("-" + "a" * 39, "work"),
        ("main", "work"),
        ("a" * 40, "-work"),
        ("a" * 40, "bad..branch"),
        ("refs/heads/../x", "work"),
    ],
)
def test_option_shaped_operands_are_refused(
    tmp_path: Path, kora_capture: Capture, revision: str, branch: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    request = CheckoutRequest(
        repo=REPO,
        rev=revision,
        root=str(tmp_path / "candidate"),
        attempt_id="attempt",
        branch=branch,
    )

    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(world.provider.checkout_plan(request, context_for(kora_capture)))

    assert raised.value.code == "vcs_request_invalid"
