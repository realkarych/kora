from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import shlex
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from enum import StrEnum
from pathlib import Path

WITNESS_ALLOWANCE = 21.0
STAGED_SIGNER = "ubuntu-archive-keyring.gpg"
APT_PACKAGE = "kora-browser-apt-witness"
APT_PACKAGE_INDEX = "dists/noble/main/binary-amd64/Packages"
APT_RELEASE = "dists/noble/InRelease"
APT_ENV = {"LANG": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


class RefusedAdmission(StrEnum):
    LOST = "lost"
    FOREIGN = "foreign"
    LATE = "late"


def hold(administrative: Path, stage: Path | None = None) -> None:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with (administrative / "lock").open("r+") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        ready = administrative / "held"
        if stage is None:
            ready.touch(exist_ok=False)
            signal.pause()
            return
        with (stage / STAGED_SIGNER).open("rb") as signer:
            status = os.fstat(signer.fileno())
            assert signer.read(1)
            with ready.open("x", encoding="ascii") as output:
                output.write(f"{status.st_dev} {status.st_ino}\n")
            signal.pause()


def held_signer(administrative: Path, stage: Path) -> tuple[int, int]:
    staged = (stage / STAGED_SIGNER).stat(follow_symlinks=False)
    assert stat.S_ISREG(staged.st_mode)
    assert staged.st_uid == 0
    assert staged.st_mode & 0o022 == 0
    assert (administrative / "held").read_text(encoding="ascii") == (
        f"{staged.st_dev} {staged.st_ino}\n"
    )
    return staged.st_dev, staged.st_ino


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


def dpkg_outcome(code: int, errors: bytes) -> str:
    if code == 0:
        return "configured"
    if b"dpkg database lock was locked" in errors:
        return "database-lock-held"
    if b"Permission denied" in errors or b"Operation not permitted" in errors:
        return "permission-refused"
    return "other-failure"


async def check_dpkg(administrative: Path, expected: str) -> None:
    code, errors = await configure(administrative)
    observed = dpkg_outcome(code, errors)
    assert observed == expected, (
        f"Private dpkg probe returned {observed} (exit {code})."
    )


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
    marker = administrative / "held"
    while not marker.is_file() or marker.stat().st_size == 0:
        await poll_until(ready_by)


async def start_unowned_controller(administrative: Path, service_name: str) -> None:
    from scripts.ci.browser_system_packages import AttemptWindow, Service

    service = Service(service_name, f"/system.slice/{service_name}")
    window = AttemptWindow.allocate(WITNESS_ALLOWANCE)
    process = await service.launch(
        ("witness", str(administrative), "stage-hold"), window
    )
    owned = await service.bind(process, window.start)
    try:
        (administrative / "invocation").write_text(owned.invocation, encoding="ascii")
        await owned.admit(process, window.start)
        await owned.confirm_admission(process, window.start)
        await process.wait()
    finally:
        os.close(owned.directory)


async def check_controller_loss(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import (
        PROOF_SECONDS,
        RETURN_SECONDS,
        OriginalService,
        Service,
    )
    from scripts.ci.browser_ubuntu_apt import Stage

    service = Service.new()
    stage = Stage(service.name).path
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
            signer = held_signer(administrative, stage)
            await check_dpkg(administrative, "database-lock-held")
            controller.kill()
            async with asyncio.timeout_at(deadline):
                assert await controller.wait() == -signal.SIGKILL

            succeeded = await owned.finished(deadline)
            assert not succeeded
            assert held_signer(administrative, stage) == signer
            proof_deadline = deadline + PROOF_SECONDS
            await owned.settle(
                proof_deadline, stage_cleanup_deadline=proof_deadline + RETURN_SECONDS
            )
            await check_dpkg(administrative, "configured")
            assert not stage.exists()
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
    from scripts.ci.browser_ubuntu_apt import Stage

    service = Service.new()
    stage = Stage(service.name).path
    window = AttemptWindow.allocate(WITNESS_ALLOWANCE + 10)
    work = ("witness", str(administrative), "stage-hold")
    client = await service.launch(work, window)
    owned = None
    try:
        owned = await service.bind(client, window.start)
        await owned.admit(client, window.start)
        await owned.confirm_admission(client, window.start)
        await wait_for_lock(administrative)
        signer = held_signer(administrative, stage)
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
        assert held_signer(administrative, stage) == signer
        await check_dpkg(administrative, "database-lock-held")
    finally:
        if client.stdin is not None:
            client.stdin.close()
        if owned is not None:
            try:
                await owned.settle(window.prove, stage_cleanup_deadline=window.join)
            finally:
                os.close(owned.directory)
        async with asyncio.timeout_at(window.join):
            await client.wait()
    assert not stage.exists()


async def initialize_administrative_database(administrative: Path) -> None:
    for directory in ("info", "updates", "parts", "triggers"):
        (administrative / directory).mkdir()
    for filename in ("lock", "status", "available"):
        (administrative / filename).touch()
    await check_dpkg(administrative, "configured")


async def check_timeout_and_retry(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import Service, package_attempt
    from scripts.ci.browser_ubuntu_apt import Stage

    for case in RefusedAdmission:
        await confirm_refused_admission(administrative, case)
    await check_expired_start(administrative)
    await check_refusal_is_terminal(administrative)
    sys.stdout.write("Package service refused missing, foreign and late admission.\n")
    service = Service.new()
    stage = Stage(service.name).path
    async with asyncio.TaskGroup() as tasks:
        original = tasks.create_task(
            package_attempt(
                ("witness", str(administrative), "stage-hold"),
                WITNESS_ALLOWANCE,
                service=service,
            )
        )
        await wait_for_lock(administrative)
        held_signer(administrative, stage)
        await check_dpkg(administrative, "database-lock-held")
    assert original.result() == "original package service failed after settlement"
    assert not stage.exists()
    sys.stdout.write(
        "Original package service released its cgroup, dpkg lock and APT stage.\n"
    )

    subsequent = await package_attempt(
        ("witness", str(administrative), "configure"), WITNESS_ALLOWANCE
    )

    assert subsequent is None
    sys.stdout.write("The next package service configured its private dpkg state.\n")


async def check_supervision(administrative: Path) -> None:
    from scripts.ci.browser_system_packages import Service, package_attempt
    from scripts.ci.browser_ubuntu_apt import Stage

    service = Service.new()
    stage = Stage(service.name).path
    async with asyncio.TaskGroup() as tasks:
        withdrawing = tasks.create_task(
            package_attempt(
                ("witness", str(administrative), "stage-hold"),
                WITNESS_ALLOWANCE,
                service=service,
            )
        )
        await wait_for_lock(administrative)
        held_signer(administrative, stage)
        await check_dpkg(administrative, "database-lock-held")
        withdrawing.cancel()

    assert withdrawing.cancelled()
    assert not stage.exists()
    await check_dpkg(administrative, "configured")
    sys.stdout.write("Cancellation settled the original package service and lock.\n")

    (administrative / "held").unlink()
    await check_controller_loss(administrative)
    sys.stdout.write("The manager settled the package service after controller loss.\n")

    (administrative / "held").unlink()
    await check_collision(administrative)
    sys.stdout.write(
        "A same-name collision preserved the original service and process.\n"
    )


async def check_lifecycle() -> None:
    for check in (check_timeout_and_retry, check_supervision):
        with tempfile.TemporaryDirectory(
            prefix="kora-browser-dpkg-", dir=os.environ["RUNNER_TEMP"]
        ) as directory:
            administrative = Path(directory)
            await initialize_administrative_database(administrative)
            await check(administrative)


async def check_playwright_adapter_refusal() -> None:
    from scripts.ci.browser_system_packages import (
        BrowserPackagesRefused,
        Programs,
        Service,
        package_attempt,
    )
    from scripts.ci.browser_ubuntu_apt import Stage

    programs = Programs.workflow()
    service = Service.new()
    stage = Stage(service.name).path
    work = ("refusal-witness", str(programs.tool_cache), str(programs.pnpm_bin))

    try:
        await package_attempt(work, WITNESS_ALLOWANCE, service=service)
    except BrowserPackagesRefused as error:
        refusal = str(error)
    else:
        raise AssertionError("The normalized Playwright failure could retry.")

    assert refusal == "The package helper refused admission."
    assert not stage.exists()
    sys.stdout.write("Playwright's normalized refusal remained terminal.\n")


async def native_command(
    directory: Path, environment: dict[str, str], *command: str
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=directory,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None
    return process.returncode, output


async def native_success(
    directory: Path, environment: dict[str, str], *command: str
) -> bytes:
    code, output = await native_command(directory, environment, *command)
    assert code == 0, output.decode("utf-8", "replace")
    return output


async def inert_archive(directory: Path, version: str) -> tuple[str, bytes, bytes]:
    package = directory / version
    control = package / "DEBIAN"
    control.mkdir(parents=True)
    metadata = (
        f"Package: {APT_PACKAGE}\nVersion: {version}\nArchitecture: all\n"
        "Maintainer: kora witness <witness@example.invalid>\n"
        "Description: inert browser APT acquisition witness\n"
    )
    (control / "control").write_text(metadata, encoding="ascii")
    filename = f"{APT_PACKAGE}_{version}_all.deb"
    await native_success(
        directory, APT_ENV, "/usr/bin/dpkg-deb", "--build", str(package), filename
    )
    archive = (directory / filename).read_bytes()
    index = (
        f"{metadata}Filename: {filename}\nSize: {len(archive)}\n"
        f"SHA256: {hashlib.sha256(archive).hexdigest()}\n\n"
    ).encode("ascii")
    return filename, archive, index


async def sign_release(
    directory: Path, signing: Sequence[str], index: bytes, valid_until: datetime
) -> bytes:
    release = directory / "Release"
    release.write_text(
        "Suite: noble\nCodename: noble\nArchitectures: amd64\nComponents: main\n"
        f"Date: {format_datetime(datetime.now(UTC) - timedelta(hours=2))}\n"
        f"Valid-Until: {format_datetime(valid_until)}\n"
        f"SHA256:\n {hashlib.sha256(index).hexdigest()} {len(index)}"
        " main/binary-amd64/Packages\n",
        encoding="ascii",
    )
    signed = directory / "InRelease"
    await native_success(
        directory,
        APT_ENV,
        *signing,
        "--output",
        str(signed),
        "--clearsign",
        str(release),
    )
    return signed.read_bytes()


def source_entry(address: str, mirror: str, signer: Path) -> str:
    return f"deb [signed-by={signer}] {address}/{mirror} noble main\n"


def apt_environment(directory: Path, source: Path, parts: Path) -> dict[str, str]:
    marker = shlex.quote(str(directory / "update-hook"))
    configuration = (
        f'Dir::Etc::Parts "{parts}";\nDir::Etc::Main "/dev/null";\n'
        f'Dir::Etc::sourcelist "{source}";\n'
        f'Dir::Cache::pkgcache "{directory / "pkg.bin"}";\n'
        f'Dir::Cache::srcpkgcache "{directory / "srcpkg.bin"}";\n'
        'Acquire::Languages "none";\nAcquire::Retries "0";\n'
        'Acquire::http::Timeout "2";\nDebug::NoLocking "true";\n'
        f'APT::Update::Post-Invoke:: "/usr/bin/touch {marker}";\n'
    )
    (directory / "profile.conf").write_text(configuration, encoding="ascii")
    (directory / "status").touch()
    isolated = directory / "apt.conf"
    isolated.write_text(
        configuration + f'Dir::State::status "{directory / "status"}";\n',
        encoding="ascii",
    )
    return {**APT_ENV, "APT_CONFIG": str(isolated)}


async def check_native_profile(
    directory: Path, ambient: Path, selectors: dict[str, str]
) -> None:
    from scripts.ci.browser_ubuntu_apt import AptRefused, require_profile

    profile = directory / "profile.conf"
    environment = {**APT_ENV, "APT_CONFIG": str(profile)}
    late_source = (
        profile.read_text(encoding="ascii")
        + f'Binary::apt-get::Dir::Etc::sourcelist "{ambient}";\n'
    )
    profile.write_text(late_source, encoding="ascii")
    await require_profile(selectors, environment)

    profile.write_text(
        late_source + 'Binary::apt-get::APT::Sources::With "foreign";\n',
        encoding="ascii",
    )
    try:
        await require_profile(selectors, environment)
    except AptRefused as refusal:
        outcome = str(refusal)
    else:
        raise AssertionError("An additional native source was not refused.")
    assert outcome.startswith("Effective apt-get inputs differ")


async def apt_source_selection(directory: Path, signing: Sequence[str]) -> None:
    signer = directory / "signer.gpg"
    await native_success(
        directory, APT_ENV, *signing, "--output", str(signer), "--export"
    )
    files: dict[str, bytes] = {}
    for prefix, version in (("admitted", "1.0"), ("unrelated", "2.0")):
        filename, archive, index = await inert_archive(directory, version)
        files[f"/{prefix}/{filename}"] = archive
        files[f"/{prefix}/{APT_PACKAGE_INDEX}"] = index
        files[f"/{prefix}/{APT_RELEASE}"] = await sign_release(
            directory, signing, index, datetime.now(UTC) + timedelta(days=1)
        )

    requests: list[str] = []
    unavailable: set[str] = set()

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(3):
                header = await reader.readuntil(b"\r\n\r\n")
                target = header.split(b" ", 2)[1].decode("ascii")
                requests.append(target)
                body = files.get(target, b"")
                if target.split("/")[1] in unavailable:
                    status = "403 Forbidden"
                    body = b""
                else:
                    status = "200 OK" if body else "404 Not Found"
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n".encode("ascii")
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, limit=8192)
    async with server:
        port = int(server.sockets[0].getsockname()[1])
        address = f"http://127.0.0.1:{port}"
        source = directory / "admitted.list"
        source.write_text(source_entry(address, "admitted", signer), encoding="ascii")
        ambient = directory / "ambient.list"
        ambient.write_text(
            source.read_text(encoding="ascii")
            + source_entry(address, "unrelated", signer),
            encoding="ascii",
        )
        await apt_cache_and_failures(
            directory, signing, source, ambient, files, requests, unavailable
        )


async def apt_cache_and_failures(
    directory: Path,
    signing: Sequence[str],
    source: Path,
    ambient: Path,
    files: dict[str, bytes],
    requests: list[str],
    unavailable: set[str],
) -> None:
    from scripts.ci.browser_ubuntu_apt import (
        apt_options,
        apt_selectors,
    )

    parts, lists, cold, archives, private, private_lists = (
        directory / name
        for name in ("parts", "lists", "cold", "archives", "private", "private-lists")
    )
    for child in (parts, lists, cold, archives, private, private_lists):
        child.mkdir()
    environment = apt_environment(directory, ambient, parts)
    shared = apt_selectors(ambient, parts, lists, archives)
    pkgcache, srcpkgcache = (directory / name for name in ("pkg.bin", "srcpkg.bin"))
    shared.update(
        {
            "Dir::Cache::pkgcache": str(pkgcache),
            "Dir::Cache::srcpkgcache": str(srcpkgcache),
        }
    )
    await native_success(
        directory, environment, "/usr/bin/apt-get", *apt_options(shared), "update"
    )
    await native_success(
        directory, environment, "/usr/bin/apt-cache", *apt_options(shared), "gencaches"
    )
    warm_caches = (pkgcache.read_bytes(), srcpkgcache.read_bytes())
    warm_lists = {
        path.name: path.read_bytes() for path in lists.iterdir() if path.is_file()
    }
    policy = await native_success(
        directory,
        environment,
        "/usr/bin/apt-cache",
        *apt_options(shared),
        "policy",
        APT_PACKAGE,
    )
    assert b"Candidate: 2.0" in policy

    unavailable.add("unrelated")
    requests.clear()
    ambient_cold = apt_options(apt_selectors(ambient, parts, cold, archives))
    code, errors = await native_command(
        directory, environment, "/usr/bin/apt-get", *ambient_cold, "update"
    )
    assert code == 100, errors
    assert b"403" in errors
    assert f"/unrelated/{APT_RELEASE}" in requests

    private_selectors = apt_selectors(source, parts, private_lists, private)
    options = apt_options(private_selectors)
    await check_native_profile(directory, ambient, private_selectors)
    requests.clear()
    expected_archive = files[f"/admitted/{APT_PACKAGE}_1.0_all.deb"]
    ambient_archive = archives / f"{APT_PACKAGE}_1.0_all.deb"
    ambient_bytes = bytes(len(expected_archive))
    ambient_archive.write_bytes(ambient_bytes)
    policy = await native_success(
        directory,
        environment,
        "/usr/bin/apt-cache",
        *apt_options(apt_selectors(source, parts, lists, private)),
        "policy",
        APT_PACKAGE,
    )
    assert b"Candidate: 1.0" in policy
    hook = directory / "update-hook"
    hook.unlink()
    await native_success(directory, environment, "/usr/bin/apt-get", *options, "update")
    assert hook.is_file()
    await native_success(
        directory,
        environment,
        "/usr/bin/apt-get",
        *options,
        "--download-only",
        "-y",
        "--no-install-recommends",
        "install",
        APT_PACKAGE,
    )
    assert (private / ambient_archive.name).read_bytes() == expected_archive
    assert ambient_archive.read_bytes() == ambient_bytes
    assert (pkgcache.read_bytes(), srcpkgcache.read_bytes()) == warm_caches
    assert {
        path.name: path.read_bytes() for path in lists.iterdir() if path.is_file()
    } == warm_lists
    assert f"/admitted/{ambient_archive.name}" in requests
    assert not any(request.startswith("/unrelated/") for request in requests)

    rejected = directory / "rejected"
    rejected.mkdir()
    options = apt_options(apt_selectors(source, parts, rejected, private))
    unavailable.add("admitted")
    code, errors = await native_command(
        directory, environment, "/usr/bin/apt-get", *options, "update"
    )
    assert code == 100, errors
    assert b"403" in errors
    unavailable.remove("admitted")

    files[f"/admitted/{APT_RELEASE}"] = files[f"/admitted/{APT_RELEASE}"].replace(
        b"Codename: noble", b"Codename: other"
    )
    code, errors = await native_command(
        directory, environment, "/usr/bin/apt-get", *options, "update"
    )
    assert code == 100, errors
    assert b"BAD" in errors, errors

    files[f"/admitted/{APT_RELEASE}"] = await sign_release(
        directory,
        signing,
        files[f"/admitted/{APT_PACKAGE_INDEX}"],
        datetime.now(UTC) - timedelta(hours=1),
    )
    code, errors = await native_command(
        directory, environment, "/usr/bin/apt-get", *options, "update"
    )
    assert code == 100, errors
    assert b"expired" in errors, errors


async def check_native_apt_source_selection() -> None:
    with tempfile.TemporaryDirectory(
        prefix="kora-browser-apt-", dir=os.environ["RUNNER_TEMP"]
    ) as name:
        directory = Path(name)
        signing = directory / "signing"
        signing.mkdir(mode=0o700)
        gpg = (
            "/usr/bin/gpg",
            "--homedir",
            str(signing),
            "--batch",
            "--yes",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
        )
        try:
            await native_success(
                directory,
                APT_ENV,
                *gpg,
                "--quick-generate-key",
                "kora browser APT witness",
                "ed25519",
                "sign",
                "0",
            )
            await apt_source_selection(directory, gpg)
        finally:
            await native_success(
                directory,
                APT_ENV,
                "/usr/bin/gpgconf",
                "--homedir",
                str(signing),
                "--kill",
                "gpg-agent",
            )
    sys.stdout.write("Native APT selected only the admitted signed archive.\n")


async def configure_managed(administrative: Path) -> int:
    code, errors = await configure(administrative)
    if code != 0:
        sys.stderr.write(f"Private dpkg probe returned {dpkg_outcome(code, errors)}.\n")
    return code


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--abandon":
        asyncio.run(start_unowned_controller(Path(sys.argv[2]), sys.argv[3]))
        return 0
    if len(sys.argv) == 4 and sys.argv[2] == "hold":
        hold(Path(sys.argv[1]), Path(sys.argv[3]))
        return 2
    if len(sys.argv) == 3:
        administrative = Path(sys.argv[1])
        if sys.argv[2] == "hold":
            hold(administrative)
            return 2
        if sys.argv[2] == "configure":
            return asyncio.run(configure_managed(administrative))
        return 2
    asyncio.run(check_lifecycle())
    asyncio.run(check_playwright_adapter_refusal())
    asyncio.run(check_native_apt_source_selection())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
