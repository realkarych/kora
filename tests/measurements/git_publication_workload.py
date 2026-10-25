import asyncio
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.vcs import (
    Checkout,
    Kept,
    checked_out,
    pinned,
    publish_request,
)
from kora.executor.entry import PreparedRevision
from kora.executor.failures import ExecutorError
from kora.executor.guard import PhaseGuard
from kora.executor.scan import scan
from kora.observability import LoggingSettings
from kora.plugins.vcs.git.plans import INDEX_FILE
from kora.protocols.vcs import (
    InspectionManifest,
    PublicationInput,
    PublishPlan,
    PublishRequest,
)
from tests.plugins.vcs.git_support import GIT, REPO, World, git, make_world

MIB: Final[int] = 1_048_576
SMALL_FILES: Final[int] = 20_000
LARGE_BYTES: Final[int] = 256 * MIB
SMALL_BYTES: Final[int] = MIB
NEAR_CAP_COMMITS: Final[int] = 20
NEAR_CAP_FILES_PER_COMMIT: Final[int] = 9_990
NEAR_CAP_LARGE_BYTES: Final[int] = 240 * MIB


def rss_scale() -> int:
    return 1024 if sys.platform == "linux" else 1


def peak_self() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_scale()


def peak_children() -> int:
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * rss_scale()


def scan_secrets(dataset: str) -> tuple[str, ...]:
    if not dataset.endswith("_secrets"):
        return ()
    return tuple(f"kora-scan-value-{index:04d}-" + "q" * 16 for index in range(256))


def random_file(path: Path, size: int) -> None:
    with path.open("wb") as stream:
        remaining = size
        while remaining:
            block = os.urandom(min(MIB, remaining))
            stream.write(block)
            remaining -= len(block)


def many_files(root: Path, count: int) -> None:
    for index in range(count):
        directory = root / f"d{index % 200:03d}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{index:06d}.txt").write_text(f"file {index}\n" * 8)


def inline_file(path: str, data: bytes) -> bytes:
    return f"M 100644 inline {path}\ndata {len(data)}\n".encode() + data + b"\n"


def fast_import_stream(base: str, first: bytes, last: bytes) -> bytes:
    lines: list[bytes] = []
    previous = base
    for commit in range(NEAR_CAP_COMMITS):
        lines.append(b"commit refs/heads/conformance-work\n")
        lines.append(f"mark :{commit + 1}\n".encode())
        lines.append(b"committer Agent <agent@example.invalid> 1700000000 +0000\n")
        lines.append(b"data 6\nbatch\n")
        lines.append(f"from {previous}\n".encode())
        if commit:
            lines.append(f"D batch{commit - 1}\n".encode())
        for index in range(NEAR_CAP_FILES_PER_COMMIT):
            body = f"near cap {commit} {index}\n".encode()
            lines.append(inline_file(f"batch{commit}/f{index:05d}", body))
        if commit == 0:
            lines.append(inline_file("first.bin", first))
        if commit == NEAR_CAP_COMMITS - 1:
            lines.append(b"D first.bin\n")
            lines.append(inline_file("last.bin", last))
        previous = f":{commit + 1}"
    return b"".join(lines)


def near_cap_candidate(world: World, candidate: Path) -> None:
    stream = fast_import_stream(
        world.base,
        os.urandom(NEAR_CAP_LARGE_BYTES),
        os.urandom(NEAR_CAP_LARGE_BYTES),
    )
    subprocess.run(
        [GIT, "fast-import", "--quiet"],
        cwd=candidate,
        input=stream,
        check=True,
        capture_output=True,
    )
    git(world.home, "checkout", "-q", "-f", "conformance-work", cwd=candidate)


def seeded_files(world: World) -> None:
    many_files(world.seed, SMALL_FILES)
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


class TimedSink:
    def __init__(self) -> None:
        self.kept_at: float | None = None

    async def keep(self, revision: PreparedRevision) -> None:
        self.kept_at = time.perf_counter()


async def near_cap_import(
    world: World,
    plan: PublishPlan,
    sealed: PublicationInput,
    checkout: Checkout,
    request: PublishRequest,
    started: float,
    sealed_at: float,
) -> dict[str, float | int | str]:
    sink = TimedSink()
    try:
        await world.executor.prepare_and_inspect(
            plan,
            sealed,
            checkout.workspace(),
            request,
            pinned(world.binding()),
            checkout.areas,
            (),
            (),
            sink,
            world.context(),
        )
    except ExecutorError as error:
        refused = f"{error.phase.value}:{error.failure_class.value}"
    else:
        refused = "accepted"
    bundle = Path(sealed.location) / "head.bundle"
    return {
        "dataset": "near_cap",
        "seal_seconds": sealed_at - started,
        "import_and_fsck_seconds": (sink.kept_at or sealed_at) - sealed_at,
        "imported": sink.kept_at is not None,
        "bundle_bytes": bundle.stat().st_size,
        "inspection": refused,
        "peak_rss_bytes": peak_self(),
        "children_peak_rss_bytes": peak_children(),
    }


async def measured(world: World, dataset: str) -> dict[str, float | int | str]:
    if dataset == "files20000":
        seeded_files(world)
    checkout = await checked_out(
        world.provider,
        world.executor,
        world.bench().areas(),
        REPO,
        world.base,
        world.context(),
    )
    candidate = world.candidate(checkout)
    if dataset == "files20000":
        for index in range(10):
            (candidate / f"d{index:03d}" / f"f{index:06d}.txt").write_text("changed\n")
    elif dataset in ("large256m", "large256m_secrets"):
        random_file(candidate / "large.bin", LARGE_BYTES)
    elif dataset == "small1m":
        random_file(candidate / "small.bin", SMALL_BYTES)
    else:
        near_cap_candidate(world, candidate)
    context = world.context()
    remote = world.remote_fixture()
    request = publish_request(remote, checkout)
    plan = await world.provider.publish_plan(request, context)
    workspace = checkout.workspace()
    started = time.perf_counter()
    exported = await world.executor.export(plan, workspace, checkout.areas, context)
    sealed = await world.executor.seal(exported, workspace, checkout.areas, context)
    sealed_at = time.perf_counter()
    if dataset == "near_cap":
        return await near_cap_import(
            world, plan, sealed, checkout, request, started, sealed_at
        )
    result = await world.executor.prepare_and_inspect(
        plan,
        sealed,
        workspace,
        request,
        pinned(world.binding()),
        checkout.areas,
        (),
        (),
        Kept(),
        context,
    )
    prepared_at = time.perf_counter()
    index = (Path(result.revision.location) / INDEX_FILE).read_bytes()
    manifest = InspectionManifest(
        prepared_digest=result.revision.prepared_digest,
        revision=result.revision.commit,
        content_index_location=INDEX_FILE,
        content_index_digest="sha256:" + hashlib.sha256(index).hexdigest(),
        complete=True,
    )
    tracemalloc.start()
    scan_started = time.perf_counter()
    async with world.provider.open(result.revision.location) as source:
        verdict = await scan(
            manifest,
            result.revision.prepared_digest,
            result.revision.commit,
            result.revision.base_commit,
            source,
            scan_secrets(dataset),
            world.executor.settings.scan,
            PhaseGuard(world.clock, None),
            context.logger,
        )
    scan_seconds = time.perf_counter() - scan_started
    _, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "dataset": dataset,
        "seal_seconds": sealed_at - started,
        "prepare_and_inspect_seconds": prepared_at - sealed_at,
        "scan_seconds": scan_seconds,
        "scanned_bytes": verdict.scanned_bytes,
        "records": verdict.records,
        "peak_rss_bytes": peak_self(),
        "children_peak_rss_bytes": peak_children(),
        "scan_traced_peak_bytes": traced_peak,
    }


def main(dataset: str) -> None:
    with (
        tempfile.TemporaryDirectory() as directory,
        captured(
            CONFORMANCE_COMPONENT,
            LoggingSettings(),
            "git-publication-cost",
            lambda: datetime.now(UTC),
            deadline_seconds=30.0,
        ) as capture,
    ):
        world = make_world(Path(directory), capture)
        row = asyncio.run(measured(world, dataset))
        capture.finish()
    sys.stdout.write(json.dumps(row))


if __name__ == "__main__":
    main(sys.argv[1])
