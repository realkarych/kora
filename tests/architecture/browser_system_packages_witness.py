from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import sys
import tempfile
import time
from enum import StrEnum
from pathlib import Path

WITNESS_ALLOWANCE = 21.0


class RefusedAdmission(StrEnum):
    LOST = "lost"
    FOREIGN = "foreign"
    LATE = "late"


def hold(administrative: Path) -> None:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with (administrative / "lock").open("r+") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        (administrative / "held").touch(exist_ok=False)
        signal.pause()


async def configure(administrative: Path) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/dpkg",
        f"--admindir={administrative}",
        f"--instdir={administrative}",
        "--force-not-root",
        "--configure",
        "-a",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )
    try:
        async with asyncio.timeout(3):
            _, errors = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    code = process.returncode
    assert code is not None
    return code, errors


async def confirm_refused_admission(
    administrative: Path, case: RefusedAdmission
) -> None:
    from scripts.ci.browser_system_packages import (
        AttemptWindow,
        OriginalService,
        Service,
    )

    window = AttemptWindow.allocate(WITNESS_ALLOWANCE)
    service = Service.new()
    client = await service.launch(("witness", str(administrative), "hold"), window)
    owned = None
    try:
        invocation = await service.owned(window.start)
        owned = OriginalService.capture(service, invocation)
        assert client.stdin is not None
        if case is RefusedAdmission.FOREIGN:
            client.stdin.write(f"{'0' * 32} {service.cgroup}\n".encode("ascii"))
            await client.stdin.drain()
        if case is RefusedAdmission.LATE:
            await asyncio.sleep(max(0.0, window.start - time.monotonic()) + 0.01)
            await owned.admit(client, window.settled)
        client.stdin.close()
        assert not await owned.finished(window.settled)
        assert not (administrative / "held").exists()
    finally:
        if client.stdin is not None:
            client.stdin.close()
        if owned is not None:
            try:
                await owned.settle(window.prove)
            finally:
                os.close(owned.directory)
        async with asyncio.timeout_at(window.join):
            await client.wait()


async def wait_for_lock(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import STOP_SECONDS, poll_until

    ready_by = time.monotonic() + WITNESS_ALLOWANCE - STOP_SECONDS
    while not (administrative / "held").is_file():
        await poll_until(ready_by)


async def check_lifecycle(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import package_attempt

    for directory in ("info", "updates", "parts", "triggers"):
        (administrative / directory).mkdir()
    for filename in ("lock", "status", "available"):
        (administrative / filename).touch()
    assert (await configure(administrative))[0] == 0

    for case in RefusedAdmission:
        await confirm_refused_admission(administrative, case)
    sys.stdout.write("Package service refused missing, foreign and late admission.\n")
    async with asyncio.TaskGroup() as tasks:
        original = tasks.create_task(
            package_attempt(("witness", str(administrative), "hold"), WITNESS_ALLOWANCE)
        )
        await wait_for_lock(administrative)
        code, errors = await configure(administrative)
        assert code != 0
        assert b"dpkg database lock was locked" in errors
    assert original.result() == "original package service failed after settlement"
    sys.stdout.write("Original package service released its cgroup and dpkg lock.\n")

    subsequent = await package_attempt(
        ("witness", str(administrative), "configure"), WITNESS_ALLOWANCE
    )

    assert subsequent is None
    sys.stdout.write("The next package service configured its private dpkg state.\n")

    (administrative / "held").unlink()
    async with asyncio.TaskGroup() as tasks:
        withdrawing = tasks.create_task(
            package_attempt(("witness", str(administrative), "hold"), WITNESS_ALLOWANCE)
        )
        await wait_for_lock(administrative)
        withdrawing.cancel()

    assert withdrawing.cancelled()
    assert (await configure(administrative))[0] == 0
    sys.stdout.write("Cancellation settled the original package service and lock.\n")


def main() -> int:
    if len(sys.argv) == 3:
        administrative = Path(sys.argv[1])
        if sys.argv[2] == "hold":
            hold(administrative)
            return 2
        if sys.argv[2] == "configure":
            return asyncio.run(configure(administrative))[0]
        return 2
    with tempfile.TemporaryDirectory(
        prefix="kora-browser-dpkg-", dir=os.environ["RUNNER_TEMP"]
    ) as directory:
        asyncio.run(check_lifecycle(Path(directory)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
