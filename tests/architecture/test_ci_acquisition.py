import asyncio
import hashlib
import io
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.ci.acquire import (
    ARTIFACTS,
    ATTEMPTS,
    RETRY_SPACING_SECONDS,
    AcquisitionError,
    Artifact,
    Transport,
    fetch,
    main,
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

CONTENT = b"pinned fixture archive bytes"
PLANTED_CREDENTIAL = "kora-fixture-credential-value"
NAME = "fixture"


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
        artifact.attempt_seconds * ATTEMPTS + RETRY_SPACING_SECONDS * (ATTEMPTS - 1)
        < 300
        for artifact in ARTIFACTS.values()
    )


def run_command(
    command: tuple[str, ...], seconds: float, pauses: RecordedPauses
) -> tuple[str | None, str]:
    errors = io.StringIO()
    try:
        asyncio.run(run_bounded(command, seconds, pauses, errors))
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
        (sys.executable, "-c", program, str(marker)), 10.0, pauses
    )

    assert failure is None
    assert "Acquisition attempt 1 of 3 failed (exit status 7)" in errors
    assert pauses.seconds == [RETRY_SPACING_SECONDS]


def test_command_exhausted_attempts_fail_with_a_clear_message() -> None:
    pauses = RecordedPauses()

    failure, errors = run_command(
        (sys.executable, "-c", "raise SystemExit(3)"), 10.0, pauses
    )

    assert failure is not None
    assert failure.startswith(f"Acquisition failed after {ATTEMPTS} attempts: ")
    assert errors.count("(exit status 3)") == ATTEMPTS
    assert pauses.seconds == [RETRY_SPACING_SECONDS] * (ATTEMPTS - 1)


def test_command_time_limit_stops_each_attempt() -> None:
    pauses = RecordedPauses()
    started = time.monotonic()

    failure, errors = run_command(
        (sys.executable, "-c", "import signal\nsignal.pause()\n"), 1.0, pauses
    )

    assert failure is not None
    assert errors.count("(time limit of 1 s reached)") == ATTEMPTS
    assert time.monotonic() - started < 20.0


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("fetch", "store"),
        ("run", "0", "--", "true"),
        ("run", "151", "--", "true"),
        ("run", "10", "true"),
        ("run", "1.5", "--", "true"),
    ],
)
def test_invalid_usage_is_refused(arguments: tuple[str, ...]) -> None:
    errors = io.StringIO()

    code = asyncio.run(
        main(arguments, ARTIFACTS, None, RecordedPauses(), io.StringIO(), errors)
    )

    assert code == 2
    assert errors.getvalue().startswith("usage: acquire.py")
