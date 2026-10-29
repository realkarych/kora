from datetime import UTC, datetime, timedelta

import pytest

from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import (
    EvidenceScope,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.identity import ProcessIdentity
from kora.runner.identity import mount_hides_processes
from kora.runner.registry import (
    AttemptKey,
    Completed,
    Entry,
    Fence,
    Retired,
    Retiring,
    Running,
    Starting,
    Transition,
    Unsubmitted,
    attempt_name,
    bind_workspace,
    lookup_cell,
    retire_cell,
    status_cell,
    submit_cell,
)

MOMENT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
KEY = AttemptKey("domain-1", "attempt-1")
DEADLINE = ExecutionDeadline(
    at=MOMENT,
    scope_kind="attempt",
    scope_id="attempt-1",
    clock_uncertainty=timedelta(0),
    grace=timedelta(seconds=30),
)
RUNNER = ProcessIdentity(
    host_id="h",
    boot_id="b",
    pid_namespace="n",
    pid=10,
    start_time=1,
    leads_group=True,
)
FENCE = Fence(retirement_id="retirement-1", fenced_at=MOMENT)
QUIESCENCE = NormalCompletionQuiescence(
    kind="normal_completion",
    attempt_id="attempt-1",
    execution_domain_id="domain-1",
    record_ref="record-1",
    observed_at=MOMENT,
    scope=EvidenceScope.LEADER_GROUP_ENV_SWEEP,
)
RECEIPT = TerminationReceipt(
    kind="termination",
    attempt_id="attempt-1",
    execution_domain_id="domain-1",
    retirement_id="retirement-0",
    stopped_at=MOMENT,
    evidence_ref="registry:x",
    scope=EvidenceScope.NO_RUNNER_STARTED,
)
STATES: dict[str, Entry | None] = {
    "none": None,
    "unsubmitted": Unsubmitted(
        state="unsubmitted", deadline=DEADLINE, gate_closed=False
    ),
    "starting": Starting(
        state="starting",
        request_digest=DIGEST,
        nonce="e" * 32,
        started_at=MOMENT,
        fence=None,
        deadline=DEADLINE,
        gate_closed=False,
    ),
    "running": Running(
        state="running",
        request_digest=DIGEST,
        runner=RUNNER,
        socket_path="/run/kora/socket",
        deadline=DEADLINE,
        gate_closed=False,
    ),
    "completed": Completed(
        state="completed",
        request_digest=DIGEST,
        runner=RUNNER,
        quiescence=QUIESCENCE,
        fence=FENCE,
        released_at=None,
        deadline=DEADLINE,
        gate_closed=False,
    ),
    "released_completed": Completed(
        state="completed",
        request_digest=DIGEST,
        runner=RUNNER,
        quiescence=QUIESCENCE,
        fence=FENCE,
        released_at=MOMENT,
        deadline=DEADLINE,
        gate_closed=False,
    ),
    "retiring": Retiring(state="retiring", runner=RUNNER, fence=FENCE),
    "retired": Retired(
        state="retired",
        runner=None,
        receipt=RECEIPT,
        phase="failed",
        normal_completion=None,
        released_at=None,
    ),
}


def starting() -> Starting:
    entry = STATES["starting"]
    assert isinstance(entry, Starting)
    return entry


def outcome(transition: Transition) -> tuple[str | None, str | None]:
    written = None if transition.entry is None else transition.entry.state
    return written, transition.refusal


def looked_up(entry: Entry | None) -> str:
    try:
        handle = lookup_cell(entry, KEY)
    except KoraProtocolError as error:
        return error.code
    return "none" if handle is None else "handle"


def observed_status(entry: Entry | None) -> str:
    try:
        status = status_cell(entry, MOMENT)
    except KoraProtocolError as error:
        return error.code
    return status.phase


@pytest.mark.parametrize(
    ("state", "same", "different", "expired"),
    [
        ("none", (None, None), (None, None), ("unsubmitted", "deadline_expired")),
        (
            "unsubmitted",
            (None, None),
            (None, "workload_conflict"),
            ("unsubmitted", "deadline_expired"),
        ),
        (
            "starting",
            (None, None),
            (None, "workload_conflict"),
            ("starting", "deadline_expired"),
        ),
        (
            "running",
            (None, None),
            (None, "workload_conflict"),
            ("running", "deadline_expired"),
        ),
        (
            "completed",
            (None, None),
            (None, "workload_conflict"),
            ("completed", "deadline_expired"),
        ),
        (
            "released_completed",
            (None, "attempt_retired"),
            (None, "attempt_retired"),
            (None, "attempt_retired"),
        ),
        (
            "retiring",
            (None, "attempt_retired"),
            (None, "attempt_retired"),
            (None, "attempt_retired"),
        ),
        (
            "retired",
            (None, "attempt_retired"),
            (None, "attempt_retired"),
            (None, "attempt_retired"),
        ),
    ],
)
def test_workspace_binding_cells(
    state: str,
    same: tuple[str | None, str | None],
    different: tuple[str | None, str | None],
    expired: tuple[str | None, str | None],
) -> None:
    entry = STATES[state]

    equal = bind_workspace(entry, DEADLINE, False)
    changed = bind_workspace(entry, None, False)
    closed = bind_workspace(entry, DEADLINE, True)

    expected_same = ("unsubmitted", None) if state == "none" else same
    expected_different = ("unsubmitted", None) if state == "none" else different
    assert outcome(equal) == expected_same
    assert outcome(changed) == expected_different
    assert outcome(closed) == expired
    if closed.entry is not None:
        assert closed.entry.model_dump()["gate_closed"] is True


@pytest.mark.parametrize(
    "state", ["none", "unsubmitted", "starting", "running", "completed"]
)
def test_closed_gate_refuses_every_later_binding_and_submit(state: str) -> None:
    gated = bind_workspace(STATES[state], DEADLINE, True).entry

    later = bind_workspace(gated, DEADLINE, False)
    submitted = submit_cell(gated, DIGEST, DEADLINE, starting)

    assert outcome(later) == (None, "deadline_expired")
    assert outcome(submitted) == (None, "deadline_expired")


@pytest.mark.parametrize(
    ("state", "same", "other"),
    [
        ("none", ("starting", None), ("starting", None)),
        ("unsubmitted", ("starting", None), ("starting", None)),
        ("starting", (None, "runner_starting"), (None, "workload_conflict")),
        ("running", (None, None), (None, "workload_conflict")),
        ("completed", (None, None), (None, "workload_conflict")),
        ("released_completed", (None, "attempt_retired"), (None, "attempt_retired")),
        ("retiring", (None, "attempt_retired"), (None, "attempt_retired")),
        ("retired", (None, "attempt_retired"), (None, "attempt_retired")),
    ],
)
def test_submit_cells(
    state: str,
    same: tuple[str | None, str | None],
    other: tuple[str | None, str | None],
) -> None:
    entry = STATES[state]

    equal = submit_cell(entry, DIGEST, DEADLINE, starting)
    changed = submit_cell(entry, OTHER_DIGEST, DEADLINE, starting)
    rebound = submit_cell(entry, DIGEST, None, starting)

    assert outcome(equal) == same
    assert outcome(changed) == other
    if state not in ("none", "released_completed", "retiring", "retired"):
        assert outcome(rebound) == (None, "workload_conflict")


@pytest.mark.parametrize(
    ("state", "lookup", "status", "retire"),
    [
        ("none", "none", "attempt_not_submitted", ("retired", None)),
        ("unsubmitted", "none", "attempt_not_submitted", ("retired", None)),
        (
            "starting",
            "runner_starting",
            "runner_starting",
            (None, "retirement_unavailable"),
        ),
        ("running", "handle", "runner_unreachable", (None, "retirement_unavailable")),
        ("completed", "handle", "complete", (None, "retirement_unavailable")),
        ("released_completed", "handle", "complete", (None, "attempt_retired")),
        ("retiring", "handle", "runner_unreachable", (None, "retirement_unavailable")),
        ("retired", "handle", "failed", (None, None)),
    ],
)
def test_read_and_retire_cells(
    state: str, lookup: str, status: str, retire: tuple[str | None, str | None]
) -> None:
    entry = STATES[state]

    looked = looked_up(entry)
    observed = observed_status(entry)
    retired = retire_cell(entry, KEY, "retirement-1", MOMENT)

    assert looked == lookup
    assert observed == status
    assert outcome(retired) == retire
    if isinstance(retired.entry, Retired):
        assert retired.entry.receipt.scope is EvidenceScope.NO_RUNNER_STARTED
        assert retired.entry.phase == "failed"
        assert retired.entry.receipt.evidence_ref == f"registry:{KEY.name}"


@pytest.mark.parametrize(
    "attempt", ["../x", "a/b", "..", "\x00", "x" * 256, "attempt-1"]
)
def test_attempt_names_are_fixed_length_hex(attempt: str) -> None:
    name = attempt_name("domain-1", attempt)

    assert len(name) == 32
    assert int(name, 16) >= 0
    assert name != attempt_name("domain-2", attempt)


def test_attempt_name_separates_its_inputs() -> None:
    joined = attempt_name("a", "bc")

    split = attempt_name("ab", "c")

    assert joined != split


@pytest.mark.parametrize(
    ("options", "hidden"),
    [
        ("rw,nosuid,nodev,noexec,relatime", False),
        ("rw,relatime,hidepid=2", True),
        ("rw,relatime,hidepid=invisible", True),
        ("rw,relatime,subset=pid", True),
        ("rw,relatime,hidepid=0", False),
        ("rw,relatime,hidepid=off", False),
        ("rw,relatime,hidepid=ptraceable", True),
        ("rw,relatime,hidepid=4", True),
        ("rw,relatime,hidepid=noaccess", True),
    ],
)
def test_procfs_mount_options_decide_visibility(options: str, hidden: bool) -> None:
    table = (
        "22 1 0:5 / /proc rw,nosuid shared:12 - proc proc " + options + "\n"
        "23 1 8:1 / / rw shared:1 - ext4 /dev/sda1 rw\n"
    ).encode()

    observed = mount_hides_processes(table)

    assert observed is hidden


def test_procfs_mount_of_another_type_hides_processes() -> None:
    table = b"22 1 0:5 / /proc rw shared:12 - tmpfs tmpfs rw\n"

    observed = mount_hides_processes(table)

    assert observed
