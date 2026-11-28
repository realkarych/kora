import asyncio
import fcntl
import json
import os
import pwd
import selectors
import shutil
import ssl
import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest

from kora.db.connection import Database
from kora.db.run_event_maintenance_admission import (
    CONTROL_LOCK,
    PREDECESSOR_REVISION,
    PROFILE_PATH,
    RELEASE_EVIDENCE,
)
from kora.db.run_event_position_v1 import POSITION_REVISION
from kora.models.run_events import RunEvent, RunEventPage
from tests.api.test_packaging import (
    TLS_EVENTS_NAMESPACE,
    api_environment,
    diagnostics,
    https_environment,
    installed_core,
    read_bound_port,
    refusal_reason,
    seed_installed_run_events,
    terminate_installed_api,
    unused_loopback_port,
    write_tls_material,
)
from tests.db.predecessor import PredecessorInstallation
from tests.db.run_event_position_claim_witness import (
    issue_installed_claim,
    replay_installed_claim,
)
from tests.support.databases import sqlite_settings

PROCESS_SECONDS = 20
LATE_EVENT_ID = "0" * 26
ROLLED_BACK_EVENT_ID = "0" * 25 + "3"
LIVE_WRITER = """
import asyncio
from sqlalchemy import text
from kora.db.connection import Database, parse_database_url
import os
import sys

async def hold():
    database = Database(parse_database_url(os.environ["KORA_DATABASE_URL"]))
    idle_reader = sys.argv[1] == "idle"
    try:
        unit = database.read_transaction() if idle_reader else database.transaction()
        async with unit as connection:
            if idle_reader:
                (await connection.execute(text("SELECT id FROM event LIMIT 1"))).close()
            else:
                (await connection.execute(text(
                    "INSERT INTO event (id, namespace_id, run_id, node_instance_id, "
                    "attempt_id, kind, actor, payload, at, created_at, updated_at) "
                    "SELECT :late, namespace_id, run_id, node_instance_id, "
                    "attempt_id, kind, actor, payload, at, created_at, updated_at "
                    "FROM event WHERE id = :earlier"
                ), {"late": sys.argv[1], "earlier": sys.argv[2]})).close()
            sys.stdout.write("ready\\n")
            sys.stdout.flush()
            if sys.stdin.readline(16) != "commit\\n":
                raise ValueError("The original transaction was cancelled.")
            if idle_reader:
                (await connection.execute(text("SELECT id FROM event LIMIT 1"))).close()
    finally:
        await database.close()

asyncio.run(hold())
"""
ARTIFACT_ID = """
from hashlib import sha256
from importlib.metadata import distribution
import sys

record = distribution("kora").read_text("RECORD")
assert record is not None
sys.stdout.write(sha256(record.encode()).hexdigest())
"""
DENY_RELEASE_WRITE = """
import sys
from pathlib import Path

release = Path(sys.argv[1])
for candidate in (release, release.with_name("replacement.json")):
    try:
        with candidate.open("ab"):
            pass
    except PermissionError:
        continue
    raise ValueError("The successor can write operator evidence.")
"""


@dataclass(frozen=True)
class InstalledProcess:
    python: Path
    uid: int
    gid: int

    def command(self, *arguments: str) -> tuple[str, ...]:
        return (str(self.python), "-I", "-m", "kora", *arguments)

    def call(
        self, arguments: Sequence[str], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            (str(self.python), "-I", *arguments),
            env=environment,
            user=self.uid,
            group=self.gid,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=PROCESS_SECONDS,
        )

    def artifact(self) -> str:
        result = self.call(("-c", ARTIFACT_ID), api_environment(None))
        assert result.returncode == 0, result.stderr
        value = result.stdout.decode()
        assert len(value) == 64
        return value


@contextmanager
def disposable_installation() -> Generator[Path]:
    if os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("The witness requires its disposable hosted owner.")
    parent = Path("/private/var") if sys.platform == "darwin" else Path("/var/lib")
    with TemporaryDirectory(prefix="kora-position-witness-", dir=parent) as directory:
        root = Path(directory)
        root.chmod(0o755)
        profile_directory = PROFILE_PATH.parent
        profile_directory.mkdir(mode=0o755)
        try:
            yield root
        finally:
            PROFILE_PATH.unlink(missing_ok=True)
            profile_directory.rmdir()


def copy_installation(python: Path, destination: Path) -> Path:
    assert python.parent.name == "bin"
    shutil.copytree(python.parent.parent, destination, symlinks=True)
    staged = destination / "bin" / python.name
    assert staged.is_file()
    return staged


def write_evidence(path: Path, value: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o644)
    directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def db_environment(path: Path) -> dict[str, str]:
    return api_environment(str(sqlite_settings(path).url))


@dataclass(frozen=True)
class DatabaseFile:
    name: str
    digest: bytes
    identity: tuple[int, ...]


def database_files(path: Path) -> tuple[DatabaseFile, ...]:
    observed: list[DatabaseFile] = []
    for file in sorted(path.parent.iterdir()):
        if file.name == path.name or file.name.startswith(path.name + "-"):
            status = file.stat()
            identity = (
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_uid,
                status.st_gid,
                status.st_ctime_ns,
            )
            observed.append(
                DatabaseFile(file.name, sha256(file.read_bytes()).digest(), identity)
            )
    return tuple(observed)


@contextmanager
def original_owner(
    owner: InstalledProcess, path: Path, earlier_id: str, event_id: str
) -> Generator[subprocess.Popen[bytes]]:
    with subprocess.Popen(
        (str(owner.python), "-I", "-c", LIVE_WRITER, event_id, earlier_id),
        env=db_environment(path),
        user=owner.uid,
        group=owner.gid,
        extra_groups=[],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                assert ready.select(PROCESS_SECONDS)
                status = process.stdout.readline(16)
            if status != b"ready\n":
                _, errors = process.communicate(timeout=PROCESS_SECONDS)
                raise AssertionError(errors.decode(errors="replace"))
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=PROCESS_SECONDS)


async def read_https_events(
    owner: InstalledProcess,
    path: Path,
    page: tuple[RunEvent, ...],
    credential: str,
    tls: Path,
) -> None:
    material = write_tls_material(tls)
    material.key.chmod(0o600)
    for file in (material.certificate, material.key):
        os.chown(file, owner.uid, owner.gid)
    assert material.key.stat().st_mode & 0o077 == 0
    port = unused_loopback_port()
    profile = https_environment(material, port)
    process = await asyncio.create_subprocess_exec(
        *owner.command("api", "--port", str(port)),
        env={**db_environment(path), **profile},
        user=owner.uid,
        group=owner.gid,
        extra_groups=[],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        actual_port, early = await read_bound_port(process)
        assert actual_port == port, early
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust.load_verify_locations(material.authority)
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{port}",
            verify=trust,
            trust_env=False,
            timeout=PROCESS_SECONDS,
        ) as client:
            result = await client.get(
                f"/api/v1/namespaces/{TLS_EVENTS_NAMESPACE}/runs/{page[0].run_id}/events",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert result.status_code == 200
            observed = RunEventPage.model_validate_json(result.content)
            assert observed.items == page
            assert observed.next_cursor is None
    finally:
        stdout, stderr = await terminate_installed_api(process)
    assert process.returncode == 0, stderr
    assert stdout == b""


def require_exit(result: subprocess.CompletedProcess[bytes], code: int) -> bytes:
    assert result.returncode == code, result.stderr
    return result.stdout


def require_maintenance(
    result: subprocess.CompletedProcess[bytes], expected: str, *private: str
) -> None:
    stdout = require_exit(result, 0 if expected == "prepared" else 78)
    assert stdout == (
        POSITION_REVISION.encode() + b"\n" if expected == "prepared" else b""
    )
    assert [
        item["outcome"]
        for item in diagnostics(result.stderr)
        if item["event"] == "db.run_event_positions.maintenance"
    ] == [expected]
    assert all(value.encode() not in result.stderr for value in private)


def transfer_directory(directory: Path, path: Path, owner: InstalledProcess) -> None:
    os.chown(directory, owner.uid, owner.gid)
    os.chown(path, owner.uid, owner.gid)
    directory.chmod(0o700)
    path.chmod(0o600)


def restore_binding(image: bytes, history: tuple[RunEvent, ...]) -> dict[str, str]:
    return {
        "verified_image": image.hex(),
        "isolated_verification": sha256(
            json.dumps([item.id for item in history]).encode()
        ).hexdigest(),
    }


async def assert_incompatible_predecessor_refused(
    control: Path,
    backup: Path,
    operator: InstalledProcess,
    prepare: tuple[str, ...],
    proof: dict[str, object],
    old_uid: int,
) -> None:
    alternate = backup.parent.parent / "incompatible"
    alternate.mkdir(mode=0o700)
    target = alternate / backup.name
    shutil.copy2(backup, target)
    legacy = Database(sqlite_settings(target))
    try:
        async with legacy.transaction() as connection:
            (
                await connection.exec_driver_sql(
                    "ALTER TABLE run RENAME COLUMN namespace_id TO old_namespace_id"
                )
            ).close()
    finally:
        await legacy.close()
    before = database_files(target)
    status = target.stat()
    alternate_proof = control / "incompatible.json"
    write_evidence(
        alternate_proof,
        {
            **proof,
            "database": {
                "backend": "sqlite",
                "path": str(target),
                "device": status.st_dev,
                "inode": status.st_ino,
                "old_uid": old_uid,
            },
        },
    )
    require_maintenance(
        operator.call((*prepare[:-1], str(alternate_proof)), db_environment(target)),
        "refused",
        str(target),
        str(alternate_proof),
    )
    assert database_files(target) == before


async def managed_transition(
    root: Path,
    original: InstalledProcess,
    successor: InstalledProcess,
) -> None:
    live = root / "live"
    live.mkdir(mode=0o700)
    database = live / "metadata.db"
    original_env = db_environment(database)
    upgrade = ("-m", "kora", "db", "upgrade")
    original_setup = InstalledProcess(original.python, 0, 0)
    assert require_exit(original_setup.call(upgrade, original_env), 0) == (
        PREDECESSOR_REVISION.encode() + b"\n"
    )
    transfer_directory(live, database, original)
    claim = await issue_installed_claim(
        original.python, original.uid, original.gid, database, root
    )
    credential, existing = await seed_installed_run_events(sqlite_settings(database))
    tls = root / "tls"
    tls.mkdir(mode=0o755)
    await read_https_events(original, database, existing, credential, tls)

    control = root / "control"
    control.mkdir(mode=0o755)
    lock = control / CONTROL_LOCK
    lock.touch(mode=0o600, exist_ok=False)
    control_stat = control.stat()
    installation = uuid4().hex
    write_evidence(
        PROFILE_PATH,
        {
            "format": "kora.run-event-maintenance-profile/1",
            "installation": installation,
            "control_directory": str(control),
            "control_device": control_stat.st_dev,
            "control_inode": control_stat.st_ino,
            "operator_uid": 0,
            "successor_uid": successor.uid,
        },
    )

    operator = InstalledProcess(successor.python, 0, 0)
    proof_file = control / "hold.json"
    prepare = (
        "-m",
        "kora",
        "db",
        "prepare-run-event-positions",
        "--proof",
        str(proof_file),
    )
    copied_event = existing[0].id.removeprefix("event_")
    with original_owner(
        original, database, copied_event, ROLLED_BACK_EVENT_ID
    ) as denied:
        stdout, stderr = denied.communicate(b"rollback\n", timeout=PROCESS_SECONDS)
        assert denied.returncode == 1, stderr
        assert stdout == b""
    await read_https_events(original, database, existing, credential, tls)

    main = database.stat()
    database_identity: dict[str, object] = {
        "backend": "sqlite",
        "path": str(database),
        "device": main.st_dev,
        "inode": main.st_ino,
        "old_uid": original.uid,
    }
    proof: dict[str, object] = {
        "format": "kora.run-event-maintenance-proof/1",
        "operation": uuid4().hex,
        "phase": "held",
        "installation": installation,
        "fence_generation": uuid4().hex,
        "predecessor": PREDECESSOR_REVISION,
        "successor": POSITION_REVISION,
        "predecessor_artifact": original.artifact(),
        "successor_artifact": successor.artifact(),
        "original_cohort": sha256(
            f"{original.uid}:{original.gid}".encode()
        ).hexdigest(),
        "database": database_identity,
    }
    premature = control / "unsettled.json"
    preceding = database_files(database)
    assert tuple(file.name for file in preceding) == (database.name,)
    write_evidence(
        premature, {**proof, "restore": restore_binding(preceding[0].digest, existing)}
    )
    premature_command = (*prepare[:-1], str(premature))
    with original_owner(original, database, copied_event, "idle") as idle:
        opened = database_files(database)
        require_maintenance(
            operator.call(premature_command, original_env),
            "refused",
            str(database),
            str(premature),
        )
        assert database_files(database) == opened
        stdout, stderr = idle.communicate(b"commit\n", timeout=PROCESS_SECONDS)
        assert idle.returncode == 0, stderr
        assert stdout == b""

    with original_owner(original, database, copied_event, LATE_EVENT_ID) as writer:
        held = database_files(database)
        require_exit(operator.call(upgrade, original_env), 78)
        require_maintenance(
            operator.call(prepare, original_env),
            "refused",
            str(database),
            str(proof_file),
        )
        require_maintenance(
            operator.call(premature_command, original_env),
            "refused",
            str(database),
            str(premature),
        )
        assert database_files(database) == held
        stdout, stderr = writer.communicate(b"commit\n", timeout=PROCESS_SECONDS)
        assert writer.returncode == 0, stderr
        assert stdout == b""

    late = existing[0].model_copy(update={"id": f"event_{LATE_EVENT_ID}"})
    final_page = (*existing, late)
    files = database_files(database)
    assert tuple(file.name for file in files) == (database.name,)
    retained = root / "restore"
    retained.mkdir(mode=0o700)
    backup = retained / database.name
    shutil.copy2(database, backup)
    assert sha256(backup.read_bytes()).digest() == files[0].digest
    isolated = root / "isolated-restore"
    isolated.mkdir(mode=0o700)
    restored = isolated / database.name
    shutil.copy2(backup, restored)
    transfer_directory(isolated, restored, original)
    await read_https_events(original, restored, final_page, credential, tls)
    await replay_installed_claim(
        original.python, original.uid, original.gid, restored, claim
    )
    with original_owner(original, restored, copied_event, "idle") as retained_owner:
        transfer_directory(isolated, restored, operator)
        forbidden = original.call(upgrade, db_environment(restored))
        assert forbidden.returncode != 0
        assert forbidden.stdout == b""
        stdout, stderr = retained_owner.communicate(
            b"commit\n", timeout=PROCESS_SECONDS
        )
        assert retained_owner.returncode == 0, stderr
        assert stdout == b""
    assert sha256(backup.read_bytes()).digest() == files[0].digest

    transfer_directory(live, database, operator)
    fenced_files = database_files(database)
    assert tuple(file.digest for file in fenced_files) == (files[0].digest,)
    blocked_old = original.call(upgrade, original_env)
    assert blocked_old.returncode != 0
    assert blocked_old.stdout == b""
    assert database_files(database) == fenced_files
    proof["restore"] = restore_binding(files[0].digest, final_page)
    write_evidence(proof_file, proof)
    require_exit(operator.call(upgrade, original_env), 78)
    shared_principal = control / "shared-principal.json"
    write_evidence(
        shared_principal,
        {**proof, "database": {**database_identity, "old_uid": successor.uid}},
    )
    require_exit(
        operator.call((*prepare[:-1], str(shared_principal)), original_env), 78
    )
    assert database_files(database) == fenced_files
    await assert_incompatible_predecessor_refused(
        control, backup, operator, prepare, proof, original.uid
    )
    for _ in range(2):
        require_maintenance(
            operator.call(prepare, original_env),
            "prepared",
            str(database),
            str(proof_file),
            str(proof["successor_artifact"]),
        )
    transfer_directory(live, database, successor)
    port = unused_loopback_port()
    staged_files = database_files(database)
    staged = successor.call(("-m", "kora", "api", "--port", str(port)), original_env)
    require_exit(staged, 78)
    assert refusal_reason(staged)
    assert database_files(database) == staged_files

    released = {**proof, "phase": "released"}
    with lock.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_evidence(control / RELEASE_EVIDENCE, released)
    require_exit(
        successor.call(
            ("-c", DENY_RELEASE_WRITE, str(control / RELEASE_EVIDENCE)), original_env
        ),
        0,
    )
    require_maintenance(
        operator.call(prepare, original_env), "refused", str(database), str(proof_file)
    )
    await read_https_events(successor, database, final_page, credential, tls)
    await replay_installed_claim(
        successor.python, successor.uid, successor.gid, database, claim, positioned=True
    )


def main(arguments: Sequence[str]) -> None:
    predecessor_path, successor_path, successor_uid, successor_gid = arguments
    old = pwd.getpwnam("nobody")
    uid, gid = int(successor_uid), int(successor_gid)
    assert uid not in (0, old.pw_uid)
    with disposable_installation() as root:
        original = InstalledProcess(
            copy_installation(Path(predecessor_path), root / "predecessor"),
            old.pw_uid,
            old.pw_gid,
        )
        successor = InstalledProcess(
            copy_installation(Path(successor_path), root / "successor"), uid, gid
        )
        asyncio.run(managed_transition(root, original, successor))


def test_installed_sqlite_managed_transition(
    core_wheel: Path,
    predecessor_installation: PredecessorInstallation,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    staging = tmp_path_factory.mktemp("kora-managed-position-artifacts")
    original = staging / "original"
    successor = staging / "successor"
    original.mkdir()
    successor.mkdir()
    original_python = installed_core(
        predecessor_installation.wheel, original, serve=True
    )
    successor_python = installed_core(core_wheel, successor, serve=True)
    completed = subprocess.run(
        (
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/env",
            "GITHUB_ACTIONS=true",
            sys.executable,
            "-m",
            "tests.db.run_event_maintenance_installed_fixture",
            str(original_python),
            str(successor_python),
            str(os.getuid()),
            str(os.getgid()),
        ),
        check=False,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""


if __name__ == "__main__":
    main(sys.argv[1:])
