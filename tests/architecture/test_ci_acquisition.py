import asyncio
import hashlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.ci.acquire import (
    ARTIFACTS,
    ATTEMPTS,
    BUDGET_LEDGER,
    COMMAND_RESERVE_SECONDS,
    FETCH_RESERVE_SECONDS,
    JOB_BUDGET_SECONDS,
    MAX_COMMAND_SECONDS,
    RETRY_SPACING_SECONDS,
    AcquisitionError,
    Artifact,
    Transport,
    bounded,
    fetch,
    main,
    opened_budget,
    run_bounded,
)
from tests.support.https_fixture import (
    Authority,
    Body,
    Drop,
    HttpsServer,
    Redirect,
    Reply,
    Stall,
    create_authority,
    https_server,
)
from tests.support.workflows import jobs_of, repository_workflows, step_list

CONTENT = b"pinned fixture archive bytes"
PLANTED_CREDENTIAL = "kora-fixture-credential-value"
NAME = "fixture"
ROOT = Path(__file__).resolve().parents[2]
COMMAND_LIMIT = re.compile(r'acquire\.py"? run (\d+) --')
FETCHING_SCRIPTS = {
    "scripts/ci/setup_scaffold_node.sh": ("node-",),
    "scripts/ci/setup_uv_cache.sh": ("uv-", "cpython-"),
    "scripts/ci/check_links.sh": ("lychee-",),
}
COMMAND_SCRIPTS = {
    "scripts/ci/apt_install.sh": "scripts/ci/apt_install.sh",
    "prepare-local-runtime-fixtures.sh prepare": "scripts/ci/apt_install.sh",
}
DEADLINE_MARGIN_SECONDS = 120.0
INVOCATION_ALLOWANCE_SECONDS = 5.0
STOP_DEADLINE_SECONDS = 10.0
LEADER = """import os
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
follower = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "signal.pause()\\n",
    ]
)
with open(sys.argv[1], "w") as record:
    record.write(f"{os.getpid()} {follower.pid}\\n")
signal.pause()
"""


@dataclass
class RecordedPauses:
    seconds: list[float] = field(default_factory=list[float])

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


@dataclass(frozen=True)
class Outcome:
    code: int
    output: str
    errors: str


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    directory = tmp_path / "authority"
    directory.mkdir()
    return create_authority(directory)


def artifact_for(
    origin: HttpsServer,
    content: bytes = CONTENT,
    redirect_host: str | None = None,
    attempt_seconds: float = 10.0,
) -> Artifact:
    return Artifact(
        origin.url("/release/archive.tar.gz"),
        "nested/archive.tar.gz",
        len(content),
        hashlib.sha256(content).hexdigest(),
        redirect_host,
        attempt_seconds,
    )


def acquire(
    store: Path, artifact: Artifact, authority: Authority, pauses: RecordedPauses
) -> Outcome:
    output, errors = io.StringIO(), io.StringIO()
    code = asyncio.run(
        main(
            ("fetch", str(store), NAME),
            {NAME: artifact},
            authority.bundle,
            pauses,
            output,
            errors,
            store.parent / BUDGET_LEDGER,
        )
    )
    return Outcome(code, output.getvalue(), errors.getvalue())


def curl() -> str:
    found = shutil.which("curl")
    assert found is not None
    return found


@pytest.mark.parametrize(
    "served",
    [
        CONTENT[:-1] + b"!",
        CONTENT[:-1],
        CONTENT + b"!",
    ],
    ids=["digest", "short", "oversized"],
)
def test_digest_or_size_mismatch_fails_after_one_attempt(
    tmp_path: Path, authority: Authority, served: bytes
) -> None:
    pauses = RecordedPauses()

    with https_server(authority, [Body(200, served)]) as origin:
        outcome = acquire(tmp_path / "store", artifact_for(origin), authority, pauses)

    assert outcome.code == 1
    assert f"Acquisition of {NAME} refused" in outcome.errors
    assert "not retried" in outcome.errors
    assert len(origin.requests) == 1
    assert pauses.seconds == []
    assert list((tmp_path / "store" / "nested").iterdir()) == []


@pytest.mark.parametrize("failure", [Body(503, b"busy"), Drop()], ids=["503", "drop"])
def test_transient_failure_then_success_is_accepted(
    tmp_path: Path, authority: Authority, failure: Reply
) -> None:
    pauses = RecordedPauses()

    with https_server(authority, [failure, Body(200, CONTENT)]) as origin:
        outcome = acquire(tmp_path / "store", artifact_for(origin), authority, pauses)

    destination = tmp_path / "store" / "nested" / "archive.tar.gz"
    assert outcome.code == 0
    assert outcome.output == f"{destination}\n"
    assert destination.read_bytes() == CONTENT
    assert len(origin.requests) == 2
    assert pauses.seconds == [RETRY_SPACING_SECONDS]
    assert "Acquisition attempt 1 of 3 for fixture failed" in outcome.errors


def test_exhausted_attempts_fail_with_a_clear_message(
    tmp_path: Path, authority: Authority
) -> None:
    pauses = RecordedPauses()

    with https_server(authority, [Body(502, b"gateway")]) as origin:
        outcome = acquire(tmp_path / "store", artifact_for(origin), authority, pauses)

    assert outcome.code == 1
    assert outcome.output == ""
    assert outcome.errors.count("failed: HTTP status 502.") == ATTEMPTS
    assert outcome.errors.endswith(
        f"Acquisition of {NAME} failed after {ATTEMPTS} attempts.\n"
    )
    assert len(origin.requests) == ATTEMPTS
    assert pauses.seconds == [RETRY_SPACING_SECONDS] * (ATTEMPTS - 1)
    assert not (tmp_path / "store" / "nested" / "archive.tar.gz").exists()


def test_attempt_time_limit_is_enforced(tmp_path: Path, authority: Authority) -> None:
    pauses = RecordedPauses()
    started = time.monotonic()

    with https_server(authority, [Stall(len(CONTENT))]) as origin:
        outcome = acquire(
            tmp_path / "store",
            artifact_for(origin, attempt_seconds=1.0),
            authority,
            pauses,
        )
        elapsed = time.monotonic() - started

    assert outcome.code == 1
    assert outcome.errors.count("time limit reached") == ATTEMPTS
    assert len(origin.requests) == ATTEMPTS
    assert elapsed < 20.0


@pytest.mark.parametrize(
    "location",
    [
        "https://{foreign}/asset",
        "https://localhost:{port}/asset",
        "http://{asset}/asset",
        "https://user@{asset}/asset",
        "https://{asset}/asset#fragment",
        "https://127.0.0.1.example:{port}/asset",
        "https://{asset}/" + "a" * 8192,
    ],
    ids=["foreign", "alias", "http", "userinfo", "fragment", "suffix", "overlong"],
)
def test_redirect_to_a_foreign_host_is_refused(
    tmp_path: Path, authority: Authority, location: str
) -> None:
    pauses = RecordedPauses()

    with (
        https_server(authority, [Body(200, CONTENT)]) as asset,
        https_server(authority, [Body(200, CONTENT)]) as foreign,
    ):
        target = location.format(
            asset=asset.netloc,
            foreign=foreign.netloc,
            port=asset.netloc.rpartition(":")[2],
        )
        with https_server(authority, [Redirect(target)]) as origin:
            outcome = acquire(
                tmp_path / "store",
                artifact_for(origin, redirect_host=asset.netloc),
                authority,
                pauses,
            )

    assert outcome.code == 1
    assert "the redirect leaves the admitted asset host; not retried" in outcome.errors
    assert target not in outcome.errors
    assert len(origin.requests) == 1
    assert asset.requests == []
    assert foreign.requests == []
    assert pauses.seconds == []


def test_redirect_without_an_admitted_host_is_refused(
    tmp_path: Path, authority: Authority
) -> None:
    pauses = RecordedPauses()

    with (
        https_server(authority, [Body(200, CONTENT)]) as asset,
        https_server(authority, [Redirect(asset.url("/asset"))]) as origin,
    ):
        outcome = acquire(tmp_path / "store", artifact_for(origin), authority, pauses)

    assert outcome.code == 1
    assert "without an admitted asset host; not retried" in outcome.errors
    assert asset.requests == []


def test_credentials_are_never_forwarded(tmp_path: Path, authority: Authority) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".curlrc").write_text(
        f'header = "Authorization: Bearer {PLANTED_CREDENTIAL}"\n'
    )
    (home / ".netrc").write_text(
        f"machine 127.0.0.1 login kora password {PLANTED_CREDENTIAL}\n"
    )
    store = tmp_path / "store"

    with https_server(authority, [Body(200, CONTENT)]) as asset:
        with https_server(
            authority, [Redirect(asset.url("/signed?signature=1"))]
        ) as origin:
            path = asyncio.run(
                fetch(
                    store,
                    NAME,
                    artifact_for(origin, redirect_host=asset.netloc),
                    Transport(curl(), home, authority.bundle),
                    RecordedPauses(),
                    io.StringIO(),
                    opened_budget(tmp_path / BUDGET_LEDGER),
                )
            )

    requests = [*origin.requests, *asset.requests]
    assert path.read_bytes() == CONTENT
    assert [request.path for request in requests] == [
        "/release/archive.tar.gz",
        "/signed?signature=1",
    ]
    assert not any(request.authorization for request in requests)
    assert not any(PLANTED_CREDENTIAL in request.text for request in requests)


@pytest.mark.parametrize("restored", ["altered", "symlink", "linked_directory"])
def test_restored_content_is_verified_before_use(
    tmp_path: Path, authority: Authority, restored: str
) -> None:
    store = tmp_path / "store"
    genuine = tmp_path / "genuine"
    (genuine / "nested").mkdir(parents=True)
    (genuine / "nested" / "archive.tar.gz").write_bytes(CONTENT)
    store.mkdir()
    if restored == "linked_directory":
        (store / "nested").symlink_to(genuine / "nested", target_is_directory=True)
    else:
        (store / "nested").mkdir()
        destination = store / "nested" / "archive.tar.gz"
        if restored == "symlink":
            destination.symlink_to(genuine / "nested" / "archive.tar.gz")
        else:
            destination.write_bytes(CONTENT[:-1] + b"!")
    pauses = RecordedPauses()

    with https_server(authority, [Body(200, CONTENT)]) as origin:
        outcome = acquire(store, artifact_for(origin), authority, pauses)

    assert outcome.code == 1
    assert origin.requests == []
    assert outcome.output == ""
    assert outcome.errors.startswith(
        "Restored fixture failed"
        if restored != "linked_directory"
        else "The acquisition"
    )


def test_verified_restored_content_needs_no_request(
    tmp_path: Path, authority: Authority
) -> None:
    destination = tmp_path / "store" / "nested" / "archive.tar.gz"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(CONTENT)

    with https_server(authority, [Body(500, b"unused")]) as origin:
        outcome = acquire(
            tmp_path / "store", artifact_for(origin), authority, RecordedPauses()
        )

    assert outcome.code == 0
    assert outcome.output == f"{destination}\n"
    assert origin.requests == []


def test_pinned_artifacts_name_exact_sources_and_hosts() -> None:
    hosts = {artifact.redirect_host for artifact in ARTIFACTS.values()}
    paths = [artifact.path for artifact in ARTIFACTS.values()]

    assert hosts == {None, "release-assets.githubusercontent.com"}
    assert len(set(paths)) == len(paths)
    assert all(artifact.url.startswith("https://") for artifact in ARTIFACTS.values())
    assert all(len(artifact.sha256) == 64 for artifact in ARTIFACTS.values())
    assert all(
        artifact.attempt_seconds + FETCH_RESERVE_SECONDS < JOB_BUDGET_SECONDS
        for artifact in ARTIFACTS.values()
    )
    assert MAX_COMMAND_SECONDS + COMMAND_RESERVE_SECONDS < JOB_BUDGET_SECONDS


def test_job_budget_caps_attempts_across_invocations(
    tmp_path: Path, authority: Authority
) -> None:
    ledger = tmp_path / BUDGET_LEDGER
    ledger.write_text(f"{JOB_BUDGET_SECONDS - FETCH_RESERVE_SECONDS - 2}\n")
    pauses = RecordedPauses()
    started = time.monotonic()

    with https_server(authority, [Stall(len(CONTENT))]) as origin:
        outcome = acquire(
            tmp_path / "store",
            artifact_for(origin, attempt_seconds=30.0),
            authority,
            pauses,
        )
        elapsed = time.monotonic() - started

    spent = float(ledger.read_text())
    assert outcome.code == 1
    assert outcome.errors.endswith(
        f"The job acquisition budget of {JOB_BUDGET_SECONDS:g} s is exhausted; "
        f"{NAME} was not acquired.\n"
    )
    assert len(origin.requests) == 1
    assert pauses.seconds == []
    assert elapsed < 15.0
    assert JOB_BUDGET_SECONDS - FETCH_RESERVE_SECONDS <= spent <= JOB_BUDGET_SECONDS


@pytest.mark.parametrize(
    ("ledger_text", "message"),
    [
        (f"{JOB_BUDGET_SECONDS}\n", "acquisition budget of 240 s is exhausted"),
        ("not a duration\n", "is invalid"),
        ("-1\n", "is invalid"),
    ],
    ids=["exhausted", "invalid", "negative"],
)
@pytest.mark.parametrize("mode", ["fetch", "run"])
def test_spent_or_invalid_budget_refuses_before_any_acquisition(
    tmp_path: Path, authority: Authority, ledger_text: str, message: str, mode: str
) -> None:
    ledger = tmp_path / BUDGET_LEDGER
    ledger.write_text(ledger_text)
    marker = tmp_path / "ran"
    command = (
        "run",
        "10",
        "--",
        sys.executable,
        "-c",
        "import pathlib, sys\npathlib.Path(sys.argv[1]).touch()\n",
        str(marker),
    )
    errors = io.StringIO()

    with https_server(authority, [Body(200, CONTENT)]) as origin:
        code = asyncio.run(
            main(
                ("fetch", str(tmp_path / "store"), NAME)
                if mode == "fetch"
                else command,
                {NAME: artifact_for(origin)},
                authority.bundle,
                RecordedPauses(),
                io.StringIO(),
                errors,
                ledger,
            )
        )

    assert code == 1
    assert message in errors.getvalue()
    assert origin.requests == []
    assert not marker.exists()


def test_missing_job_directory_is_refused() -> None:
    errors = io.StringIO()

    code = asyncio.run(
        main(
            ("run", "10", "--", "true"),
            ARTIFACTS,
            None,
            RecordedPauses(),
            io.StringIO(),
            errors,
            None,
        )
    )

    assert code == 1
    assert errors.getvalue().startswith("RUNNER_TEMP must name")


def worst_case(limit: float, reserve: float) -> float:
    return ATTEMPTS * (limit + reserve) + (ATTEMPTS - 1) * RETRY_SPACING_SECONDS


def command_worst_cases(text: str) -> list[float]:
    return [
        worst_case(float(limit), COMMAND_RESERVE_SECONDS)
        for limit in COMMAND_LIMIT.findall(text)
    ]


def fetch_worst_case(prefix: str) -> float:
    return max(
        worst_case(artifact.attempt_seconds, FETCH_RESERVE_SECONDS)
        for name, artifact in ARTIFACTS.items()
        if name.startswith(prefix)
    )


def step_worst_cases(run: str) -> list[float]:
    cases = command_worst_cases(run)
    for script, prefixes in FETCHING_SCRIPTS.items():
        if script in run:
            cases.extend(fetch_worst_case(prefix) for prefix in prefixes)
    for marker, script in COMMAND_SCRIPTS.items():
        if marker in run:
            cases.extend(command_worst_cases((ROOT / script).read_text()))
    return cases


def test_every_acquiring_script_is_accounted_for() -> None:
    scripts = sorted((ROOT / "scripts/ci").glob("*.sh"))

    acquiring = {
        str(path.relative_to(ROOT))
        for path in scripts
        if "acquire.py" in path.read_text()
    }
    installing = {
        str(path.relative_to(ROOT))
        for path in scripts
        if "apt_install.sh" in path.read_text()
    }

    assert acquiring == {*FETCHING_SCRIPTS, "scripts/ci/apt_install.sh"}
    assert installing == {"scripts/ci/prepare-local-runtime-fixtures.sh"}


def test_every_job_acquisition_worst_case_fits_its_deadline() -> None:
    jobs = [
        (
            location,
            job,
            [
                case
                for step in step_list(job)
                for case in step_worst_cases(str(step.get("run", "")))
            ],
        )
        for location, job in jobs_of(repository_workflows())
    ]

    acquiring = [(location, job, cases) for location, job, cases in jobs if cases]
    exceeding = [
        (location, sum(cases))
        for location, job, cases in acquiring
        if min(sum(cases), JOB_BUDGET_SECONDS)
        + len(cases) * INVOCATION_ALLOWANCE_SECONDS
        > float(str(job["timeout-minutes"])) * 60 - DEADLINE_MARGIN_SECONDS
    ]
    assert len(acquiring) >= 10
    assert exceeding == []


def run_command(
    command: tuple[str, ...], seconds: float, pauses: RecordedPauses, ledger: Path
) -> tuple[str | None, str]:
    errors = io.StringIO()
    try:
        asyncio.run(
            run_bounded(command, seconds, pauses, errors, opened_budget(ledger))
        )
    except AcquisitionError as error:
        return str(error), errors.getvalue()
    return None, errors.getvalue()


def test_command_is_retried_until_it_succeeds(tmp_path: Path) -> None:
    marker = tmp_path / "attempted"
    program = (
        "import pathlib, sys\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "seen = marker.exists()\n"
        "marker.touch()\n"
        "raise SystemExit(0 if seen else 7)\n"
    )
    pauses = RecordedPauses()

    failure, errors = run_command(
        (sys.executable, "-c", program, str(marker)),
        10.0,
        pauses,
        tmp_path / BUDGET_LEDGER,
    )

    assert failure is None
    assert "Acquisition attempt 1 of 3 failed (exit status 7)" in errors
    assert pauses.seconds == [RETRY_SPACING_SECONDS]


def test_command_exhausted_attempts_fail_with_a_clear_message(tmp_path: Path) -> None:
    pauses = RecordedPauses()

    failure, errors = run_command(
        (sys.executable, "-c", "raise SystemExit(3)"),
        10.0,
        pauses,
        tmp_path / BUDGET_LEDGER,
    )

    assert failure is not None
    assert failure.startswith(f"Acquisition failed after {ATTEMPTS} attempts: ")
    assert errors.count("(exit status 3)") == ATTEMPTS
    assert pauses.seconds == [RETRY_SPACING_SECONDS] * (ATTEMPTS - 1)


def test_command_time_limit_applies_to_each_attempt(tmp_path: Path) -> None:
    pauses = RecordedPauses()
    started = time.monotonic()

    failure, errors = run_command(
        (sys.executable, "-c", "import signal\nsignal.pause()\n"),
        1.0,
        pauses,
        tmp_path / BUDGET_LEDGER,
    )

    assert failure is not None
    assert errors.count("(time limit of 1 s reached)") == ATTEMPTS
    assert time.monotonic() - started < 20.0


def running(pid: int) -> bool:
    status = shutil.which("ps")
    assert status is not None
    state = subprocess.run(
        (status, "-o", "stat=", "-p", str(pid)),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    ).stdout.strip()
    return state != "" and not state.startswith("Z")


def stopped(pid: int) -> bool:
    deadline = time.monotonic() + STOP_DEADLINE_SECONDS
    while running(pid):
        if time.monotonic() > deadline:
            return False
    return True


def stop_leftover(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def test_command_time_limit_kills_the_whole_process_group(tmp_path: Path) -> None:
    leader = tmp_path / "leader.py"
    leader.write_text(LEADER)
    record = tmp_path / "processes"

    try:
        failure = asyncio.run(bounded((sys.executable, str(leader), str(record)), 2.0))
        processes = [int(value) for value in record.read_text().split()]

        assert failure == "time limit of 2 s reached"
        assert len(processes) == 2
        assert [pid for pid in processes if not stopped(pid)] == []
    finally:
        for value in record.read_text().split() if record.exists() else ():
            stop_leftover(int(value))


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("fetch", "store"),
        ("run", "0", "--", "true"),
        ("run", str(MAX_COMMAND_SECONDS + 1), "--", "true"),
        ("run", "10", "true"),
        ("run", "1.5", "--", "true"),
    ],
)
def test_invalid_usage_is_refused(arguments: tuple[str, ...]) -> None:
    errors = io.StringIO()

    code = asyncio.run(
        main(arguments, ARTIFACTS, None, RecordedPauses(), io.StringIO(), errors, None)
    )

    assert code == 2
    assert errors.getvalue().startswith("usage: acquire.py")
