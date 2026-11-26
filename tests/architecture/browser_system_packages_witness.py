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
        env={"LANG": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
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
        BrowserPackagesRefused,
        Service,
    )

    window = AttemptWindow.allocate(WITNESS_ALLOWANCE)
    service = Service.new()
    client = await service.launch(("witness", str(administrative), "hold"), window)
    owned = None
    try:
        owned = await service.bind(client, window.start)
        assert client.stdin is not None
        if case is RefusedAdmission.FOREIGN:
            client.stdin.write(f"{'0' * 32} {service.cgroup}\n".encode("ascii"))
            await client.stdin.drain()
        if case is not RefusedAdmission.LATE:
            client.stdin.close()
        try:
            await owned.finished(window.settled)
        except BrowserPackagesRefused as error:
            refusal = str(error)
        else:
            raise AssertionError("The helper did not refuse admission.")
        assert refusal == "The package helper refused admission."
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


async def check_expired_start(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import (
        SYSTEM_CGROUP,
        AttemptWindow,
        OriginalService,
        Service,
    )

    service = Service.new()
    window = AttemptWindow.allocate(WITNESS_ALLOWANCE)
    client = await service.launch(
        ("witness", str(administrative), "hold"),
        window,
        gate_cutoff=time.monotonic() - 1,
    )
    try:
        invocation = await service.offer(client, window.start)
        async with asyncio.timeout_at(window.settled):
            assert await client.wait() != 0
        status = await service.show(window.prove)
        assert status["Id"] == service.name
        assert status["InvocationID"] == invocation
        assert status["ActiveState"] == "failed"
        assert status["Job"] == ""
        assert status["ExecMainStatus"] == "78"
        assert not (administrative / "held").exists()
        try:
            owned = OriginalService.capture(service, invocation)
        except FileNotFoundError:
            cgroup = SYSTEM_CGROUP / service.cgroup[1:]
            assert not cgroup.exists()
        else:
            try:
                assert owned.empty()
            finally:
                os.close(owned.directory)
        code, _ = await service.control(("reset-failed",), window.prove, root=True)
        assert code == 0
    finally:
        if client.stdin is not None:
            client.stdin.close()


async def wait_for_lock(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import STOP_SECONDS, poll_until

    ready_by = time.monotonic() + WITNESS_ALLOWANCE - STOP_SECONDS
    while not (administrative / "held").is_file():
        await poll_until(ready_by)


async def start_unowned_controller(administrative: Path, service_name: str) -> None:
    from scripts.ci.browser_system_packages import AttemptWindow, Service

    service = Service(service_name, f"/system.slice/{service_name}")
    window = AttemptWindow.allocate(WITNESS_ALLOWANCE)
    process = await service.launch(("witness", str(administrative), "hold"), window)
    owned = await service.bind(process, window.start)
    try:
        (administrative / "invocation").write_text(owned.invocation, encoding="ascii")
        await owned.admit(process, window.start)
        await owned.confirm_admission(process, window.start)
        await process.wait()
    finally:
        os.close(owned.directory)


async def check_controller_loss(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import OriginalService, Service

    service = Service.new()
    controller = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.architecture.browser_system_packages_witness",
        "--abandon",
        str(administrative),
        service.name,
    )
    deadline = time.monotonic() + WITNESS_ALLOWANCE
    try:
        await wait_for_lock(administrative)
        invocation = (administrative / "invocation").read_text(encoding="ascii")
        await service.require_running(invocation, deadline)
        owned = OriginalService.capture(service, invocation)
        try:
            code, errors = await configure(administrative)
            assert code != 0
            assert b"dpkg database lock was locked" in errors
            controller.kill()
            async with asyncio.timeout_at(deadline):
                assert await controller.wait() == -signal.SIGKILL

            succeeded = await owned.finished(deadline)
            assert not succeeded
            assert (await configure(administrative))[0] == 0
            await owned.settle(deadline + 5)
        finally:
            os.close(owned.directory)
    finally:
        if controller.returncode is None:
            controller.kill()
            async with asyncio.timeout(2):
                await controller.wait()


async def check_refusal_is_terminal(administrative: Path) -> None:
    from scripts.ci.acquire import opened_budget, run_attempts
    from scripts.ci.browser_system_packages import (
        BrowserPackagesRefused,
        package_attempt,
    )

    async def rejected(limit: float) -> str | None:
        return await package_attempt(
            ("witness", str(administrative), "unsupported"), limit
        )

    async def pause(_: float) -> None:
        raise AssertionError("An unconfirmed package admission cannot retry.")

    ledger = administrative / "budget"
    try:
        await run_attempts(
            "private package state",
            WITNESS_ALLOWANCE,
            pause,
            sys.stderr,
            opened_budget(ledger),
            rejected,
        )
    except BrowserPackagesRefused as error:
        refusal = str(error)
    else:
        raise AssertionError("The unconfirmed attempt was not refused.")
    assert refusal == "Package helper admission was not confirmed."


async def check_collision(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import (
        AttemptWindow,
        BrowserPackagesRefused,
        Service,
        package_attempt,
    )

    service = Service.new()
    window = AttemptWindow.allocate(WITNESS_ALLOWANCE + 10)
    work = ("witness", str(administrative), "hold")
    client = await service.launch(work, window)
    owned = None
    try:
        owned = await service.bind(client, window.start)
        await owned.admit(client, window.start)
        await owned.confirm_admission(client, window.start)
        await wait_for_lock(administrative)
        deadline = window.settled
        before = await service.show(deadline)
        original_pid = int(before["MainPID"])
        assert original_pid > 0

        try:
            await package_attempt(work, WITNESS_ALLOWANCE, service=service)
        except BrowserPackagesRefused as error:
            refusal = str(error)
        else:
            raise AssertionError("The same-name collision was not refused.")

        after = await service.show(deadline)
        assert refusal == "The launched package helper did not establish ownership."
        assert after["InvocationID"] == owned.invocation
        assert int(after["MainPID"]) == original_pid
        assert after["ActiveState"] == "active"
        assert (await configure(administrative))[0] != 0
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


async def check_lifecycle(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import package_attempt

    for directory in ("info", "updates", "parts", "triggers"):
        (administrative / directory).mkdir()
    for filename in ("lock", "status", "available"):
        (administrative / filename).touch()
    assert (await configure(administrative))[0] == 0

    for case in RefusedAdmission:
        await confirm_refused_admission(administrative, case)
    await check_expired_start(administrative)
    await check_refusal_is_terminal(administrative)
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

    (administrative / "held").unlink()
    await check_controller_loss(administrative)
    sys.stdout.write("The manager settled the package service after controller loss.\n")

    (administrative / "held").unlink()
    await check_collision(administrative)
    sys.stdout.write(
        "A same-name collision preserved the original service and process.\n"
    )


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--abandon":
        asyncio.run(start_unowned_controller(Path(sys.argv[2]), sys.argv[3]))
        return 0
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
