import asyncio
import signal
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from kora.runner.processes import ExitAnchor
from tests.measurements.lifetime_evidence import PARENT_EXIT, ParentResult


@dataclass(frozen=True)
class Cohort:
    arguments: tuple[str, ...]
    home: Path
    folder: Path


@dataclass
class Parent:
    cohort: Cohort
    process: subprocess.Popen[bytes]
    anchor: ExitAnchor | None = None

    def result(self) -> ParentResult:
        return ParentResult(
            pid=self.process.pid,
            returncode=self.process.returncode,
            reaped=self.process.returncode is not None,
        )


async def wait_parents(
    parents: list[Parent], deadline: float, *, successful: bool, fallback: bool
) -> None:
    cutoff: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    try:
        while True:
            pending: set[asyncio.Future[None]] = {cutoff}
            live = 0
            for parent in parents:
                process = parent.process
                code = process.poll()
                anchor = parent.anchor
                if code is None and anchor is not None and anchor.exited:
                    code = process.wait()
                if code is not None:
                    if successful and code != 0:
                        raise RuntimeError(
                            f"measurement parent {process.pid} exited {code}"
                        )
                    continue
                live += 1
                if anchor is None or anchor.unknown:
                    if not fallback:
                        raise RuntimeError(
                            "measurement parent exit observation unknown"
                        )
                    continue
                pending.add(anchor.readiness)
            if not live:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("measurement cohort deadline")
            await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
    finally:
        cutoff.cancel()


def signal_parents(parents: list[Parent], value: signal.Signals) -> None:
    for parent in parents:
        if parent.process.poll() is None:
            parent.process.send_signal(value)


async def cleanup_parents(parents: list[Parent], final: float) -> None:
    signal_parents(parents, signal.SIGINT)
    try:
        await wait_parents(
            parents, max(time.monotonic(), final - 1), successful=False, fallback=True
        )
    except TimeoutError:
        signal_parents(parents, signal.SIGKILL)
        await wait_parents(parents, final, successful=False, fallback=True)


async def run_cohorts(
    cohorts: tuple[Cohort, ...], deadline: float
) -> tuple[ParentResult, ...]:
    if len(cohorts) != 3:
        raise ValueError("exactly three independent cohorts required")
    parents: list[Parent] = []
    resources = ExitStack()
    try:
        for cohort in cohorts:
            stdout = resources.enter_context((cohort.folder / "stdout.json").open("xb"))
            stderr = resources.enter_context((cohort.folder / "stderr.txt").open("xb"))
            process = subprocess.Popen(
                cohort.arguments,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
            )
            parent = Parent(cohort, process)
            parents.append(parent)
            parent.anchor = ExitAnchor(process.pid)
            resources.callback(parent.anchor.close)
        await wait_parents(parents, deadline - 5, successful=True, fallback=False)
        return tuple(parent.result() for parent in parents)
    finally:
        final = min(time.monotonic() + 5, deadline)
        cleanup = asyncio.create_task(cleanup_parents(parents, final))
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    cancelled = True
        finally:
            try:
                for parent in parents:
                    result = parent.result()
                    (parent.cohort.folder / PARENT_EXIT).write_text(
                        result.model_dump_json()
                    )
            finally:
                resources.close()
        if cancelled:
            raise asyncio.CancelledError
