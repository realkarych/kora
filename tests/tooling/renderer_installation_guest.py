import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from functools import partial
from io import FileIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import BuiltinMethodType, FrameType, TracebackType
from typing import Never, cast

ANCHOR = Path("/run/kora-preparation")
CHECKER = "/run/kora-preparer/scripts/release/verify_renderer_installation.py"
COMPARATOR = "/run/kora-preparer/scripts/release/renderer_installation_check.py"
COMPARATOR_ARGUMENTS = ("/usr/bin/python3", "-I", "-S", "-B", COMPARATOR)
BUILD_SAMPLES = Path("/run/kora-preparer/fixture-inputs")
ENTRY_MARKER = Path("/run/kora-renderer-fixture/probe/entry-ran")
OWNER_LOSS_CHANNEL = "KORA_RENDERER_FIXTURE_OWNER_CHANNEL"
OWNER_LOSS_READY_BYTES = 9
RETAINED_BUSY_CHANNEL = "KORA_RENDERER_FIXTURE_BUSY_CHANNEL"
RETAINED_BUSY_PARK_BYTES = 25
LATE_ATTACHMENT_CHANNEL = "KORA_RENDERER_FIXTURE_LATE_ATTACHMENT_CHANNEL"
LATE_CHILD_CHANNEL = "KORA_RENDERER_FIXTURE_LATE_CHILD_CHANNEL"
LATE_CHILD_READY = struct.Struct("!cIQ")
OBSERVATION_MARGIN_NS = 2_000_000_000
FINAL_READBACK_CHANNEL = "KORA_RENDERER_FIXTURE_FINAL_READBACK_CHANNEL"
MOUNT_TOPOLOGY_CHANNEL = "KORA_RENDERER_FIXTURE_MOUNT_TOPOLOGY_CHANNEL"
BOOT_IMAGE = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
BOOT_KERNEL = "41ba36d860aa0b0cd995f4ca8856d80249b244577e704929b55be13fbd1ebd2f"
BOOT_INITRD = "bf276ffc12872256080890698b42db0dc9c5a469c896d8b59af6bc4e723cd186"
BASE = {
    "release": "ubuntu-24.04-amd64-20261017",
    "image_sha256": BOOT_IMAGE,
    "kernel_release": "6.8.0-147-generic",
    "kernel_sha256": BOOT_KERNEL,
    "initrd_sha256": BOOT_INITRD,
}
INPUTS = {
    "cpython_sha256": (
        "3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522"
    ),
    "jinja_sha256": "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67",
    "markupsafe_sha256": (
        "6da83a088f8ef93b2d483a8232a4dbf4d69d3d8496b568a03c56becac43e1808"
    ),
}
ENV = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
INVENTORY_DOMAIN = b"kora.renderer-root-inventory/1\0"
BLOCK_SIZE = 4096
DATA_BLOCKS = 2048
PROBE_FILE = "std/probe.bin"
MAPPERS = {"kora-renderer-verifier", "kora-renderer-candidate", "kora-1817-io"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
INOTIFY_ACCESS = 0x01
INOTIFY_OPEN = 0x20
INOTIFY_EVENT = struct.Struct("=iIII")


@dataclass(frozen=True)
class Volume:
    role: str
    data: Path
    hashes: Path
    inventory: bytes
    inventory_digest: str
    verity_root: str
    salt: str

    def root_record(self) -> dict[str, object]:
        return {
            "inventory_sha256": self.inventory_digest,
            "entry": "entry.py",
            "interpreter": "bin/python",
            "elf_loader": "lib/ld.so",
            "stdlib": "std",
            "verity": {
                "data_blocks": DATA_BLOCKS,
                "root_hash": self.verity_root,
                "salt": self.salt,
            },
        }


@dataclass(frozen=True)
class FixtureMapping:
    name: str
    uuid: str
    path: str
    table: str

    def check_live(self) -> None:
        assert command("/usr/sbin/dmsetup", "table", self.name).rstrip() == (
            f"0 {DATA_BLOCKS * 8} verity {self.table}".encode()
        )
        assert command("/usr/sbin/dmsetup", "status", self.name).rstrip() == (
            f"0 {DATA_BLOCKS * 8} verity V".encode()
        )
        assert (
            command(
                "/usr/sbin/dmsetup",
                "info",
                "--noheadings",
                "-c",
                "-o",
                "uuid",
                self.name,
            ).strip()
            == self.uuid.encode()
        )


def command(*argv: str) -> bytes:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=ENV,
        cwd="/",
        check=True,
        timeout=8,
    ).stdout


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def require_guest() -> None:
    current = os.uname()
    assert os.getuid() == os.geteuid() == 0
    assert current.machine == "x86_64"
    assert current.release == BASE["kernel_release"]
    namespace = os.stat("/proc/self/ns/mnt")
    initial_namespace = os.stat("/proc/1/ns/mnt")
    assert (namespace.st_dev, namespace.st_ino) != (
        initial_namespace.st_dev,
        initial_namespace.st_ino,
    )
    assert command("/usr/bin/dpkg", "--print-architecture") == b"amd64\n"
    assert {"ID=ubuntu", 'VERSION_ID="24.04"'} <= set(
        Path("/etc/os-release").read_text(encoding="ascii").splitlines()
    )
    versions = command(
        "/usr/bin/dpkg-query",
        "-W",
        "-f=${binary:Package} ${Version}\n",
        "python3",
        "util-linux",
        "cryptsetup-bin",
        "dmsetup",
        "e2fsprogs",
    ).splitlines()
    assert set(versions) == {
        b"python3 3.12.3-0ubuntu2.1",
        b"util-linux 2.39.3-9ubuntu6.6",
        b"cryptsetup-bin 2:2.7.0-1ubuntu4.2",
        b"dmsetup 2:1.02.185-3ubuntu3.2",
        b"e2fsprogs 1.47.0-2.4~exp1ubuntu4.1",
    }
    anchor_mounts = [
        line.split(" - ", 1)[0].split()
        for line in Path("/proc/self/mountinfo")
        .read_text(encoding="ascii")
        .splitlines()
        if line.split()[4] in {"/", "/run"}
    ]
    assert any(fields[4] == "/" for fields in anchor_mounts)
    assert not any(
        field.startswith(("shared:", "master:", "propagate_from:"))
        for fields in anchor_mounts
        for field in fields[6:]
    )
    assert not ENTRY_MARKER.exists()
    command("/usr/sbin/modprobe", "loop")
    command("/usr/sbin/modprobe", "dm_verity")


def resource_snapshot() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    roots = str(ANCHOR / "roots")
    mounts = frozenset(
        fields[4]
        for line in Path("/proc/self/mountinfo")
        .read_text(encoding="ascii")
        .splitlines()
        if (fields := line.split())[4] == roots or fields[4].startswith(roots + "/")
    )
    mappings = frozenset(
        name
        for device in Path("/sys/class/block").glob("dm-*")
        if (name := (device / "dm/name").read_text(encoding="ascii").strip()) in MAPPERS
    )
    media = str(ANCHOR / "media") + "/"
    loops = frozenset(
        value
        for backing in Path("/sys/class/block").glob("loop*/loop/backing_file")
        if (value := backing.read_text(encoding="ascii").strip()).startswith(media)
    )
    return mounts, mappings, loops


def fixture_files(role: str) -> dict[str, bytes]:
    interpreter = (BUILD_SAMPLES / "python3.11").read_bytes()
    loader = Path("/lib64/ld-linux-x86-64.so.2").read_bytes()
    extension = (BUILD_SAMPLES / "markupsafe.so").read_bytes()
    assert all(data.startswith(b"\x7fELF") for data in (interpreter, loader, extension))
    return {
        "bin/python": interpreter,
        "entry.py": (
            f"from pathlib import Path\nPath({str(ENTRY_MARKER)!r}).touch()\n"
            f"raise SystemExit({role!r})\n"
        ).encode("ascii"),
        "lib/ld.so": loader,
        "packages/jinja2/runtime.py": (BUILD_SAMPLES / "jinja-runtime.py").read_bytes(),
        "packages/markupsafe/_speedups.so": extension,
        PROBE_FILE: probe_bytes(role),
        "std/pathlib.py": (BUILD_SAMPLES / "pathlib.py").read_bytes(),
    }


def probe_bytes(role: str) -> bytes:
    return (b"v" if role == "verifier" else b"c") * BLOCK_SIZE


def make_volume(
    role: str,
    *,
    extra: bool = False,
    filesystem_block: int = BLOCK_SIZE,
    journal: bool = False,
    unreadable: bool = False,
) -> Volume:
    files = fixture_files(role)
    inventory = canonical(
        [
            {
                "path": path,
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
            for path, body in sorted(files.items())
        ]
    )
    inventory_digest = hashlib.sha256(INVENTORY_DOMAIN + inventory).hexdigest()
    staging = ANCHOR / f"stage-{role}"
    staging.mkdir(exist_ok=True)
    if extra:
        files["unlisted.py"] = b"extra fixture file\n"
    for path, body in files.items():
        target = staging / path
        target.parent.mkdir(exist_ok=True, parents=True)
        for parent in target.parents:
            parent.chmod(0o755)
            if parent == staging:
                break
        target.write_bytes(body)
        target.chmod(0o400 if unreadable and path == PROBE_FILE else 0o444)

    data = ANCHOR / "media" / f"{role}.data"
    hashes = ANCHOR / "media" / f"{role}.hash"
    with data.open("wb") as image:
        image.truncate(DATA_BLOCKS * BLOCK_SIZE)
    with hashes.open("wb") as image:
        image.truncate(1024 * 1024)
    filesystem_blocks = DATA_BLOCKS * BLOCK_SIZE // filesystem_block
    journal_feature = "has_journal" if journal else "^has_journal"
    command(
        "/usr/sbin/mke2fs",
        "-q",
        "-F",
        "-t",
        "ext4",
        "-b",
        str(filesystem_block),
        "-O",
        journal_feature,
        "-d",
        str(staging),
        str(data),
        str(filesystem_blocks),
    )
    command("/usr/sbin/debugfs", "-w", "-R", "rmdir /lost+found", str(data))
    with data.open("rb") as image:
        superblock = os.pread(image.fileno(), 1024, 1024)
    assert superblock[56:58] == b"\x53\xef"
    assert 1024 << int.from_bytes(superblock[24:28], "little") == filesystem_block
    assert bool(int.from_bytes(superblock[92:96], "little") & 4) == journal
    salt = ("a1" if role == "verifier" else "b2") * 32
    formatted = command(
        "/usr/sbin/veritysetup",
        "format",
        "--no-superblock",
        "--format=1",
        "--hash=sha256",
        "--data-block-size=4096",
        "--hash-block-size=4096",
        "--data-blocks=2048",
        "--hash-offset=0",
        "--salt",
        salt,
        str(data),
        str(hashes),
    )
    roots = re.findall(rb"^Root hash:\s+([0-9a-f]{64})$", formatted, re.MULTILINE)
    assert len(roots) == 1
    data.chmod(0o400)
    hashes.chmod(0o400)
    return Volume(
        role, data, hashes, inventory, inventory_digest, roots[0].decode("ascii"), salt
    )


def provision(verifier: Volume, candidate: Volume) -> str:
    registry = ANCHOR / "registry"
    for volume in (verifier, candidate):
        (registry / "inventories" / f"{volume.inventory_digest}.json").write_bytes(
            volume.inventory
        )
    manifest = canonical(
        {
            "revision": 1,
            "base": BASE,
            "inputs": INPUTS,
            "verifier": verifier.root_record(),
            "candidate": candidate.root_record(),
        }
    )
    selector = hashlib.sha256(manifest).hexdigest()
    (registry / "deployments" / f"{selector}.json").write_bytes(manifest)
    return selector


def check_cli(
    label: str,
    selector: str,
    *,
    pin: str,
    match: bool = False,
    initrd: str = BOOT_INITRD,
    failed_sink: int | None = None,
    refusal: str | None = None,
) -> bytes | None:
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    env = {
        "LC_ALL": "C",
        "KORA_PREPARATION_SELECTOR": pin,
        "KORA_PREPARATION_IMAGE_SHA256": BOOT_IMAGE,
        "KORA_PREPARATION_KERNEL_SHA256": BOOT_KERNEL,
        "KORA_PREPARATION_INITRD_SHA256": initrd,
    }
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", CHECKER, selector],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=failed_sink if failed_sink is not None else subprocess.PIPE,
        env=env,
        cwd="/",
        check=False,
        timeout=20,
    )
    assert not ENTRY_MARKER.exists()
    assert resource_snapshot() == before
    if match:
        assert result.returncode == 0
        assert result.stdout == f"static_match {selector}\n".encode()
    else:
        assert result.returncode == 1
        assert re.fullmatch(rb"static_refusal [a-z_]+\n", result.stdout)
        if refusal is not None:
            assert result.stdout == f"static_refusal {refusal}\n".encode()
    sys.stdout.write(f"KORA_RENDERER_FIXTURE_CASE {label} settled=1\n")
    sys.stdout.flush()
    return result.stderr


def corrupt_probe(volume: Volume) -> None:
    output = command(
        "/usr/sbin/debugfs", "-R", f"bmap /{PROBE_FILE} 0", str(volume.data)
    )
    block = int(output)
    assert 0 < block < DATA_BLOCKS
    volume.data.chmod(0o600)
    with volume.data.open("r+b", buffering=0) as image:
        offset = block * BLOCK_SIZE
        assert os.pread(image.fileno(), BLOCK_SIZE, offset) == probe_bytes(volume.role)
        assert os.pwrite(image.fileno(), b"x", offset) == 1
        os.fsync(image.fileno())
    volume.data.chmod(0o400)


def attach_fixture_loop(path: Path) -> str:
    loop = command("/usr/sbin/losetup", "--find", "--show", "--read-only", str(path))
    node = loop.decode("ascii").strip()
    assert re.fullmatch(r"/dev/loop[0-9]+", node)
    actual = Path("/sys/class/block", Path(node).name, "loop/backing_file")
    assert actual.read_text(encoding="ascii").strip() == str(path)
    return node


@contextmanager
def fixture_mapping(
    volume: Volume, name: str, uuid: str
) -> Generator[FixtureMapping, None, None]:
    data_node = attach_fixture_loop(volume.data)
    hash_node = attach_fixture_loop(volume.hashes)
    backing = tuple(
        Path("/sys/class/block", Path(node).name, "dev")
        .read_text(encoding="ascii")
        .strip()
        for node in (data_node, hash_node)
    )
    table = (
        f"1 {backing[0]} {backing[1]} 4096 4096 "
        f"{DATA_BLOCKS} 0 sha256 {volume.verity_root} {volume.salt}"
    )
    assert not os.path.lexists(f"/dev/mapper/{name}")
    command(
        "/usr/sbin/dmsetup",
        "create",
        name,
        "--readonly",
        "--uuid",
        uuid,
        "--table",
        f"0 {DATA_BLOCKS * 8} verity {table}",
    )
    command("/usr/sbin/dmsetup", "mknodes", name)
    try:
        yield FixtureMapping(name, uuid, f"/dev/mapper/{name}", table)
    finally:
        command("/usr/sbin/dmsetup", "remove", "--uuid", uuid)
        assert name not in resource_snapshot()[1]
        command("/usr/sbin/losetup", "--detach", hash_node)
        command("/usr/sbin/losetup", "--detach", data_node)


@contextmanager
def mounted(path: str, mountpoint: Path) -> Generator[int, None, None]:
    mountpoint.mkdir(mode=0o700)
    command("/usr/bin/mount", "-t", "ext4", "-o", "ro,noload", path, str(mountpoint))
    try:
        root = os.open(mountpoint, DIRECTORY)
        try:
            yield root
        finally:
            os.close(root)
    finally:
        command("/usr/bin/umount", str(mountpoint))
        assert str(mountpoint) not in resource_snapshot()[0]
        mountpoint.rmdir()


def kernel_io_refusal(volume: Volume) -> None:
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    mountpoint = ANCHOR / "roots" / "io-probe"
    with (
        fixture_mapping(volume, "kora-1817-io", "KORA-1817-IO") as mapping,
        mounted(mapping.path, mountpoint),
    ):
        mapping.check_live()
        with (mountpoint / PROBE_FILE).open("rb", buffering=0) as probe:
            try:
                probe.read(BLOCK_SIZE)
            except OSError as failure:
                if failure.errno != errno.EIO:
                    raise
            else:
                raise AssertionError(
                    "owned dm-verity data corruption did not produce EIO"
                )
    assert resource_snapshot() == before
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE kernel_eio settled=1\n")
    sys.stdout.flush()


def namespace_identity(pid: int) -> tuple[int, int]:
    namespace = Path(f"/proc/{pid}/ns/mnt").stat()
    return namespace.st_dev, namespace.st_ino


def root_identity(pid: int, descriptor: int) -> tuple[int, str, int]:
    if sys.platform != "linux":
        raise RuntimeError("guest descriptor identity requires Linux")
    root = Path(f"/proc/{pid}/fd/{descriptor}").stat()
    information = Path(f"/proc/{pid}/fdinfo/{descriptor}").read_text("ascii")
    fields = dict(line.split(":", 1) for line in information.splitlines())
    assert int(fields["flags"].strip(), 8) & (os.O_ACCMODE | os.O_PATH) == os.O_RDONLY
    device = f"{os.major(root.st_dev)}:{os.minor(root.st_dev)}"
    return int(fields["mnt_id"].strip()), device, root.st_ino


def child_authority(pid: int, passed: tuple[int, ...]) -> None:
    proc = Path(f"/proc/{pid}")
    parent = os.getpid()
    status = dict(
        line.split(":", 1)
        for line in (proc / "status").read_text("ascii").splitlines()
        if ":" in line
    )
    assert status["Uid"].split() == ["65532"] * 4
    assert status["Gid"].split() == ["65532"] * 4
    assert not status["Groups"].split()
    assert int(status["PPid"]) == parent
    assert status["NoNewPrivs"].strip() == "1"
    for capability in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[capability].strip(), 16) == 0
    assert namespace_identity(pid) == namespace_identity(parent)
    for descriptor in passed[2:]:
        assert root_identity(pid, descriptor) == root_identity(parent, descriptor)


def child_held_roots(
    child: subprocess.Popen[bytes],
    passed: tuple[int, int, int, int],
) -> None:
    proc = Path(f"/proc/{child.pid}")
    deadline = time.monotonic() + 5
    command = b"\0".join(os.fsencode(argument) for argument in COMPARATOR_ARGUMENTS)
    expected = {"0", "1", "2", *(str(fd) for fd in passed)}
    while True:
        assert time.monotonic() < deadline, (
            "direct comparator did not reach inherited input"
        )
        assert child.poll() is None
        if (proc / "cmdline").read_bytes() == command + b"\0":
            descriptors = {fd.name for fd in (proc / "fd").iterdir()}
            if descriptors == expected:
                break
        time.sleep(0.01)

    child_authority(child.pid, passed)


def compare_inherited_roots(
    roots: tuple[int, int],
    request: bytes,
    *,
    match: bytes = b"",
    deadline_ns: int | None = None,
) -> None:
    with ExitStack() as channels:
        request_fd, send_fd = os.pipe()
        receive_fd, result_fd = os.pipe()
        receive = channels.enter_context(os.fdopen(receive_fd, "rb", buffering=0))
        sender = channels.enter_context(os.fdopen(send_fd, "wb", buffering=0))
        child_input = channels.enter_context(os.fdopen(request_fd, "rb", buffering=0))
        child_result = channels.enter_context(os.fdopen(result_fd, "wb", buffering=0))
        passed = (request_fd, result_fd, *roots)
        admission_deadline = (
            time.monotonic_ns() + 15_000_000_000 if deadline_ns is None else deadline_ns
        )
        env = {
            "LC_ALL": "C",
            "KORA_PREP_REQ": str(request_fd),
            "KORA_PREP_RESULT": str(result_fd),
            "KORA_PREP_VERIFIER": str(roots[0]),
            "KORA_PREP_CANDIDATE": str(roots[1]),
            "KORA_PREP_DEADLINE_NS": str(admission_deadline),
        }
        with subprocess.Popen(
            [
                "/usr/bin/setpriv",
                "--reuid=65532",
                "--regid=65532",
                "--clear-groups",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--bounding-set=-all",
                "--no-new-privs",
                *COMPARATOR_ARGUMENTS,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            close_fds=True,
            pass_fds=passed,
        ) as child:
            child_input.close()
            child_result.close()
            try:
                child_held_roots(child, passed)
                assert not select.select((receive,), (), (), 0)[0]
                assert sender.write(request) == len(request)
                sender.close()
                assert child.wait(timeout=15) == (0 if match else 1)
                assert receive.read(4097) == match
                assert receive.read(1) == b""
            finally:
                sender.close()
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
    assert not ENTRY_MARKER.exists()


def request_frame(
    job: str,
    selector: str,
    expected: tuple[tuple[int, str, int], tuple[int, str, int]],
    mappings: tuple[FixtureMapping, FixtureMapping],
    documents: tuple[bytes, bytes, bytes],
) -> bytes:
    payload = canonical(
        {
            "job": job,
            "selector": selector,
            "namespace": namespace_identity(os.getpid()),
            "roots": expected,
            "mappings": [
                [mapping.uuid, root[1], mapping.table, "V"]
                for root, mapping in zip(expected, mappings, strict=True)
            ],
        }
    )
    sizes = tuple(len(document) for document in documents)
    header = struct.pack("!5I", len(payload) + sum(sizes) + 16, len(payload), *sizes)
    return header + payload + b"".join(documents)


@contextmanager
def ordinary_ext4(volume: Volume) -> Generator[int, None, None]:
    copied = ANCHOR / "media" / "foreign.data"
    shutil.copyfile(volume.data, copied)
    device = attach_fixture_loop(copied)
    try:
        with mounted(device, ANCHOR / "roots" / "foreign") as descriptor:
            yield descriptor
    finally:
        command("/usr/sbin/losetup", "--detach", device)
        copied.unlink()


def expected_comparison(job: str, selector: str) -> bytes:
    entries = 0
    file_bytes = 0
    for role in ("verifier", "candidate"):
        files = fixture_files(role)
        directories = {
            parent
            for path in files
            for parent in Path(path).parents
            if parent != Path(".")
        }
        entries += len(files) + len(directories)
        file_bytes += sum(map(len, files.values()))
    body = canonical(
        {"job": job, "selector": selector, "entries": entries, "bytes": file_bytes}
    )
    return struct.pack("!I", len(body)) + body


def inherited_root_cases(verifier: Volume, candidate: Volume, selector: str) -> None:
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    manifest = (ANCHOR / "registry/deployments" / f"{selector}.json").read_bytes()
    documents = (manifest, verifier.inventory, candidate.inventory)
    job = "7" * 64
    with (
        fixture_mapping(
            verifier, "kora-renderer-verifier", f"KORA-{job}-verifier"
        ) as verifier_map,
        fixture_mapping(
            candidate, "kora-renderer-candidate", f"KORA-{job}-candidate"
        ) as candidate_map,
        mounted(verifier_map.path, ANCHOR / "roots/verifier") as first,
        mounted(candidate_map.path, ANCHOR / "roots/candidate") as second,
    ):
        parent = os.getpid()
        roots = (first, second)
        expected_first = root_identity(parent, first)
        expected_second = root_identity(parent, second)
        expected = (expected_first, expected_second)
        assert expected_first[0] != expected_second[0]
        assert expected_first[1] != expected_second[1]
        mappings = (verifier_map, candidate_map)
        held = resource_snapshot()
        assert tuple(map(len, held)) == (2, 2, 4)
        for mapping in mappings:
            mapping.check_live()
        request = request_frame(job, selector, expected, mappings, documents)

        compare_inherited_roots(
            roots, request, match=expected_comparison(job, selector)
        )
        compare_inherited_roots((second, first), request)
        compare_inherited_roots(roots, request[:-1])
        compare_inherited_roots(roots, request + b"x")
        compare_inherited_roots(roots, request, deadline_ns=time.monotonic_ns() - 1)
        subdir = os.open("std", DIRECTORY, dir_fd=second)
        try:
            identity = root_identity(parent, subdir)
            assert identity[:2] == expected_second[:2]
            assert identity[2] != expected_second[2]
            compare_inherited_roots((first, subdir), request)
        finally:
            os.close(subdir)
        with ordinary_ext4(candidate) as foreign:
            foreign_identity = root_identity(parent, foreign)
            assert foreign_identity[0] != expected_second[0]
            assert foreign_identity[1] != expected_second[1]
            compare_inherited_roots((first, foreign), request)
            stale = (foreign_identity[0], expected_second[1], expected_second[2])
            stale_request = request_frame(
                job, selector, (expected_first, stale), mappings, documents
            )
            compare_inherited_roots(roots, stale_request)

        assert root_identity(parent, first) == expected_first
        assert root_identity(parent, second) == expected_second
        assert resource_snapshot() == held
        for mapping in mappings:
            mapping.check_live()
    assert resource_snapshot() == before
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE original_fd_cases settled=1\n")
    sys.stdout.flush()


def check_completion_diagnostic(stderr: bytes, selector: str) -> None:
    lines = stderr.splitlines()
    assert len(lines) == 1
    payload: object = json.loads(lines[0])
    assert isinstance(payload, dict)
    record = cast("dict[str, object]", payload)
    assert record.keys() == {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "message",
        "component",
        "logger",
        "instance_id",
        "phase",
        "reason",
        "entries",
        "bytes",
        "settled",
    }
    assert record["schema_version"] == 1
    assert record["level"] == "INFO"
    assert record["event"] == "release.renderer_preparation.completed"
    assert record["component"] == "release.renderer_preparation"
    assert record["logger"] == "kora.release.renderer_preparation"
    assert record["phase"] == "preparation"
    assert record["reason"] == "matched"
    assert record["settled"] is True
    assert all(
        isinstance(record[field], str)
        for field in ("timestamp", "message", "instance_id")
    )
    entries = record["entries"]
    file_bytes = record["bytes"]
    assert type(entries) is int
    assert type(file_bytes) is int
    assert 0 < entries <= 4096
    assert 0 < file_bytes <= 256 * 1024 * 1024
    private_values = (
        selector,
        BOOT_IMAGE,
        BOOT_KERNEL,
        BOOT_INITRD,
        str(ANCHOR),
        str(ENTRY_MARKER),
        PROBE_FILE,
    )
    assert all(value.encode("ascii") not in stderr for value in private_values)


def base_cases(selector: str) -> None:
    stderr = check_cli("static_match", selector, pin=selector, match=True)
    assert stderr is not None
    check_completion_diagnostic(stderr, selector)
    check_cli("independent_selector", selector, pin="0" * 64, refusal="selector")
    check_cli("boot_evidence", selector, pin=selector, initrd="0" * 64, refusal="boot")
    read_end, writer = os.pipe()
    os.close(read_end)
    try:
        check_cli(
            "failed_diagnostics", selector, pin=selector, match=True, failed_sink=writer
        )
    finally:
        os.close(writer)


def content_cases(verifier: Volume, candidate: Volume, selector: str) -> None:
    corrupt_probe(candidate)
    kernel_io_refusal(candidate)
    check_cli("verity_io", selector, pin=selector, refusal="read")
    candidate.data.unlink()
    candidate.hashes.unlink()
    changed = make_volume("candidate", extra=True)
    new_selector = provision(verifier, changed)
    check_cli("unlisted_file", new_selector, pin=new_selector)


def unreadable_probe(root: int) -> None:
    assert os.getuid() == os.geteuid() == 65532
    assert os.getgid() == os.getegid() == 65532
    assert not os.getgroups()
    assert "std" in os.listdir(root)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    directory = os.open("std", DIRECTORY, dir_fd=root)
    try:
        entry = os.stat("probe.bin", dir_fd=directory, follow_symlinks=False)
        assert stat.S_ISREG(entry.st_mode)
        assert stat.S_IMODE(entry.st_mode) == 0o400
        assert entry.st_uid == 0
        assert entry.st_nlink == 1
        assert entry.st_size == BLOCK_SIZE
        try:
            denied = os.open("probe.bin", file_flags, dir_fd=directory)
        except OSError as error:
            if error.errno != errno.EACCES:
                raise
        else:
            os.close(denied)
            raise AssertionError(
                "comparator identity opened the unreadable fixture file"
            )
    finally:
        os.close(directory)


def unreadable_entry_case(candidate: Volume, selector: str) -> None:
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    mountpoint = ANCHOR / "roots" / "unreadable-probe"
    with (
        fixture_mapping(
            candidate, "kora-renderer-candidate", "KORA-1817-UNREADABLE"
        ) as mapping,
        mounted(mapping.path, mountpoint) as root,
    ):
        mapping.check_live()
        with (mountpoint / PROBE_FILE).open("rb", buffering=0) as source:
            assert source.read(BLOCK_SIZE + 1) == probe_bytes(candidate.role)
        result = subprocess.run(
            [
                "/usr/bin/setpriv",
                "--reuid=65532",
                "--regid=65532",
                "--clear-groups",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--bounding-set=-all",
                "--no-new-privs",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                str(Path(__file__).resolve()),
                "unreadable_probe",
                str(root),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=ENV,
            cwd="/",
            pass_fds=(root,),
            check=False,
            timeout=8,
        )
        assert result.returncode == 0
        assert result.stdout == result.stderr == b""

    assert resource_snapshot() == before
    assert descriptor_snapshot() == descriptors
    check_cli("unreadable_entry", selector, pin=selector, refusal="channel")
    assert descriptor_snapshot() == descriptors


def geometry_cases(verifier: Volume) -> None:
    for label, block, journal, refusal in (
        ("journaled_ext4", 4096, True, "verity"),
        ("ext4_block_size", 1024, False, "native"),
    ):
        candidate = make_volume("candidate", filesystem_block=block, journal=journal)
        selector = provision(verifier, candidate)
        check_cli(label, selector, pin=selector, refusal=refusal)
        candidate.data.unlink()
        candidate.hashes.unlink()


def descriptor_snapshot() -> dict[int, tuple[int, int]]:
    descriptors: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc/self/fd"):
        descriptor = int(name)
        try:
            item = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            descriptors[descriptor] = (item.st_dev, item.st_ino)
    return descriptors


@dataclass(frozen=True)
class WalkExpected:
    entries: int
    scanners: int
    descents: int
    verified_files: int
    refuses: bool = False


def record_bounded_walk(
    root_path: Path, names: Sequence[str], checks: WalkExpected
) -> None:
    from scripts.release.renderer_installation_check import (
        Index,
        Refusal,
        mount_id,
        open_directory,
    )

    index = Index()
    for name in names:
        payload = (root_path / name).read_bytes()
        index.add(os.fsencode(name), len(payload), hashlib.sha256(payload).digest())
    calls: Counter[str] = Counter()
    reads: Counter[int] = Counter()

    def observe(frame: FrameType, event: str, function: object) -> None:
        if event != "c_call":
            return
        if frame.f_code is Index.walk.__code__ and function is os.scandir:
            calls["scanner"] += 1
        if frame.f_code is open_directory.__code__ and function is os.dup:
            assert descriptor_snapshot() == from_root
            calls["root_dup"] += 1
        if frame.f_code is open_directory.__code__ and function is os.open:
            calls["descents"] += 1
        if frame.f_code is Index.file.__code__:
            if function is os.open:
                calls["file_open"] += 1
            if (
                isinstance(function, BuiltinMethodType)
                and function.__name__ == "read"
                and isinstance(function.__self__, FileIO)
            ):
                item: object = frame.f_locals["item"]
                assert isinstance(item, int)
                reads[item] += 1

    before = descriptor_snapshot()
    with ExitStack() as owned:
        root = os.open(root_path, DIRECTORY)
        owned.callback(os.close, root)
        from_root = descriptor_snapshot()
        assert from_root.keys() - before.keys() == {root}
        mount = mount_id(root)
        prior = cast(
            "Callable[[FrameType, str, object], object] | None", sys.getprofile()
        )
        deadline = time.monotonic_ns() + 15_000_000_000

        sys.setprofile(observe)
        refusal: str | None = None
        try:
            index.walk(root, mount, (0, index.count), deadline)
        except Refusal as error:
            refusal = str(error)
        finally:
            sys.setprofile(prior)

        assert descriptor_snapshot() == from_root
        assert mount_id(root) == mount

    assert descriptor_snapshot() == before
    assert refusal == ("capacity" if checks.refuses else None)
    assert index.examined == checks.entries
    assert index.hashed == checks.verified_files
    assert sum(index.found) == checks.verified_files
    assert calls == {
        "scanner": checks.scanners,
        "root_dup": checks.scanners,
        "descents": checks.descents,
        "file_open": checks.verified_files,
    }
    assert reads == {item: 2 for item in range(checks.verified_files)}


def bounded_tree_cases(root: Path) -> None:
    deep = root / "deep"
    relative = Path(*("d" for _ in range(32)), "leaf")
    (deep / relative).parent.mkdir(parents=True)
    (deep / relative).write_bytes(b"d")
    record_bounded_walk(deep, (relative.as_posix(),), WalkExpected(33, 33, 528, 1))
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE depth_walk settled=1\n")
    sys.stdout.flush()

    wide = root / "wide"
    (wide / "sub").mkdir(parents=True)
    (wide / "sub/leaf").write_bytes(b"s")
    names = tuple(f"root-{item:04d}" for item in range(4095))
    for name in names[:-1]:
        (wide / name).write_bytes(b"w")
    record_bounded_walk(wide, (*names[:-1], "sub/leaf"), WalkExpected(4096, 2, 1, 4095))
    (wide / names[-1]).write_bytes(b"w")
    record_bounded_walk(
        wide, (*names, "sub/leaf"), WalkExpected(4097, 2, 1, 4095, refuses=True)
    )
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE examined_entry_bound settled=1\n")
    sys.stdout.flush()


def bounded_pipe_case() -> None:
    if sys.platform != "linux":
        raise RuntimeError("guest pipe capacity requires Linux")
    from scripts.release.renderer_installation_check import Refusal
    from scripts.release.verify_renderer_installation import (
        CONTROL_SCRATCH,
        DIRECTORY_SCRATCH,
        JSON_SCRATCH,
        pipe,
        reserve,
    )

    page = os.sysconf("SC_PAGESIZE")
    budget = reserve()
    mib = 1024 * 1024
    charged = 12 * mib + JSON_SCRATCH + DIRECTORY_SCRATCH + CONTROL_SCRATCH + 2 * budget
    before = descriptor_snapshot()
    assert page > 1
    assert budget == 16 * page
    assert charged <= 16 * mib

    with ExitStack() as first_owner, ExitStack() as second_owner:
        first = pipe(first_owner, budget)
        second = pipe(second_owner, budget)
        assert descriptor_snapshot().keys() - before.keys() == {
            source.fileno() for pair in (first, second) for source in pair
        }
        sizes: list[int] = []
        for reader, writer in (first, second):
            read_size = fcntl.fcntl(reader.fileno(), fcntl.F_GETPIPE_SZ)
            assert read_size == fcntl.fcntl(writer.fileno(), fcntl.F_GETPIPE_SZ)
            assert page <= read_size <= budget
            assert read_size % page == 0
            sizes.append(read_size)
        for reader, writer in (first, second):
            assert writer.write(b"c") == 1
            writer.close()
            assert reader.read(1) == b"c"
            assert reader.read(1) == b""

    assert descriptor_snapshot() == before
    refusal: str | None = None
    with ExitStack() as refused_owner:
        try:
            pipe(refused_owner, page - 1)
        except Refusal as error:
            refusal = str(error)
        assert refusal == "capacity"
        provisional = descriptor_snapshot().keys() - before.keys()
        assert len(provisional) == 2
        assert all(fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ) >= page for fd in provisional)
    assert descriptor_snapshot() == before
    sys.stdout.write(
        f"KORA_RENDERER_FIXTURE_CASE pipe_capacity settled=1 page={page} "
        f"reserved_each={budget} first={sizes[0]} second={sizes[1]} charged={charged}\n"
    )
    sys.stdout.flush()


def require_pinned_checker() -> None:
    assert sys.flags.isolated
    assert sys.flags.no_site
    assert sys.flags.dont_write_bytecode
    assert Path(COMPARATOR).resolve(strict=True) == Path(COMPARATOR)
    assert Path(CHECKER).resolve(strict=True) == Path(CHECKER)
    sys.path[:0] = ["/run/kora-preparer", "/run/kora-preparer/support"]
    from scripts.release import (
        renderer_installation_check,
        verify_renderer_installation,
    )

    assert Path(renderer_installation_check.__file__).resolve() == Path(COMPARATOR)
    assert Path(verify_renderer_installation.__file__).resolve() == Path(CHECKER)


def bounds_cases() -> None:
    require_pinned_checker()
    with TemporaryDirectory(dir="/run", prefix="kora-renderer-bounds-") as directory:
        bounded_tree_cases(Path(directory))
    bounded_pipe_case()
    sys.stdout.write("KORA_RENDERER_FIXTURE_PASS suite=bounds\n")
    sys.stdout.flush()


def busy_unmount_case(verifier: Volume) -> None:
    require_pinned_checker()
    from scripts.release.verify_renderer_installation import Job, MountOwner, device

    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    mountpoint = ANCHOR / "roots" / "verifier"
    mountpoint.mkdir(mode=0o700)
    original_namespace = namespace_identity(os.getpid())
    attempts: list[int] = []
    first_refusal = 0
    with fixture_mapping(
        verifier, "kora-renderer-verifier", "KORA-1817-BUSY"
    ) as mapping:
        mapping.check_live()
        job = Job()
        mount = MountOwner(
            0,
            str(mountpoint),
            device(os.stat(mapping.path).st_rdev),
            original_namespace,
        )
        release = partial(mount.unmount, job)
        with ExitStack() as owner:
            owner.callback(job.release, release)
            mount.acquire(job)
            with (mountpoint / PROBE_FILE).open("rb", buffering=0) as held:
                original = root_identity(os.getpid(), held.fileno())
                assert original[:2] == (mount.number, mount.dev)
                assert held.read(BLOCK_SIZE) == probe_bytes(verifier.role)
                assert descriptor_snapshot().keys() - descriptors.keys() == {
                    held.fileno()
                }
                native = subprocess.run(
                    ["/usr/bin/umount", "/run/kora-preparation/roots/verifier"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    env=ENV,
                    cwd="/",
                    check=False,
                    timeout=8,
                )
                assert native.returncode == 32
                assert not native.stdout
                assert native.stderr.endswith(b": target is busy.\n")
                assert mount.current() == mount.number

                def observe(frame: FrameType, event: str, result: object) -> None:
                    nonlocal first_refusal
                    if event != "return" or frame.f_code is not Job.execute.__code__:
                        return
                    child = cast("subprocess.Popen[bytes]", frame.f_locals["child"])
                    assert child.args == ["/usr/bin/umount", str(mountpoint)]
                    assert child.returncode is not None
                    assert job.active is None
                    attempts.append(child.returncode)
                    if len(attempts) == 1:
                        assert child.returncode == 32
                        assert job.refusal == "settlement"
                        assert mount.current() == original[0]
                        assert root_identity(os.getpid(), held.fileno()) == original
                        first_refusal = job.first_refusal
                        held.close()

                prior = cast(
                    "Callable[[FrameType, str, object], object] | None",
                    sys.getprofile(),
                )
                sys.stdout.write(
                    "KORA_RENDERER_FIXTURE_STAGE busy_unmount release_started\n"
                )
                sys.stdout.flush()
                sys.setprofile(observe)
                try:
                    job.release(release)
                finally:
                    sys.setprofile(prior)

                assert held.closed
                assert attempts == [32, 0]
                assert job.refusal == "settlement"
                assert job.first_refusal == first_refusal > 0
                assert mount.current() is None
                assert mount.number == original[0]
                assert str(mountpoint) not in resource_snapshot()[0]
    mountpoint.rmdir()
    assert namespace_identity(os.getpid()) == original_namespace
    assert not os.path.lexists(mapping.path)
    assert descriptor_snapshot() == descriptors
    assert resource_snapshot() == before
    assert not ENTRY_MARKER.exists()
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE busy_unmount settled=1\n")
    sys.stdout.flush()


def fixture_mount_rows(dev: str) -> tuple[tuple[int, bytes, bool], ...]:
    from scripts.release.renderer_installation_check import mount_rows

    expected = dev.encode("ascii")
    with closing(mount_rows()) as rows:
        return tuple(
            (row.number, row.path, row.whole_read_only)
            for row in rows
            if row.dev == expected
        )


def release_fixture_mount(
    dev: str, allowed: tuple[Path, ...], expected_namespace: tuple[int, int]
) -> None:
    from scripts.release.renderer_installation_check import mount_rows

    assert namespace_identity(os.getpid()) == expected_namespace
    with closing(mount_rows()) as live:
        mounts = tuple(live)
    rows = tuple(row for row in mounts if row.dev == dev.encode("ascii"))
    assert len(rows) <= 1
    for row in rows:
        assert row.path in {os.fsencode(target) for target in allowed}
        assert tuple(mount for mount in mounts if mount.path == row.path) == (row,)
        command("/usr/bin/umount", os.fsdecode(row.path))
    assert not fixture_mount_rows(dev)


def wrong_mount_identity_case(verifier: Volume, candidate: Volume) -> None:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    original_namespace = namespace_identity(os.getpid())
    target = ANCHOR / "roots" / "verifier"
    parked = ANCHOR / "roots" / "identity-parked"
    with ExitStack() as owned:
        for path in (target, parked):
            path.mkdir(mode=0o700)
            owned.callback(path.rmdir)
        original = owned.enter_context(
            fixture_mapping(verifier, "kora-renderer-verifier", "KORA-1817-ORIGINAL")
        )
        replacement = owned.enter_context(
            fixture_mapping(
                candidate, "kora-renderer-candidate", "KORA-1817-REPLACEMENT"
            )
        )
        for mapping in (original, replacement):
            mapping.check_live()
        original_dev = checker.device(os.stat(original.path).st_rdev)
        replacement_dev = checker.device(os.stat(replacement.path).st_rdev)
        assert original_dev != replacement_dev
        owned.callback(
            release_fixture_mount, original_dev, (target, parked), original_namespace
        )
        owned.callback(
            release_fixture_mount, replacement_dev, (target,), original_namespace
        )

        job = checker.Job()
        mount = checker.MountOwner(0, str(target), original_dev, original_namespace)
        mount.acquire(job)
        original_root = os.open(target, DIRECTORY)
        owned.callback(os.close, original_root)
        held = root_identity(os.getpid(), original_root)
        assert (mount.number, mount.dev) == held[:2]
        assert fixture_mount_rows(original_dev) == (
            (held[0], os.fsencode(target), True),
        )

        command("/usr/bin/mount", "--move", str(target), str(parked))
        command(
            "/usr/bin/mount",
            "-t",
            "ext4",
            "-o",
            "ro,noload",
            replacement.path,
            str(target),
        )
        original_rows = fixture_mount_rows(original_dev)
        replacement_rows = fixture_mount_rows(replacement_dev)
        assert original_rows == ((held[0], os.fsencode(parked), True),)
        assert len(replacement_rows) == 1
        replacement_id, path_name, read_only = replacement_rows[0]
        assert replacement_id != held[0]
        assert path_name == os.fsencode(target)
        assert read_only
        assert checker.device(target.stat().st_dev) == replacement_dev
        assert root_identity(os.getpid(), original_root) == held
        assert job.refusal is None

        native_release_started = False

        def observe(frame: FrameType, event: str, result: object) -> None:
            nonlocal native_release_started
            if event == "call" and frame.f_code is subprocess.Popen.__init__.__code__:
                native_release_started = True

        prior = cast(
            "Callable[[FrameType, str, object], object] | None", sys.getprofile()
        )
        refusal: str | None = None
        sys.setprofile(observe)
        try:
            with job.checked():
                mount.unmount(job)
        except checker.Refusal as error:
            refusal = str(error)
        finally:
            sys.setprofile(prior)

        assert refusal == job.refusal == "settlement"
        observed_refusal = job.first_refusal
        assert observed_refusal > 0
        assert not native_release_started
        assert job.active is None
        assert mount.number == held[0]
        assert fixture_mount_rows(original_dev) == original_rows
        assert fixture_mount_rows(replacement_dev) == replacement_rows
        assert checker.device(target.stat().st_dev) == replacement_dev
        assert root_identity(os.getpid(), original_root) == held

    assert job.refusal == "settlement"
    assert job.first_refusal == observed_refusal
    assert namespace_identity(os.getpid()) == original_namespace
    assert not os.path.lexists(original.path)
    assert not os.path.lexists(replacement.path)
    assert descriptor_snapshot() == descriptors
    assert resource_snapshot() == before
    assert not ENTRY_MARKER.exists()
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE wrong_mount_identity settled=1\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class HeldRoot:
    descriptor: int
    block: int
    control: int
    path: str
    uuid: str
    table: str
    identity: tuple[int, str, int]


def held_root(frame: FrameType, identity: object) -> HeldRoot:
    values = frame.f_locals
    return HeldRoot(
        cast("int", values["fd"]),
        cast("int", values["block"]),
        cast("int", values["dm"]),
        cast("str", values["path"]),
        cast("str", values["uuid"]),
        cast("str", values["table"]),
        cast("tuple[int, str, int]", identity),
    )


def pipe_identity(entry: os.stat_result) -> tuple[int, int]:
    return entry.st_dev, entry.st_ino


def observe_admitted_child(
    child: subprocess.Popen[bytes],
    passed: tuple[int, ...],
    pipes: Sequence[tuple[int, int]],
    original_deadline: int,
) -> None:
    proc = Path(f"/proc/{child.pid}")
    ready_by = min(original_deadline, time.monotonic_ns() + 5_000_000_000)
    expected = {"0", "1", "2", *(str(fd) for fd in passed)}
    command = b"\0".join(os.fsencode(argument) for argument in COMPARATOR_ARGUMENTS)
    while (remaining := ready_by - time.monotonic_ns()) > 0:
        if (proc / "cmdline").read_bytes() == command + b"\0":
            if {fd.name for fd in (proc / "fd").iterdir()} == expected:
                break
        select.select((), (), (), min(0.01, remaining / 1_000_000_000))
    else:
        raise AssertionError("original comparator did not reach inherited input")
    child_authority(child.pid, passed)
    assert [pipe_identity((proc / "fd" / str(fd)).stat()) for fd in passed[:2]] == pipes
    assert time.monotonic_ns() < original_deadline


@dataclass
class PreparationObservationFailure:
    first: BaseException | None = None

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if error is None:
            return False
        if self.first is None:
            self.first = error
        return True


def observe_successful_preparation() -> int:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    expected_stages = (
        "initial_live",
        "child_bound",
        "result_eof",
        "child_reaped",
        "pipes_closed",
        "final_live",
        "roots_closed",
        "settled",
        "stdout_write",
    )
    stages: list[str] = []
    before = resource_snapshot()
    descriptors = descriptor_snapshot()
    original_namespace = namespace_identity(os.getpid())
    roots: list[HeldRoot] = []
    live_checks: list[HeldRoot] = []
    endpoints: list[FileIO] = []
    pipes: list[tuple[int, int]] = []
    closed_roots: set[int] = set()
    child: subprocess.Popen[bytes] | None = None
    comparison_worker: threading.Thread | None = None
    matched: tuple[int, int] | None = None
    observation_failure = PreparationObservationFailure()
    close_callback = "_BaseExitStack._create_cb_wrapper.<locals>._exit_wrapper"
    returned = {
        checker.observe.__code__,
        checker.mounts.__code__,
        checker.pipe.__code__,
        checker.receive.__code__,
        checker.read_pipe.__code__,
        checker.child_match.__code__,
        checker.run.__code__,
    }

    def reached(stage: str) -> None:
        assert len(stages) < len(expected_stages)
        assert stage == expected_stages[len(stages)]
        stages.append(stage)

    def observe_return(frame: FrameType, result: object) -> None:
        nonlocal matched
        caller = frame.f_back
        code = frame.f_code
        if code is checker.observe.__code__:
            assert len(live_checks) < 2
            live_checks.append(held_root(frame, result))
        elif code is checker.mounts.__code__ and caller is not None:
            assert caller.f_code is checker.compare.__code__
            assert namespace_identity(os.getpid()) == original_namespace
            assert len(live_checks) == 2
            for root in live_checks:
                assert root_identity(os.getpid(), root.descriptor) == root.identity
            if not roots:
                roots.extend(live_checks)
                reached("initial_live")
            else:
                assert live_checks == roots
                assert resource_snapshot() == (
                    frozenset(root.path for root in roots),
                    frozenset(f"kora-renderer-{role}" for role in checker.ROLES),
                    frozenset(
                        str(ANCHOR / "media" / f"{role}.{kind}")
                        for role in checker.ROLES
                        for kind in ("data", "hash")
                    ),
                )
                reached("final_live")
            live_checks.clear()
        elif caller is not None and caller.f_code is checker.child_match.__code__:
            if code is checker.pipe.__code__:
                reader, writer = cast("tuple[FileIO, FileIO]", result)
                pipe = pipe_identity(os.fstat(reader.fileno()))
                assert pipe_identity(os.fstat(writer.fileno())) == pipe
                assert pipe not in pipes
                assert len(pipes) < 2
                pipes.append(pipe)
                endpoints.extend((reader, writer))
            elif code is checker.receive.__code__:
                assert isinstance(result, tuple)
                assert child is frame.f_locals["child"]
                assert child is not None
                assert child.returncode == 0
                assert not Path(f"/proc/{child.pid}").exists()
                matched = cast("tuple[int, int]", result)
                reached("child_reaped")
        elif code is checker.child_match.__code__:
            assert result == matched
            job = cast("checker.Job", frame.f_locals["job"])
            assert job.active is None
            assert len(endpoints) == 4
            assert all(endpoint.closed for endpoint in endpoints)
            assert not set(pipes).intersection(descriptor_snapshot().values())
            reached("pipes_closed")
        elif (
            code is checker.read_pipe.__code__
            and caller is not None
            and caller.f_code is checker.receive.__code__
            and result == b""
        ):
            reached("result_eof")
        elif code is checker.run.__code__:
            assert result == matched
            assert comparison_worker is not None
            assert not comparison_worker.is_alive()
            reached("settled")

    def observe(frame: FrameType, event: str, result: object) -> None:
        nonlocal child, comparison_worker
        code = frame.f_code
        if event == "return" and code in returned:
            observe_return(frame, result)
        elif event == "call" and code is checker.compare.__code__:
            assert comparison_worker is None
            comparison_worker = threading.current_thread()
        elif event == "call" and code is checker.Job.activate.__code__:
            job = cast("checker.Job", frame.f_locals["self"])
            assert child is None
            child = job.active
            assert child is not None
            assert len(endpoints) == 4
            transfer = frame.f_back
            assert transfer is not None
            assert transfer.f_code is checker.transfer.__code__
            handoff = transfer.f_back
            assert handoff is not None
            assert handoff.f_code is checker.child_match.__code__
            passed_roots = cast("tuple[int, int]", handoff.f_locals["roots"])
            assert passed_roots == tuple(root.descriptor for root in roots)
            passed = (
                cast("int", handoff.f_locals["request"]),
                cast("int", handoff.f_locals["result"]),
                *passed_roots,
            )
            observe_admitted_child(child, passed, pipes, job.deadline_ns)
            reached("child_bound")
        elif event == "call" and code is checker.MountOwner.unmount.__code__:
            assert stages[-1] == "roots_closed"
            open_fds = descriptor_snapshot()
            assert all(root.block not in open_fds for root in roots)
        elif (
            event == "c_return"
            and result is os.close
            and code.co_qualname == close_callback
            and len(closed_roots) < 2
        ):
            arguments = cast("tuple[int, ...]", frame.f_locals["args"])
            if arguments[0] in {root.descriptor for root in roots}:
                assert stages[-1] == "final_live"
                assert arguments[0] not in closed_roots
                closed_roots.add(arguments[0])
                if len(closed_roots) == 2:
                    reached("roots_closed")
        elif (
            event == "c_call"
            and code is checker.main.__code__
            and isinstance(result, BuiltinMethodType)
            and result.__self__ is sys.stdout
            and result.__name__ == "write"
        ):
            assert resource_snapshot() == before
            assert descriptor_snapshot() == descriptors
            assert not ENTRY_MARKER.exists()
            reached("stdout_write")

    def observer(frame: FrameType, event: str, result: object) -> None:
        if observation_failure.first is None:
            with observation_failure:
                observe(frame, event, result)

    prior = cast("Callable[[FrameType, str, object], object] | None", sys.getprofile())
    prior_threads = cast(
        "Callable[[FrameType, str, object], object] | None", threading.getprofile()
    )
    threading.setprofile(observer)
    sys.setprofile(observer)
    try:
        status = checker.main()
    finally:
        sys.setprofile(prior)
        threading.setprofile(prior_threads)
    if observation_failure.first is not None:
        raise observation_failure.first
    assert status == 0
    assert tuple(stages) == expected_stages
    return status


def successful_preparation_case(selector: str) -> None:
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(Path(__file__).resolve()), selector],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={
            "LC_ALL": "C",
            "KORA_PREPARATION_SELECTOR": selector,
            "KORA_PREPARATION_IMAGE_SHA256": BOOT_IMAGE,
            "KORA_PREPARATION_KERNEL_SHA256": BOOT_KERNEL,
            "KORA_PREPARATION_INITRD_SHA256": BOOT_INITRD,
        },
        cwd="/",
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    assert result.stdout == f"static_match {selector}\n".encode()
    assert resource_snapshot() == before
    assert not ENTRY_MARKER.exists()
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE successful_preparation settled=1\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class OwnerLossResources:
    mounts: dict[str, tuple[int, str]]
    mappings: dict[str, tuple[str, str, tuple[str, ...]]]
    loops: dict[str, str]


def owner_loss_resources() -> OwnerLossResources:
    from scripts.release.renderer_installation_check import mount_rows

    roles = ("verifier", "candidate")
    paths = {os.fsencode(ANCHOR / "roots" / role): role for role in roles}
    mapper_names = {f"kora-renderer-{role}": role for role in roles}
    backing = {
        str(ANCHOR / "media" / f"{role}.{kind}")
        for role in roles
        for kind in ("data", "hash")
    }
    mounts: dict[str, tuple[int, str]] = {}
    with closing(mount_rows()) as rows:
        for row in rows:
            if row.path in paths:
                role = paths[row.path]
                assert role not in mounts
                assert row.whole_read_only
                mounts[role] = (row.number, row.dev.decode("ascii"))
    assert mounts.keys() == set(roles)
    assert len({mount_id for mount_id, _ in mounts.values()}) == 2

    mappings: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for node in Path("/sys/class/block").glob("dm-*"):
        name = (node / "dm/name").read_text("ascii").strip()
        if name in mapper_names:
            role = mapper_names[name]
            dev = (node / "dev").read_text("ascii").strip()
            uuid = (node / "dm/uuid").read_text("ascii").strip()
            assert role not in mappings
            assert dev == mounts[role][1]
            assert re.fullmatch(rf"KORA-[0-9a-f]{{64}}-{role}", uuid)
            assert (node / "ro").read_text("ascii").strip() == "1"
            dependencies = tuple(
                sorted(
                    (source / "dev").read_text("ascii").strip()
                    for source in (node / "slaves").iterdir()
                )
            )
            assert len(dependencies) == 2
            mappings[role] = (dev, uuid, dependencies)
    assert mappings.keys() == set(roles)
    assert len({dev for dev, _, _ in mappings.values()}) == 2
    assert len({uuid.rsplit("-", 1)[0] for _, uuid, _ in mappings.values()}) == 1

    loops: dict[str, str] = {}
    for association in Path("/sys/class/block").glob("loop*/loop/backing_file"):
        path = association.read_text("ascii").strip()
        if path in backing:
            node = association.parent.parent
            assert path not in loops
            assert (node / "ro").read_text("ascii").strip() == "1"
            assert (association.parent / "autoclear").read_text("ascii").strip() == "0"
            loops[path] = (node / "dev").read_text("ascii").strip()
    assert loops.keys() == backing
    assert len(set(loops.values())) == 4
    for role, (_, _, dependencies) in mappings.items():
        assert set(dependencies) == {
            loops[str(ANCHOR / "media" / f"{role}.{kind}")] for kind in ("data", "hash")
        }
    return OwnerLossResources(mounts, mappings, loops)


def observe_owner_loss(descriptor: int) -> Never:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    failure = PreparationObservationFailure()
    with socket.socket(fileno=descriptor) as gate:
        gate.set_inheritable(False)
        gate.setblocking(False)

        def observer(frame: FrameType, event: str, result: object) -> None:
            if (
                event != "call"
                or frame.f_code is not checker.MountOwner.unmount.__code__
                or failure.first is not None
            ):
                return
            with failure:
                job = cast("checker.Job", frame.f_locals["job"])
                mount = cast("checker.MountOwner", frame.f_locals["self"])
                assert mount.issued
                assert mount.number is not None
                assert mount.original_namespace == namespace_identity(os.getpid())
                assert job.active is None
                assert job.refusal is None
                now = time.monotonic_ns()
                deadline = min(job.deadline_ns, now + checker.SETTLEMENT_NS)
                assert now < deadline
                ready = b"R" + deadline.to_bytes(OWNER_LOSS_READY_BYTES - 1, "big")
                assert gate.send(ready) == OWNER_LOSS_READY_BYTES
                try:
                    remaining = max(0, deadline - time.monotonic_ns())
                    select.select((gate,), (), (), remaining / 1_000_000_000)
                finally:
                    assert gate.send(b"F") == 1
                raise AssertionError("owner-loss rendezvous resumed")

        prior = cast(
            "Callable[[FrameType, str, object], object] | None", threading.getprofile()
        )
        threading.setprofile(observer)
        try:
            checker.main()
        finally:
            threading.setprofile(prior)

    if failure.first is not None:
        raise failure.first
    raise AssertionError("original preparer did not reach its release boundary")


def start_observed_preparer(
    selector: str, channel: int, channel_variable: str
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["/usr/bin/python3", "-I", "-S", "-B", str(Path(__file__).resolve()), selector],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            "LC_ALL": "C",
            "KORA_PREPARATION_SELECTOR": selector,
            "KORA_PREPARATION_IMAGE_SHA256": BOOT_IMAGE,
            "KORA_PREPARATION_KERNEL_SHA256": BOOT_KERNEL,
            "KORA_PREPARATION_INITRD_SHA256": BOOT_INITRD,
            channel_variable: str(channel),
        },
        cwd="/",
        close_fds=True,
        pass_fds=(channel,),
    )


def read_observed_frame(fd: int, size: int, deadline: int) -> bytes:
    frame = bytearray()
    while len(frame) < size:
        remaining = max(0, deadline - time.monotonic_ns()) / 1_000_000_000
        assert select.select((fd,), (), (), remaining)[0]
        chunk = os.read(fd, size - len(frame))
        assert chunk
        frame.extend(chunk)
    return bytes(frame)


def settle_failed_preparer(
    child: subprocess.Popen[bytes],
    gate: socket.socket,
    baseline: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> None:
    from scripts.release.verify_renderer_installation import SETTLEMENT_NS

    deadline = time.monotonic_ns() + SETTLEMENT_NS + OBSERVATION_MARGIN_NS
    try:
        with gate:
            child.send_signal(signal.SIGTERM)
    finally:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic_ns()) / 1_000_000_000)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                "preparer still owns work at guest retirement"
            ) from error
        assert not Path(f"/proc/{child.pid}").exists()
        assert resource_snapshot() == baseline, (
            "mount or device ownership requires guest retirement"
        )
        assert time.monotonic_ns() <= deadline, (
            "preparer settlement observation exceeded its bound"
        )


@contextmanager
def observe_preparer_settlement(
    child: subprocess.Popen[bytes],
    gate: socket.socket,
    baseline: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> Generator[None, None, None]:
    finished = False
    try:
        yield
        finished = True
    finally:
        try:
            if not finished:
                settle_failed_preparer(child, gate, baseline)
        finally:
            if child.stdout is not None:
                child.stdout.close()


def owner_loss_case(selector: str) -> None:
    require_pinned_checker()
    from scripts.release.verify_renderer_installation import SETTLEMENT_NS

    assert resource_snapshot() == (frozenset(), frozenset(), frozenset())
    namespace = namespace_identity(os.getpid())
    deadline = time.monotonic_ns() + 20_000_000_000

    def remaining(until: int = deadline) -> float:
        return max(0, until - time.monotonic_ns()) / 1_000_000_000

    parent, preparer = socket.socketpair()
    with parent, preparer:
        child = start_observed_preparer(selector, preparer.fileno(), OWNER_LOSS_CHANNEL)
        preparer.close()
        assert child.stdout is not None
        with child.stdout as output:
            try:
                frame = read_observed_frame(
                    parent.fileno(), OWNER_LOSS_READY_BYTES, deadline
                )
                assert frame[:1] == b"R"
                gate_deadline = int.from_bytes(frame[1:], "big")
                observed = time.monotonic_ns()
                assert observed < gate_deadline <= observed + SETTLEMENT_NS
                stop_by = min(deadline, gate_deadline)
                assert child.poll() is None
                assert namespace_identity(child.pid) == namespace
                owned = owner_loss_resources()
                assert not select.select((parent,), (), (), 0)[0]
                assert time.monotonic_ns() < stop_by

                child.kill()
                assert time.monotonic_ns() < stop_by
                status = child.wait(timeout=remaining(stop_by))
                assert time.monotonic_ns() < stop_by

                assert status == -signal.SIGKILL
                assert not Path(f"/proc/{child.pid}").exists()
                assert select.select((parent,), (), (), remaining())[0]
                assert parent.recv(2) == b""
                assert select.select((output,), (), (), remaining())[0]
                assert output.read(1) == b""
                assert namespace_identity(os.getpid()) == namespace
                assert owner_loss_resources() == owned
                assert not ENTRY_MARKER.exists()
            finally:
                parent.close()
                if child.returncode is None:
                    child.wait(timeout=remaining())

    sys.stdout.write(
        "KORA_RENDERER_FIXTURE_CASE owner_loss preparer_signal=9 "
        "resources_live_after_reap=1\n"
    )
    sys.stdout.flush()


def observe_retained_busy(descriptor: int) -> Never:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    failure = PreparationObservationFailure()
    original_job: checker.Job | None = None
    first_refusal = 0
    candidate = ANCHOR / "roots" / "candidate"
    with socket.socket(fileno=descriptor) as gate:
        gate.set_inheritable(False)
        gate.setblocking(False)

        def observe(frame: FrameType, event: str) -> None:
            nonlocal original_job, first_refusal
            code = frame.f_code
            if event == "call" and code is checker.MountOwner.unmount.__code__:
                assert original_job is None
                mount = cast("checker.MountOwner", frame.f_locals["self"])
                job = cast("checker.Job", frame.f_locals["job"])
                assert mount.issued
                assert mount.number is not None
                assert mount.path == str(candidate)
                assert mount.original_namespace == namespace_identity(os.getpid())
                assert job.refusal is None
                assert job.active is None
                deadline = min(
                    job.deadline_ns, time.monotonic_ns() + checker.SETTLEMENT_NS
                )
                ready = b"R" + deadline.to_bytes(OWNER_LOSS_READY_BYTES - 1, "big")
                assert gate.send(ready) == OWNER_LOSS_READY_BYTES
                assert read_observed_frame(gate.fileno(), 1, deadline) == b"H"
                assert time.monotonic_ns() < deadline
                original_job = job
            elif (
                original_job is not None
                and event == "return"
                and code is checker.Job.execute.__code__
            ):
                assert frame.f_locals["self"] is original_job
                child = cast("subprocess.Popen[bytes]", frame.f_locals["child"])
                assert child.args == ["/usr/bin/umount", str(candidate)]
                assert child.returncode == 32
                assert original_job.active is None
                assert original_job.refusal == "settlement"
                if first_refusal == 0:
                    first_refusal = original_job.first_refusal
                assert original_job.first_refusal == first_refusal > 0
            elif (
                event == "call"
                and code is threading.Event.wait.__code__
                and (caller := frame.f_back) is not None
                and caller.f_code is checker.Job.retain_for_guest_retirement.__code__
            ):
                assert original_job is not None
                assert caller.f_locals["self"] is original_job
                assert original_job.active is None
                assert original_job.refusal == "settlement"
                assert original_job.first_refusal == first_refusal > 0
                parked = time.monotonic_ns()
                assert parked - first_refusal >= checker.SETTLEMENT_NS
                report = b"P" + struct.pack(
                    "!QQQ", first_refusal, parked, threading.get_native_id()
                )
                assert gate.send(report) == RETAINED_BUSY_PARK_BYTES

        def observer(frame: FrameType, event: str, result: object) -> None:
            if failure.first is None and frame.f_code in relevant:
                with failure:
                    observe(frame, event)
                if failure.first is not None:
                    with failure:
                        failed = b"F" * RETAINED_BUSY_PARK_BYTES
                        assert gate.send(failed) == RETAINED_BUSY_PARK_BYTES

        relevant = {
            checker.MountOwner.unmount.__code__,
            checker.Job.execute.__code__,
            threading.Event.wait.__code__,
        }
        prior = cast(
            "Callable[[FrameType, str, object], object] | None", threading.getprofile()
        )
        threading.setprofile(observer)
        try:
            checker.main()
        finally:
            threading.setprofile(prior)

    if failure.first is not None:
        raise failure.first
    raise AssertionError("original preparer returned before outer retirement")


def wait_for_parked_worker(pid: int, thread: int, deadline: int) -> None:
    assert os.uname().machine == "x86_64"
    syscall = Path(f"/proc/{pid}/task/{thread}/syscall")
    futex_flags = 128 | 256
    while (remaining := deadline - time.monotonic_ns()) > 0:
        call = syscall.read_text("ascii").split()
        if (
            len(call) >= 3
            and call[0] == "202"
            and int(call[2], 0) & ~futex_flags in {0, 9}
        ):
            return
        select.select((), (), (), min(0.01, remaining / 1_000_000_000))
    raise AssertionError("original cleanup worker did not park")


def retained_busy_case(selector: str) -> None:
    require_pinned_checker()
    from scripts.release.verify_renderer_installation import SETTLEMENT_NS

    assert resource_snapshot() == (frozenset(), frozenset(), frozenset())
    namespace = namespace_identity(os.getpid())
    deadline = time.monotonic_ns() + 20_000_000_000
    parent, preparer = socket.socketpair()
    parked = False
    with parent, preparer:
        child = start_observed_preparer(
            selector, preparer.fileno(), RETAINED_BUSY_CHANNEL
        )
        preparer.close()
        assert child.stdout is not None
        with child.stdout as output:
            try:
                ready = read_observed_frame(
                    parent.fileno(), OWNER_LOSS_READY_BYTES, deadline
                )
                assert ready[:1] == b"R"
                gate_deadline = int.from_bytes(ready[1:], "big")
                now = time.monotonic_ns()
                assert now < gate_deadline <= now + SETTLEMENT_NS
                assert child.poll() is None
                assert namespace_identity(child.pid) == namespace
                owned = owner_loss_resources()
                mount = ANCHOR / "roots" / "candidate"
                with (mount / PROBE_FILE).open("rb", buffering=0) as held:
                    identity = root_identity(os.getpid(), held.fileno())
                    assert identity[:2] == owned.mounts["candidate"]
                    assert held.read(BLOCK_SIZE) == probe_bytes("candidate")
                    held_since = time.monotonic_ns()
                    assert held_since < min(gate_deadline, deadline)
                    assert parent.send(b"H") == 1

                    report = read_observed_frame(
                        parent.fileno(), RETAINED_BUSY_PARK_BYTES, deadline
                    )
                    assert report[:1] == b"P"
                    first = int.from_bytes(report[1:9], "big")
                    observed = int.from_bytes(report[9:17], "big")
                    thread = int.from_bytes(report[17:25], "big")
                    assert held_since <= first <= observed <= time.monotonic_ns()
                    assert observed - first >= SETTLEMENT_NS
                    assert thread > 0
                    assert thread != child.pid
                    refusal = b"static_refusal settlement\n"
                    assert (
                        read_observed_frame(output.fileno(), len(refusal), deadline)
                        == refusal
                    )

                    wait_for_parked_worker(child.pid, thread, deadline)
                    assert not select.select((parent, output), (), (), 0)[0]
                    assert child.poll() is None
                    assert namespace_identity(child.pid) == namespace
                    assert owner_loss_resources() == owned
                    assert root_identity(os.getpid(), held.fileno()) == identity
                    assert not ENTRY_MARKER.exists()
                assert held.closed
                assert child.poll() is None
                wait_for_parked_worker(child.pid, thread, deadline)
                assert owner_loss_resources() == owned
                assert not select.select((parent, output), (), (), 0)[0]
                parked = True
            finally:
                parent.close()
                if not parked and child.returncode is None:
                    child.wait(
                        timeout=max(0, deadline - time.monotonic_ns()) / 1_000_000_000
                    )

    sys.stdout.write(
        "KORA_RENDERER_FIXTURE_CASE retained_busy parked=1 "
        "first_refusal=settlement resources_live=1\n"
    )
    sys.stdout.flush()


def observe_late_attachment(descriptor: int) -> int:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    observed = False
    failure = PreparationObservationFailure()
    with socket.socket(fileno=descriptor) as gate:
        gate.set_inheritable(False)

        def observe(frame: FrameType, event: str, result: object) -> None:
            nonlocal observed
            if failure.first is not None or observed:
                return
            if (
                frame.f_code is not checker.attach.__code__
                or event != "c_return"
                or result is not fcntl.ioctl
                or "config" not in frame.f_locals
            ):
                return
            with failure:
                fd = cast("int", frame.f_locals["fd"])
                expected = cast("tuple[str, int, int, int]", frame.f_locals["expected"])
                job = cast("checker.Job", frame.f_locals["job"])
                assert checker.loop_state(fd) == expected
                assert job.refusal is None
                assert gate.send(b"R") == 1
                assert read_observed_frame(gate.fileno(), 1, job.deadline_ns) == b"C"
                assert checker.loop_state(fd) == expected
                assert job.refusal == "cancelled"
                observed = True

        threading.setprofile(observe)
        try:
            status = checker.main()
        finally:
            threading.setprofile(None)
    if failure.first is not None:
        raise failure.first
    assert observed
    assert status == 1
    return status


def late_attachment_case(selector: str) -> None:
    baseline = resource_snapshot()
    assert baseline == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    deadline = time.monotonic_ns() + 20_000_000_000
    parent, preparer = socket.socketpair()
    with parent, preparer:
        child = start_observed_preparer(
            selector, preparer.fileno(), LATE_ATTACHMENT_CHANNEL
        )
        preparer.close()
        assert child.stdout is not None
        with child.stdout as output:
            try:
                assert read_observed_frame(parent.fileno(), 1, deadline) == b"R"
                attached = resource_snapshot()
                assert attached == (
                    frozenset(),
                    frozenset(),
                    frozenset({str(ANCHOR / "media/verifier.data")}),
                )
                child.send_signal(signal.SIGTERM)
                refusal = b"static_refusal cancelled\n"
                assert (
                    read_observed_frame(output.fileno(), len(refusal), deadline)
                    == refusal
                )
                assert child.poll() is None
                assert resource_snapshot() == attached
                assert parent.send(b"C") == 1
                assert child.communicate(timeout=8) == (b"", None)
                assert child.returncode == 1
            finally:
                parent.close()
                child.wait(timeout=8)
    assert resource_snapshot() == baseline
    assert descriptor_snapshot() == descriptors
    assert not ENTRY_MARKER.exists()
    check_cli("late_attachment_reopen", selector, pin=selector, match=True)
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE late_attachment settled=1\n")
    sys.stdout.flush()


def watch_traversal(owned: ExitStack, roots: tuple[int, int]) -> FileIO:
    libc = ctypes.CDLL(None)
    notify_fd = cast("int", libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK))
    assert notify_fd >= 0
    notify = owned.enter_context(FileIO(notify_fd, "rb"))
    assert not os.get_inheritable(notify_fd)
    watched: set[int] = set()
    for root in roots:
        watch = cast(
            "int",
            libc.inotify_add_watch(
                notify_fd,
                os.fsencode(f"/proc/self/fd/{root}"),
                INOTIFY_ACCESS | INOTIFY_OPEN,
            ),
        )
        assert watch >= 0
        assert watch not in watched
        watched.add(watch)
        with os.scandir(root) as scanner:
            assert next(scanner, None) is not None
        observed, mask, _, name_size = cast(
            "tuple[int, int, int, int]",
            INOTIFY_EVENT.unpack(notify.read(INOTIFY_EVENT.size)),
        )
        assert observed == watch
        assert mask & INOTIFY_ACCESS
        assert name_size == 0
        assert not select.select((notify,), (), (), 0)[0]
    return notify


def observe_late_child(descriptor: int) -> int:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    original: subprocess.Popen[bytes] | None = None
    notify: FileIO | None = None
    pipes: list[tuple[int, int]] = []
    reaped = False
    failure = PreparationObservationFailure()
    with ExitStack() as owned:
        gate = owned.enter_context(socket.socket(fileno=descriptor))
        gate.set_inheritable(False)

        def observe(frame: FrameType, event: str, _result: object) -> None:
            nonlocal original, notify, reaped
            if failure.first is not None or event not in {"call", "return"}:
                return
            code = frame.f_code
            caller = frame.f_back
            compare = checker.child_match.__code__
            with failure:
                if code is compare and event == "call":
                    assert notify is None
                    roots = cast("tuple[int, int]", frame.f_locals["roots"])
                    notify = watch_traversal(owned, roots)
                elif caller is not None and caller.f_code is compare:
                    assert code is not checker.write_pipe.__code__
                    if code is subprocess.Popen.__init__.__code__ and event == "return":
                        assert original is None
                        assert notify is not None
                        original = cast(
                            "subprocess.Popen[bytes]", frame.f_locals["self"]
                        )
                        job = cast("checker.Job", caller.f_locals["job"])
                        request = cast("int", caller.f_locals["request"])
                        result = cast("int", caller.f_locals["result"])
                        passed = (
                            request,
                            result,
                            *cast("tuple[int, int]", caller.f_locals["roots"]),
                        )
                        pipes.extend(pipe_identity(os.fstat(fd)) for fd in passed[:2])
                        observe_admitted_child(original, passed, pipes, job.deadline_ns)
                        assert job.active is None
                        assert job.refusal is None
                        assert not select.select((request,), (), (), 0)[0]
                        ready = LATE_CHILD_READY.pack(
                            b"R", original.pid, job.deadline_ns
                        )
                        assert gate.send(ready) == len(ready)
                        release_by = (
                            job.deadline_ns
                            + checker.SETTLEMENT_NS
                            + 2 * OBSERVATION_MARGIN_NS
                        )
                        assert read_observed_frame(gate.fileno(), 1, release_by) == b"C"
                        assert job.refusal == "deadline"
                        assert (
                            time.monotonic_ns() - job.first_refusal
                            >= checker.SETTLEMENT_NS
                        )
                        assert job.active is None
                        assert original.returncode is None
                        assert not select.select((request,), (), (), 0)[0]
                elif code is compare and event == "return":
                    assert original is not None
                    assert notify is not None
                    job = cast("checker.Job", frame.f_locals["job"])
                    assert job.active is None
                    assert job.refusal == "deadline"
                    assert original.returncode == -signal.SIGKILL
                    assert not Path(f"/proc/{original.pid}").exists()
                    assert not set(pipes).intersection(descriptor_snapshot().values())
                    with notify:
                        assert not select.select((notify,), (), (), 0)[0]
                    reaped = True

        threading.setprofile(observe)
        try:
            status = checker.main()
        finally:
            threading.setprofile(None)
    if failure.first is not None:
        raise failure.first
    assert reaped
    assert status == 1
    return status


def late_child_case(selector: str) -> None:
    require_pinned_checker()
    from scripts.release.verify_renderer_installation import DEADLINE_NS, SETTLEMENT_NS

    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    started = time.monotonic_ns()
    ready_by = started + 20_000_000_000
    parent, preparer = socket.socketpair()

    with parent, preparer:
        child = start_observed_preparer(selector, preparer.fileno(), LATE_CHILD_CHANNEL)
        with observe_preparer_settlement(child, parent, before):
            preparer.close()
            output = child.stdout
            assert output is not None
            ready = read_observed_frame(
                parent.fileno(), LATE_CHILD_READY.size, ready_by
            )
            tag, pid, hard_deadline = cast(
                "tuple[bytes, int, int]", LATE_CHILD_READY.unpack(ready)
            )
            now = time.monotonic_ns()
            assert tag == b"R"
            assert now <= ready_by
            assert started + DEADLINE_NS <= hard_deadline <= now + DEADLINE_NS
            assert now < hard_deadline
            proc = Path(f"/proc/{pid}")
            assert proc.exists()
            assert child.poll() is None
            assert not select.select((output,), (), (), 0)[0]
            expected = owner_loss_resources()
            refusal = b"static_refusal deadline\n"
            refuses_from = hard_deadline + SETTLEMENT_NS
            refuses_by = refuses_from + OBSERVATION_MARGIN_NS
            assert (
                read_observed_frame(output.fileno(), len(refusal), refuses_by)
                == refusal
            )
            assert refuses_from <= time.monotonic_ns() <= refuses_by
            assert proc.exists()
            assert child.poll() is None
            assert owner_loss_resources() == expected
            assert parent.send(b"C") == 1
            assert child.communicate(timeout=8) == (b"", None)
            assert child.returncode == 1
            assert not proc.exists()
            assert resource_snapshot() == before

    assert descriptor_snapshot() == descriptors
    assert not ENTRY_MARKER.exists()
    check_cli("late_child_reopen", selector, pin=selector, match=True)
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE late_child no_traversal=1 settled=1\n")
    sys.stdout.flush()


def observe_final_readback(descriptor: int) -> int:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    reaped = challenged = refused = False
    failure = PreparationObservationFailure()
    with socket.socket(fileno=descriptor) as gate:
        gate.set_inheritable(False)

        def observe(frame: FrameType, event: str, result: object) -> None:
            nonlocal reaped, challenged, refused
            if failure.first is not None or refused:
                return
            with failure:
                code = frame.f_code
                if code is checker.child_match.__code__ and event == "return":
                    job = cast("checker.Job", frame.f_locals["job"])
                    assert isinstance(result, tuple)
                    assert job.active is None
                    reaped = True
                elif code is checker.observe.__code__ and reaped:
                    deadline = time.monotonic_ns() + 8_000_000_000
                    if event == "call" and not challenged:
                        assert str(frame.f_locals["path"]).endswith("/verifier")
                        assert gate.send(b"R") == 1
                        assert read_observed_frame(gate.fileno(), 1, deadline) == b"M"
                        challenged = True
                    elif event == "return" and challenged:
                        assert gate.send(b"F") == 1
                        assert read_observed_frame(gate.fileno(), 1, deadline) == b"C"
                        assert result is None
                        refused = True

        threading.setprofile(observe)
        try:
            status = checker.main()
        finally:
            threading.setprofile(None)
    if failure.first is not None:
        raise failure.first
    assert refused
    assert status == 1
    return status


def replace_verity_table(name: str, table: str) -> None:
    command("/usr/sbin/dmsetup", "suspend", "--noflush", name)
    try:
        command("/usr/sbin/dmsetup", "reload", "--readonly", "--table", table, name)
    finally:
        command("/usr/sbin/dmsetup", "resume", name)


def final_readback_case(selector: str) -> None:
    require_pinned_checker()
    baseline = resource_snapshot()
    assert baseline == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    deadline = time.monotonic_ns() + 20_000_000_000
    name = "kora-renderer-verifier"
    parent, preparer = socket.socketpair()
    with parent, preparer:
        child = start_observed_preparer(
            selector, preparer.fileno(), FINAL_READBACK_CHANNEL
        )
        preparer.close()
        try:
            assert read_observed_frame(parent.fileno(), 1, deadline) == b"R"
            owned = owner_loss_resources()
            table = command("/usr/sbin/dmsetup", "table", name).decode("ascii").strip()
            prefix, salt = table.rsplit(" ", 1)
            wrong_salt = "01" * 32
            assert wrong_salt != salt
            changed = f"{prefix} {wrong_salt}"
            try:
                replace_verity_table(name, changed)
                assert (
                    command("/usr/sbin/dmsetup", "table", name)
                    == f"{changed}\n".encode()
                )
                assert owner_loss_resources() == owned
                assert parent.send(b"M") == 1
                assert read_observed_frame(parent.fileno(), 1, deadline) == b"F"
            finally:
                replace_verity_table(name, table)
            assert parent.send(b"C") == 1
            assert child.communicate(timeout=8) == (b"static_refusal verity\n", None)
            assert child.returncode == 1
        finally:
            parent.close()
            child.wait(timeout=8)
            if child.stdout is not None:
                child.stdout.close()
    assert resource_snapshot() == baseline
    assert descriptor_snapshot() == descriptors
    assert not ENTRY_MARKER.exists()
    sys.stdout.write("KORA_RENDERER_FIXTURE_CASE final_readback settled=1\n")
    sys.stdout.flush()


def observe_mount_topology(descriptor: int) -> int:
    require_pinned_checker()
    from scripts.release import verify_renderer_installation as checker

    checked = False
    failure = PreparationObservationFailure()
    with socket.socket(fileno=descriptor) as gate:
        gate.set_inheritable(False)

        def observe(frame: FrameType, event: str, _result: object) -> None:
            nonlocal checked
            if failure.first is not None or event not in {"call", "return"}:
                return
            with failure:
                code = frame.f_code
                assert code is not checker.child_match.__code__
                if code is checker.mounts.__code__:
                    assert not checked
                    caller = frame.f_back
                    assert caller is not None
                    assert caller.f_code is checker.compare.__code__
                    deadline = cast("checker.Job", caller.f_locals["job"]).deadline_ns
                    if event == "call":
                        assert gate.send(b"R") == 1
                        assert read_observed_frame(gate.fileno(), 1, deadline) == b"M"
                    elif event == "return":
                        assert gate.send(b"F") == 1
                        assert read_observed_frame(gate.fileno(), 1, deadline) == b"C"
                        checked = True

        threading.setprofile(observe)
        try:
            status = checker.main()
        finally:
            threading.setprofile(None)
    if failure.first is not None:
        raise failure.first
    assert checked
    assert status == 1
    return status


@dataclass(frozen=True)
class FixtureMountRow:
    number: int
    parent: int
    device: str
    root: Path
    path: Path


def fixture_mount_table() -> dict[int, FixtureMountRow]:
    rows: dict[int, FixtureMountRow] = {}
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        number, parent, dev, root, path, _ = line.split(None, 5)
        mount_id = int(number)
        assert mount_id not in rows
        rows[mount_id] = FixtureMountRow(
            mount_id,
            int(parent),
            dev.decode("ascii"),
            Path(os.fsdecode(root)),
            Path(os.fsdecode(path)),
        )
    return rows


def mountpoint_identity(path: Path) -> tuple[int, str, int]:
    descriptor = os.open(path, DIRECTORY)
    try:
        return root_identity(os.getpid(), descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def topology_bind(source: Path, target: Path) -> Generator[FixtureMountRow, None, None]:
    namespace = namespace_identity(os.getpid())
    source_id, source_dev, source_inode = mountpoint_identity(source)
    target_parent, _, _ = mountpoint_identity(target)
    initial = fixture_mount_table()
    assert all(row.path != target for row in initial.values())
    source_row = initial[source_id]
    assert source_row.device == source_dev
    expected_root = source_row.root / source.relative_to(source_row.path)

    def association(known: FixtureMountRow | None) -> FixtureMountRow | None:
        assert namespace_identity(os.getpid()) == namespace
        current = fixture_mount_table()
        matches = tuple(row for row in current.values() if row.path == target)
        if not matches:
            assert known is None or known.number not in current
            return None
        assert len(matches) == 1
        (row,) = matches
        assert known is None or row == known
        assert row.number not in initial
        assert current.get(source_id) == source_row
        assert current.get(target_parent) == initial[target_parent]
        assert (row.parent, row.root, row.device) == (
            target_parent,
            expected_root,
            source_dev,
        )
        assert mountpoint_identity(source) == (source_id, source_dev, source_inode)
        assert mountpoint_identity(target) == (row.number, source_dev, source_inode)
        return row

    installed: FixtureMountRow | None = None
    try:
        command("/usr/bin/mount", "--bind", str(source), str(target))
        installed = association(None)
        assert installed is not None
        yield installed
    finally:
        attached = association(installed)
        if attached is not None:
            try:
                command("/usr/bin/umount", str(target))
            finally:
                assert association(attached) is None


def mount_topology_case(selector: str, *, nested: bool) -> None:
    require_pinned_checker()
    before = resource_snapshot()
    assert before == (frozenset(), frozenset(), frozenset())
    descriptors = descriptor_snapshot()
    roots = ANCHOR / "roots"
    verifier = roots / "verifier"
    scratch = roots / "topology-fixture"
    scratch.mkdir(mode=0o700)
    source, target = (scratch, verifier / "std") if nested else (verifier, scratch)
    deadline = time.monotonic_ns() + 20_000_000_000

    parent, preparer = socket.socketpair()
    with parent, preparer:
        child = start_observed_preparer(
            selector, preparer.fileno(), MOUNT_TOPOLOGY_CHANNEL
        )
        with observe_preparer_settlement(child, parent, before):
            preparer.close()
            assert read_observed_frame(parent.fileno(), 1, deadline) == b"R"
            expected = owner_loss_resources()
            with topology_bind(source, target) as extra:
                assert extra.number not in {
                    mount[0] for mount in expected.mounts.values()
                }
                roles = {mount[1] for mount in expected.mounts.values()}
                if nested:
                    assert extra.device not in roles
                    assert extra.parent == expected.mounts["verifier"][0]
                else:
                    assert extra.device == expected.mounts["verifier"][1]
                assert owner_loss_resources() == expected
                assert resource_snapshot()[0] == {
                    str(verifier),
                    str(roots / "candidate"),
                    str(target),
                }
                assert parent.send(b"M") == 1
                assert read_observed_frame(parent.fileno(), 1, deadline) == b"F"
            assert owner_loss_resources() == expected
            assert parent.send(b"C") == 1
            assert child.communicate(timeout=8) == (b"static_refusal root\n", None)
            assert child.returncode == 1
            assert resource_snapshot() == before

    scratch.rmdir()
    assert descriptor_snapshot() == descriptors
    assert not ENTRY_MARKER.exists()
    label = "nested_mount" if nested else "overlapping_mount"
    sys.stdout.write(f"KORA_RENDERER_FIXTURE_CASE {label} settled=1\n")
    sys.stdout.flush()


def main(args: Sequence[str]) -> None:
    assert tuple(args) in {
        ("base",),
        ("content",),
        ("geometry",),
        ("handoff",),
        ("bounds",),
        ("settlement",),
        ("success_order",),
        ("identity_guard",),
        ("owner_loss",),
        ("retained_busy",),
        ("unreadable",),
        ("late_attachment",),
        ("late_child",),
        ("final_readback",),
        ("mount_topology",),
    }
    require_guest()
    if args[0] == "bounds":
        bounds_cases()
        return
    ENTRY_MARKER.parent.parent.mkdir(mode=0o711)
    ENTRY_MARKER.parent.parent.chmod(0o711)
    ENTRY_MARKER.parent.mkdir(mode=0o700)
    os.chown(ENTRY_MARKER.parent, 65532, 65532)
    ANCHOR.mkdir(mode=0o700)
    for path in (
        "registry",
        "registry/deployments",
        "registry/inventories",
        "media",
        "roots",
    ):
        (ANCHOR / path).mkdir(mode=0o700)
    verifier = make_volume("verifier")
    if args[0] == "geometry":
        geometry_cases(verifier)
    elif args[0] == "settlement":
        busy_unmount_case(verifier)
    elif args[0] == "identity_guard":
        wrong_mount_identity_case(verifier, make_volume("candidate"))
    elif args[0] == "unreadable":
        candidate = make_volume("candidate", unreadable=True)
        selector = provision(verifier, candidate)
        unreadable_entry_case(candidate, selector)
    else:
        candidate = make_volume("candidate")
        selector = provision(verifier, candidate)
        if args[0] == "base":
            base_cases(selector)
        elif args[0] == "handoff":
            inherited_root_cases(verifier, candidate, selector)
        elif args[0] == "success_order":
            successful_preparation_case(selector)
        elif args[0] == "owner_loss":
            owner_loss_case(selector)
        elif args[0] == "retained_busy":
            retained_busy_case(selector)
        elif args[0] == "late_attachment":
            late_attachment_case(selector)
        elif args[0] == "late_child":
            late_child_case(selector)
        elif args[0] == "final_readback":
            final_readback_case(selector)
        elif args[0] == "mount_topology":
            mount_topology_case(selector, nested=False)
            mount_topology_case(selector, nested=True)
            check_cli("mount_topology_reopen", selector, pin=selector, match=True)
        else:
            content_cases(verifier, candidate, selector)
    sys.stdout.write(f"KORA_RENDERER_FIXTURE_PASS suite={args[0]}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "unreadable_probe":
        unreadable_probe(int(sys.argv[2]))
    elif len(sys.argv) == 2 and re.fullmatch(r"[0-9a-f]{64}", sys.argv[1]):
        owner_channel = os.environ.get(OWNER_LOSS_CHANNEL)
        if owner_channel is not None:
            observe_owner_loss(int(owner_channel))
        busy_channel = os.environ.get(RETAINED_BUSY_CHANNEL)
        if busy_channel is not None:
            observe_retained_busy(int(busy_channel))
        late_channel = os.environ.get(LATE_ATTACHMENT_CHANNEL)
        if late_channel is not None:
            raise SystemExit(observe_late_attachment(int(late_channel)))
        late_child_channel = os.environ.get(LATE_CHILD_CHANNEL)
        if late_child_channel is not None:
            raise SystemExit(observe_late_child(int(late_child_channel)))
        final_channel = os.environ.get(FINAL_READBACK_CHANNEL)
        if final_channel is not None:
            raise SystemExit(observe_final_readback(int(final_channel)))
        topology_channel = os.environ.get(MOUNT_TOPOLOGY_CHANNEL)
        if topology_channel is not None:
            raise SystemExit(observe_mount_topology(int(topology_channel)))
        raise SystemExit(observe_successful_preparation())
    else:
        main(sys.argv[1:])
