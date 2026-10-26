import asyncio
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.vcs import (
    Checkout,
    Kept,
    VcsRemote,
    authorization,
    checked_out,
    grant,
    pinned,
    publish_request,
)
from kora.executor.entry import PreparedResult, PublishPermit
from kora.executor.failures import ExecutorError, Phase
from kora.executor.launcher import StepLauncher
from kora.executor.protected import (
    CHECK_BUDGET,
    PATH_CAP,
    SLICE_UNITS,
    Finding,
    FuelBounds,
    check_paths,
    checked_off_loop,
    compiled_globs,
)
from kora.ir.globs import PathGlob
from kora.observability import LoggingSettings
from kora.plugins.vcs.git.plans import TREE_HELPER
from kora.plugins.vcs.git.remotes import RemoteRefused, remote_path
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.vcs import PublicationInput, PublishPlan, PublishRequest
from tests.measurements.harness import Report, Sample, report_lifecycle, settled_counts
from tests.plugins.vcs.git_support import REPO, ManualClock, World, git, make_world
from tests.plugins.vcs.launchers import RecordingLauncher, StallingLauncher

pytestmark = pytest.mark.measurement

CYCLES: int = 20
WARM_UP: int = 1
DATASET_FILES: int = 1_000
INDEPENDENT_RUNS: int = 3
STREAMING_MARGIN_BYTES: int = 64 * 1024 * 1024
LEAKED_VALUE: str = "kora-measurement-secret-31f0"
ADVERSARIAL_PATHS: int = 1_100
ADVERSARIAL_GLOBS: int = 48
CAPPED_GLOBS: int = 64
CHANGED_PATH_BYTES: int = 524_288
WORKLOAD: str = "tests.measurements.git_publication_workload"
Outcome = Callable[[World, int], Coroutine[object, object, str]]


@pytest.fixture(scope="module")
def report() -> Iterator[Report]:
    yield from report_lifecycle()


@pytest.fixture
def capture() -> Iterator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(),
        "git-publication-measurement",
        lambda: datetime.now(UTC),
        deadline_seconds=30.0,
    ) as active:
        yield active


@dataclass(frozen=True)
class Counters:
    threads: int
    descriptors: int
    processes: int
    zombies: int


KEPT: Kept = Kept()


@dataclass(frozen=True)
class Sealed:
    checkout: Checkout
    plan: PublishPlan
    request: PublishRequest
    sealed: PublicationInput


def children() -> tuple[int, int]:
    listed = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,stat="],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    own = str(os.getpid())
    group = str(os.getpgid(0))
    rows = [fields for line in listed.splitlines() if len(fields := line.split()) >= 4]
    mine = [row for row in rows if row[2] == group and row[0] != own]
    zombies = sum(1 for row in mine if row[3].startswith("Z"))
    return len(mine) - zombies - 1, zombies


def counters() -> Counters:
    threads, descriptors = settled_counts()
    processes, zombies = children()
    return Counters(threads, descriptors, processes, zombies)


def tree_bytes(root: Path) -> int:
    return sum(path.lstat().st_size for path in root.rglob("*") if not path.is_dir())


def seeded_dataset(world: World) -> None:
    for index in range(DATASET_FILES):
        directory = world.seed / f"d{index % 20:02d}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{index:04d}.txt").write_text(f"dataset {index}\n")
    git(world.home, "add", "-A", cwd=world.seed)
    git(world.home, "commit", "-q", "-m", "dataset", cwd=world.seed)
    git(
        world.home,
        "push",
        "-q",
        str(world.remote),
        "HEAD:refs/heads/main",
        cwd=world.seed,
    )
    world.base = git(world.home, "rev-parse", "HEAD", cwd=world.seed)


def remote_for(world: World, cycle: int) -> VcsRemote:
    fixture = world.remote_fixture()
    return VcsRemote(
        repo=fixture.repo,
        revision=world.base,
        missing_revision=fixture.missing_revision,
        unreachable_repo=fixture.unreachable_repo,
        publish_branch=f"kora/measure/run/agent/{cycle}",
        commit_message=fixture.commit_message,
    )


async def sealed_change(world: World, cycle: int, files: dict[str, str]) -> Sealed:
    checkout = await checked_out(
        world.provider,
        world.executor,
        world.bench().areas(),
        REPO,
        world.base,
        world.context(),
    )
    candidate = world.candidate(checkout)
    for name, content in files.items():
        (candidate / name).write_text(content)
    context = world.context()
    request = publish_request(remote_for(world, cycle), checkout)
    plan = await world.provider.publish_plan(request, context)
    workspace = checkout.workspace()
    exported = await world.executor.export(plan, workspace, checkout.areas, context)
    sealed = await world.executor.seal(exported, workspace, checkout.areas, context)
    return Sealed(checkout, plan, request, sealed)


async def inspected(
    world: World, change: Sealed, deadline: ExecutionDeadline | None = None
) -> PreparedResult:
    return await world.executor.prepare_and_inspect(
        change.plan,
        change.sealed,
        change.checkout.workspace(),
        change.request,
        pinned(world.binding()),
        change.checkout.areas,
        (world.capture.scope.admit(LEAKED_VALUE),),
        (),
        KEPT,
        world.context(deadline),
    )


async def refused(work: Awaitable[object]) -> str:
    try:
        await work
    except ExecutorError as error:
        return error.failure_class.value
    return "accepted"


async def success(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {f"new{cycle}.txt": "new\n"})
    result = await inspected(world, change)
    outcome = await world.executor.publish(
        result,
        change.plan,
        authorization(result),
        grant(),
        PublishPermit(result.publication.operation_id),
        world.context(),
    )
    return outcome.state


async def artifact_invalid(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {".gitattributes": "* filter=lfs\n"})
    return await refused(inspected(world, change))


async def secret_leak(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {"leak.txt": LEAKED_VALUE})
    return await refused(inspected(world, change))


async def timeout(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {f"late{cycle}.txt": "late\n"})
    due = ExecutionDeadline(
        at=world.clock.now() + timedelta(seconds=1),
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )
    return await refused(inspected(world, change, due))


async def cancellation(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {f"cancel{cycle}.txt": "cancel\n"})
    launcher = world.executor.launcher
    assert isinstance(launcher, RecordingLauncher)
    started = len(launcher.specs)
    task = asyncio.create_task(inspected(world, change))
    deadline = time.monotonic() + 30
    while len(launcher.specs) <= started + 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return "cancelled"
    return "completed"


async def authorization_mismatch(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {f"auth{cycle}.txt": "auth\n"})
    result = await inspected(world, change)
    stale = authorization(result).model_copy(update={"dispatcher_id": "earlier"})
    return await refused(
        world.executor.publish(
            result,
            change.plan,
            stale,
            grant(),
            PublishPermit(result.publication.operation_id),
            world.context(),
        )
    )


async def reconcile(world: World, cycle: int) -> str:
    change = await sealed_change(world, cycle, {f"head{cycle}.txt": "head\n"})
    result = await inspected(world, change)
    await world.executor.publish(
        result,
        change.plan,
        authorization(result),
        grant(),
        PublishPermit(result.publication.operation_id),
        world.context(),
    )
    state = await world.executor.reconcile(result, world.context())
    return state.value


async def fuel(world: World, cycle: int) -> str:
    globs = compiled_globs(("*a*b*c*d*",), Phase.INSPECT)
    small = FuelBounds(budget=10_000, per_path=10_000, slice_units=1_000)
    paths = ["/".join(["a" * 90] * 40)] * 4
    return check_paths(globs, paths, small, threading.Event()).finding.value


async def remote_refusal(world: World, cycle: int) -> str:
    try:
        remote_path(f"ssh://host/remote-{cycle}.git")
    except RemoteRefused as error:
        return error.rule
    return "accepted"


OUTCOMES: dict[str, tuple[Outcome, str]] = {
    "success": (success, "pushed"),
    "artifact_invalid": (artifact_invalid, "artifact_invalid"),
    "secret_leak": (secret_leak, "secret_leak"),
    "timeout": (timeout, "timeout"),
    "cancellation": (cancellation, "cancelled"),
    "authorization_mismatch": (authorization_mismatch, "config_error"),
    "reconcile": (reconcile, "equal"),
    "fuel": (fuel, "fuel"),
    "remote_refusal": (remote_refusal, "remote_form_refused"),
}


def released(world: World) -> tuple[bool, int, int]:
    clean = True
    retained = 0
    total = 0
    for case in world.root.glob("bench-*/case-*"):
        control = case / "control"
        publication = case / "publication"
        clean = clean and not any(control.iterdir())
        for entry in publication.iterdir():
            size = tree_bytes(entry)
            total += size
            kept = {revision.location for revision in KEPT.revisions}
            if entry.name.startswith("input-") or str(entry) in kept:
                retained += size
                leftovers = [
                    item.name
                    for item in entry.iterdir()
                    if item.name.startswith(("scratch-", "index-"))
                ]
                clean = clean and not leftovers
            else:
                clean = False
        for path in sorted(case.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        subprocess.run(["/bin/rm", "-rf", str(case)], check=True)
    KEPT.revisions.clear()
    return clean, retained, total


def run_outcome(world: World, name: str, report: Report) -> None:
    action, expected = OUTCOMES[name]
    for warm in range(WARM_UP):
        assert asyncio.run(action(world, 10_000 + warm)) == expected
        released(world)
    before = counters()
    growth: list[Counters] = []
    storage_clean = True
    for cycle in range(CYCLES):
        started = time.perf_counter()
        result = asyncio.run(action(world, cycle))
        seconds = time.perf_counter() - started
        clean, retained, total = released(world)
        after = counters()
        growth.append(after)
        storage_clean = storage_clean and clean and retained == total
        report.samples.append(
            Sample(
                f"lifecycle_{name}",
                cycle,
                1,
                seconds,
                {
                    "result": result,
                    "threads": after.threads,
                    "descriptors": after.descriptors,
                    "processes": after.processes,
                    "zombies": after.zombies,
                    "retained_bytes": retained,
                    "publication_bytes": total,
                },
            )
        )
        assert result == expected
    report.gates[f"{name}_zero_thread_delta"] = all(
        sample.threads == before.threads for sample in growth
    )
    report.gates[f"{name}_zero_descriptor_delta"] = all(
        sample.descriptors == before.descriptors for sample in growth
    )
    report.gates[f"{name}_zero_process_delta"] = all(
        sample.processes == before.processes for sample in growth
    )
    report.gates[f"{name}_zero_zombies"] = all(sample.zombies == 0 for sample in growth)
    report.gates[f"{name}_retained_equals_recovery_state"] = storage_clean


def test_git_publication_lifecycle(
    tmp_path: Path, capture: Capture, report: Report
) -> None:
    started = time.perf_counter()
    report.environment["git_version"] = git(tmp_path, "--version")
    report.environment["prlimit"] = sys.platform == "linux"
    for name in OUTCOMES:
        root = tmp_path / name
        root.mkdir()
        launcher: StepLauncher = (
            StallingLauncher(TREE_HELPER) if name == "timeout" else RecordingLauncher()
        )
        world = make_world(root, capture, launcher=launcher, clock=ManualClock())
        seeded_dataset(world)
        run_outcome(world, name, report)
    report.durations["lifecycle"] = time.perf_counter() - started

    failed = [gate for gate, passed in report.gates.items() if not passed]
    assert failed == []


def workload(dataset: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", WORKLOAD, dataset],
        capture_output=True,
        check=False,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr.decode()[-2000:]
    row: dict[str, object] = json.loads(completed.stdout)
    return row


def number(row: dict[str, object], name: str) -> float:
    value = row[name]
    assert isinstance(value, int | float)
    return float(value)


def test_git_publication_cost(report: Report) -> None:
    started = time.perf_counter()
    rows: dict[str, list[dict[str, object]]] = {}
    for dataset in ("small1m", "large256m", "files20000"):
        rows[dataset] = [workload(dataset) for _ in range(INDEPENDENT_RUNS)]
    secrets = workload("large256m_secrets")
    report.samples.append(
        Sample(
            "scan_large256m_secrets256",
            0,
            1,
            number(secrets, "scan_seconds"),
            {
                name: value
                for name, value in secrets.items()
                if isinstance(value, int | float | str)
            },
        )
    )
    report.gates["max_secret_scan_within_bound"] = (
        number(secrets, "scan_seconds") * 8 < 600
    )
    near_cap = workload("near_cap")
    report.samples.append(
        Sample(
            "near_cap_import",
            0,
            1,
            number(near_cap, "import_and_fsck_seconds"),
            {
                name: value
                for name, value in near_cap.items()
                if isinstance(value, int | float | str)
            },
        )
    )
    report.gates["near_cap_imports_under_limits"] = near_cap["imported"] is True
    report.gates["near_cap_inspection_refused_over_index_bound"] = (
        near_cap["inspection"] == "inspect:artifact_invalid"
    )
    for dataset, samples in rows.items():
        for repeat, row in enumerate(samples):
            report.samples.append(
                Sample(
                    f"cost_{dataset}",
                    repeat,
                    1,
                    number(row, "prepare_and_inspect_seconds"),
                    {
                        name: value
                        for name, value in row.items()
                        if isinstance(value, int | float | str)
                    },
                )
            )
        peaks = [number(row, "peak_rss_bytes") for row in samples]
        report.environment[f"{dataset}_peak_rss_spread_bytes"] = max(peaks) - min(peaks)
    small = statistics.median(number(row, "peak_rss_bytes") for row in rows["small1m"])
    large = statistics.median(
        number(row, "peak_rss_bytes") for row in rows["large256m"]
    )
    report.durations["cost"] = time.perf_counter() - started
    report.gates["large_scan_streams"] = large - small <= STREAMING_MARGIN_BYTES

    failed = [
        gate
        for gate in (
            "large_scan_streams",
            "max_secret_scan_within_bound",
            "near_cap_imports_under_limits",
            "near_cap_inspection_refused_over_index_bound",
        )
        if not report.gates[gate]
    ]
    assert failed == []


def adversarial_globs(count: int) -> tuple[PathGlob, ...]:
    glob = "**/" + "/".join(["*x*x*"] * 13 + ["*y"])
    return compiled_globs(
        tuple(f"{glob}/z{index}" for index in range(count)), Phase.INSPECT
    )


def adversarial_paths(count: int) -> list[str]:
    stem = "/".join(["x" * 9] * 39)
    return [f"{stem}/xxxxx{index:04d}" for index in range(count)]


def realistic_globs(count: int) -> tuple[PathGlob, ...]:
    base = [
        "tests",
        ".github/**",
        "**/*.lock",
        "scripts",
        "Makefile",
        "pyproject.toml",
        "src/**/conftest.py",
        "docs/decisions",
        "**/*.pem",
        "tooling/ci/*.yml",
    ]
    extra = [f"src/pkg{index}/generated/**" for index in range(count - len(base))]
    return compiled_globs(tuple(base + extra)[:count], Phase.INSPECT)


def realistic_paths() -> list[str]:
    return [
        f"src/pkg{index % 100}/module{index}/file{index}.py" for index in range(10_000)
    ]


def test_protected_path_fuel(report: Report) -> None:
    full = FuelBounds()
    unlimited = FuelBounds(budget=10**15, per_path=10**15, slice_units=10**15)
    for count in (10, 64):
        started = time.perf_counter()
        result = check_paths(
            realistic_globs(count), realistic_paths(), full, threading.Event()
        )
        report.samples.append(
            Sample(
                f"fuel_realistic_{count}",
                0,
                10_000,
                time.perf_counter() - started,
                {
                    "finding": result.finding.value,
                    "budget_fraction": result.spent / CHECK_BUDGET,
                },
            )
        )
        report.gates[f"fuel_realistic_{count}_completes"] = (
            result.finding is Finding.CLEAR
        )
    capped = check_paths(
        adversarial_globs(CAPPED_GLOBS), adversarial_paths(1), full, threading.Event()
    )
    report.gates["fuel_path_cap_fails_closed"] = capped.finding is Finding.FUEL
    globs = adversarial_globs(ADVERSARIAL_GLOBS)
    paths = adversarial_paths(ADVERSARIAL_PATHS)
    single = check_paths(globs, paths[:1], unlimited, threading.Event()).spent
    started = time.perf_counter()
    exhausted = check_paths(globs, paths, full, threading.Event())
    exhaustion_seconds = time.perf_counter() - started
    rate = exhausted.spent / exhaustion_seconds
    report.samples.append(
        Sample(
            "fuel_adversarial",
            0,
            len(paths),
            exhaustion_seconds,
            {
                "finding": exhausted.finding.value,
                "path_cost": single,
                "path_cost_fraction_of_cap": single / PATH_CAP,
                "units_per_second": rate,
            },
        )
    )
    report.gates["fuel_adversarial_inside_caps"] = (
        single < PATH_CAP and sum(len(path) for path in paths) <= CHANGED_PATH_BYTES
    )
    report.gates["fuel_adversarial_fails_closed"] = exhausted.finding is Finding.FUEL
    slice_seconds = SLICE_UNITS / rate
    latency = cancellation_latency(globs, paths, full)
    report.environment["fuel_slice_seconds"] = slice_seconds
    report.environment["fuel_cancellation_seconds"] = latency
    report.gates["fuel_cancellation_within_one_slice"] = latency <= slice_seconds + 0.5

    failed = [
        gate
        for gate, passed in report.gates.items()
        if gate.startswith("fuel") and not passed
    ]
    assert failed == []


def cancellation_latency(
    globs: tuple[PathGlob, ...], paths: list[str], bounds: FuelBounds
) -> float:
    async def cancelled() -> float:
        task = asyncio.create_task(checked_off_loop(globs, paths * 4, bounds))
        await asyncio.sleep(0.2)
        task.cancel()
        cancelled_at = time.perf_counter()
        try:
            await task
        except asyncio.CancelledError:
            pass
        while any(
            thread.name == "kora-protected-paths" for thread in threading.enumerate()
        ):
            await asyncio.sleep(0.005)
        return time.perf_counter() - cancelled_at

    return asyncio.run(cancelled())
