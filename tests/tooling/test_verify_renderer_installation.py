import errno
import os
import select
import signal
import subprocess
import sys
import time
from array import array
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from hashlib import file_digest, sha256
from io import BytesIO, FileIO
from itertools import chain
from pathlib import Path
from struct import pack, unpack
from tempfile import TemporaryFile
from types import BuiltinMethodType, FrameType
from typing import cast

import pytest

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from scripts.release.renderer_installation_check import (
    DOMAIN,
    PACK,
    READ_REFUSAL_EXIT,
    Index,
    Refusal,
    deployment,
    frame_size,
    open_directory,
)
from scripts.release.verify_renderer_installation import (
    CONTROL_SCRATCH,
    DIRECTORY_SCRATCH,
    JSON_SCRATCH,
    Job,
    compare,
    length,
    read_exact,
    read_pipe,
    receive,
    reserve,
    transfer,
)
from tests.support.observability import ProcessResult, run_python

SELECTOR = "e6868b5c2a9acac05d0b8064833261f8428d43a64564405e754dfad635f68e2d"
VERIFIER_INVENTORY = "8d09374f70f88d52b4e32c8bcbd1928e49ed73f539940e8746d4ba6349ce9960"
CANDIDATE_INVENTORY = "8b5cad86a06e8e08b7aad62ecd8306e0c4f36ce0189e1dfa8564b2b9053a0c4d"
LONG_INVENTORY_DIRECTORY = "/".join(("y" * 254,) * 4)


def test_invalid_selector_emits_one_content_free_refusal() -> None:
    sentinel = "RENDERER-REFUSAL-TEST-MARKER"
    arguments = ["-B", "-m", "scripts.release.verify_renderer_installation", sentinel]

    result = run_python(arguments, {})

    assert result.code == 1
    assert result.stdout == b"static_refusal document\n"
    assert sentinel.encode("ascii") not in result.stderr
    assert b"Traceback" not in result.stderr
    records = result.records()
    assert len(records) == 1
    record = records[0]
    expected = {
        "schema_version": 1,
        "level": "WARNING",
        "event": "release.renderer_preparation.refused",
        "component": "release.renderer_preparation",
        "logger": "kora.release.renderer_preparation",
        "phase": "preparation",
        "reason": "document",
        "entries": 0,
        "bytes": 0,
    }
    assert record.keys() == expected.keys() | {
        "timestamp",
        "message",
        "instance_id",
        "settled",
    }
    assert {field: record[field] for field in expected} == expected
    assert type(record["entries"]) is int
    assert type(record["bytes"]) is int
    assert record["settled"] is True


@pytest.mark.parametrize("error_type", ["ArithmeticError", "GeneratorExit"])
def test_unexpected_process_exception_exits_with_one_safe_record(
    error_type: str,
) -> None:
    source = """
import signal
import sys

from scripts.release.verify_renderer_installation import main

def unexpected(signum: int, frame: object) -> None:
    try:
        raise LookupError("RENDERER-PRIVATE-CAUSE")
    except LookupError as error:
        raise ERROR_TYPE("RENDERER-PRIVATE-ERROR") from error

signal.signal(signal.SIGUSR1, unexpected)
sys.exit(main())
""".replace("ERROR_TYPE", error_type)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as output, os.fdopen(writer, "wb") as stdout:
        os.set_blocking(writer, False)
        written = 0
        for width in (4096, 1):
            payload = b"x" * width
            while True:
                try:
                    written += os.write(writer, payload)
                except BlockingIOError:
                    break
        os.set_blocking(writer, True)

        with subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
        ) as child:
            stdout.close()
            try:
                assert child.stderr is not None
                assert select.select((child.stderr,), (), (), 5)[0]
                refusal = child.stderr.readline()
                assert b'"event":"release.renderer_preparation.refused"' in refusal
                child.send_signal(signal.SIGUSR1)
                _, diagnostics = child.communicate(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
        result = ProcessResult(child.returncode, output.read(), refusal + diagnostics)

    assert result.code == 70
    assert result.stdout == b"x" * written
    assert b"Traceback" not in result.stderr
    assert b"RENDERER-PRIVATE" not in result.stderr
    _, failed = result.records()
    assert failed["event"] == "process.failed"
    assert failed["component"] == "release.renderer_preparation"
    assert failed["level"] == "CRITICAL"
    assert failed["exit_code"] == 70
    exception_chain = cast("list[dict[str, object]]", failed["exception"])
    assert [entry["type"] for entry in exception_chain] == [error_type, "LookupError"]
    assert all(
        entry.keys() == {"type", "module", "frames"} for entry in exception_chain
    )
    frames = cast("list[dict[str, object]]", exception_chain[0]["frames"])
    assert frames[-1]["function"] == "unexpected"
    assert all(frame.keys() == {"module", "function", "line"} for frame in frames)


def root_document(inventory_digest: str, root_hash: str, salt: str) -> JsonObject:
    return {
        "inventory_sha256": inventory_digest,
        "entry": "entry.py",
        "interpreter": "bin/python",
        "elf_loader": "lib/ld.so",
        "stdlib": "std",
        "verity": {"data_blocks": 2048, "root_hash": root_hash, "salt": salt},
    }


def golden_manifest() -> bytes:
    return canonical_bytes(
        {
            "revision": 1,
            "base": {
                "release": "ubuntu-24.04-amd64-20261017",
                "image_sha256": (
                    "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
                ),
                "kernel_release": "6.8.0-147-generic",
                "kernel_sha256": "2" * 64,
                "initrd_sha256": "1" * 64,
            },
            "inputs": {
                "cpython_sha256": (
                    "3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522"
                ),
                "jinja_sha256": (
                    "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67"
                ),
                "markupsafe_sha256": (
                    "6da83a088f8ef93b2d483a8232a4dbf4d69d3d8496b568a03c56becac43e1808"
                ),
            },
            "verifier": root_document(VERIFIER_INVENTORY, "3" * 64, "4" * 64),
            "candidate": root_document(CANDIDATE_INVENTORY, "5" * 64, "6" * 64),
        }
    )


def inventory_row(path: str, content: bytes) -> bytes:
    return canonical_bytes(
        {
            "path": path,
            "sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )


def golden_inventory(entry: bytes, extra_entries: int = 0) -> bytes:
    files = chain(
        (("bin/python", b"p"), ("entry.py", entry), ("lib/ld.so", b"l")),
        ((f"std/extra-{number:04d}.py", b"") for number in range(extra_entries)),
        (("std/x.py", b"s"),),
    )
    rows = (inventory_row(path, content) for path, content in files)
    return b"[" + b",".join(rows) + b"]"


def stabilize_fixture_access_time(path: Path) -> None:
    metadata = path.stat()
    recent = max(time.time_ns(), metadata.st_mtime_ns, metadata.st_ctime_ns)
    access_ns = recent + 86_400 * 10**9
    os.utime(path, ns=(access_ns, metadata.st_mtime_ns))


def test_exact_v1_golden_manifest_and_both_inventories() -> None:
    manifest = golden_manifest()
    verifier = golden_inventory(b"v")
    candidate = golden_inventory(b"c")
    verifier_source, candidate_source = BytesIO(verifier), BytesIO(candidate)
    index = Index()

    prepared = deployment(manifest, SELECTOR)
    verifier_start, verifier_end = index.inventory(
        verifier_source, len(verifier), prepared.roots[0]
    )
    candidate_start, candidate_end = index.inventory(
        candidate_source, len(candidate), prepared.roots[1]
    )

    assert len(manifest) == 1349
    assert len(verifier) == len(candidate) == 448
    assert prepared.boot == ("2" * 64, "1" * 64)
    assert prepared.roots[0].inventory == VERIFIER_INVENTORY
    assert prepared.roots[1].inventory == CANDIDATE_INVENTORY
    assert verifier_end - verifier_start == 4
    assert candidate_end - candidate_start == 4


def test_duplicate_manifest_key_refuses_with_matching_selector() -> None:
    manifest_with_duplicate_revision = b'{"revision":1,' + golden_manifest()[1:]
    selector = sha256(manifest_with_duplicate_revision).hexdigest()

    with pytest.raises(Refusal, match="^document$"):
        deployment(manifest_with_duplicate_revision, selector)


@pytest.mark.parametrize(
    ("fragment", "replacement"),
    [
        pytest.param(b'"revision":1', b'"revision":1,"unknown":0', id="unknown-key"),
        pytest.param(b'"revision":1', b'"revision":true', id="boolean-revision"),
        pytest.param(b'"revision":1', b'"revision": 1', id="alternate-encoding"),
        pytest.param(b'"entry.py"', b'"./entry.py"', id="path-alias"),
        pytest.param(b'"entry.py"', b'"entry.\xff"', id="invalid-utf8"),
    ],
)
def test_manifest_refuses_forbidden_bytes_with_matching_selector(
    fragment: bytes, replacement: bytes
) -> None:
    manifest = golden_manifest().replace(fragment, replacement, 1)
    selector = sha256(manifest).hexdigest()

    with pytest.raises(Refusal):
        deployment(manifest, selector)


def manifest_for_candidate(candidate: FileIO) -> bytes:
    candidate.seek(0)
    digest = file_digest(candidate, lambda: sha256(DOMAIN)).hexdigest()
    candidate.seek(0)
    return golden_manifest().replace(CANDIDATE_INVENTORY.encode(), digest.encode())


def test_canonical_inventory_streams_at_combined_document_bound(tmp_path: Path) -> None:
    maximum_document_bytes = 4 * 1024 * 1024
    metadata_size = 512
    long_path_rows = 3_719
    first_long_path = f"{LONG_INVENTORY_DIRECTORY}/0000"
    last_path = "/".join(("z" * 214, "z" * 214, "z" * 213))
    verifier_path = tmp_path / "verifier.json"
    verifier_path.write_bytes(golden_inventory(b"v"))
    index = Index()
    reads: list[int] = []
    candidate_offset = 0

    def observe(_frame: FrameType, event: str, function: object) -> None:
        nonlocal candidate_offset
        if (
            event == "c_return"
            and isinstance(function, BuiltinMethodType)
            and function.__name__ == "read"
            and function.__self__ is candidate
        ):
            offset = candidate.tell()
            reads.append(offset - candidate_offset)
            candidate_offset = offset

    with (
        (tmp_path / "deployment.json").open("w+b", buffering=0) as manifest,
        verifier_path.open("rb", buffering=0) as verifier,
        (tmp_path / "candidate.json").open("w+b", buffering=0) as candidate,
    ):
        candidate.write(golden_inventory(b"c")[:-1])
        for number in range(long_path_rows):
            candidate.write(
                b"," + inventory_row(f"{LONG_INVENTORY_DIRECTORY}/{number:04d}", b"")
            )
        tail = candidate.tell()
        candidate.write(b"," + inventory_row(last_path, b"") + b"]")
        document = manifest_for_candidate(candidate)
        manifest.write(document)
        manifest_size = length(manifest.fileno())
        verifier_size = length(verifier.fileno())
        candidate_size = length(candidate.fileno())
        accepted = (manifest_size, verifier_size, candidate_size)

        allowed = frame_size(metadata_size, accepted)
        manifest.seek(0)
        roots = deployment(manifest.read(), sha256(document).hexdigest()).roots
        first = index.inventory(verifier, verifier_size, roots[0])
        profile = cast(
            "Callable[[FrameType, str, object], object] | None", sys.getprofile()
        )
        sys.setprofile(observe)
        try:
            second = index.inventory(candidate, candidate_size, roots[1])
        finally:
            sys.setprofile(profile)
        candidate.seek(tail)
        candidate.write(b"," + inventory_row(last_path + "z", b"") + b"]")
        manifest.seek(0)
        manifest.write(manifest_for_candidate(candidate))
        over_limit = (
            length(manifest.fileno()),
            verifier_size,
            length(candidate.fileno()),
        )
        with pytest.raises(Refusal, match="^capacity$"):
            frame_size(metadata_size, over_limit)

    assert sum(accepted) == maximum_document_bytes
    assert sum(over_limit) == maximum_document_bytes + 1
    assert unpack("!5I", allowed) == (
        maximum_document_bytes + metadata_size + 16,
        metadata_size,
        manifest_size,
        verifier_size,
        candidate_size,
    )
    assert first == (0, 4)
    assert second == (4, 3_728)
    assert sum(reads) == candidate_size
    assert all(0 < size <= 32 * 1024 for size in reads)
    assert len(first_long_path) == 1024
    assert index.path(8) == first_long_path.encode()
    assert index.path(second[1] - 1) == last_path.encode()
    assert index.used == 3_808_969


def test_inventory_refuses_path_over_byte_bound_before_packing() -> None:
    path = f"{LONG_INVENTORY_DIRECTORY}/00000"
    inventory = golden_inventory(b"c")[:-1] + b"," + inventory_row(path, b"") + b"]"
    source = BytesIO(inventory)
    candidate = replace(
        deployment(golden_manifest(), SELECTOR).roots[1],
        inventory=sha256(DOMAIN + inventory).hexdigest(),
    )
    index = Index()

    with pytest.raises(Refusal, match="^path$"):
        index.inventory(source, len(inventory), candidate)

    assert len(path) == 1025
    assert index.count == 4
    assert index.used == 35
    assert index.records[4 * PACK.size : 5 * PACK.size] == bytes(PACK.size)
    assert not any(index.paths[35 : 35 + len(path)])


def test_index_refuses_insufficient_budget_before_allocating_arenas() -> None:
    ceiling = 8 * 1024 * 1024
    maximum_entries = 4096
    arenas = (maximum_entries * 1024, maximum_entries * 46, maximum_entries)
    pending_bytes = maximum_entries * 2 * array("I").itemsize
    needed = sum(arenas) + pending_bytes
    rejected = Index.__new__(Index)

    accepted = Index(budget=needed)
    with pytest.raises(Refusal, match="^capacity$"):
        rejected.__init__(budget=needed - 1)
    with pytest.raises(Refusal, match="^capacity$"):
        rejected.__init__(budget=ceiling + 1)

    assert (len(accepted.paths), len(accepted.records), len(accepted.found)) == arenas
    assert not vars(rejected)


def test_reservation_refuses_insufficient_budget_at_admission() -> None:
    ceiling = 16 * 1024 * 1024
    pipe_bytes = 16 * os.sysconf("SC_PAGESIZE")
    scratch = JSON_SCRATCH + DIRECTORY_SCRATCH + CONTROL_SCRATCH
    needed = 12 * 1024 * 1024 + scratch + 2 * pipe_bytes

    default = reserve()
    accepted = reserve(budget=needed)
    with pytest.raises(Refusal, match="^capacity$"):
        reserve(budget=needed - 1)
    with pytest.raises(Refusal, match="^capacity$"):
        reserve(budget=ceiling + 1)

    assert default == accepted == pipe_bytes


@pytest.mark.parametrize("additional_entry", [0, 1], ids=["at-bound", "over-bound"])
def test_inventory_entry_limit_is_shared_across_roots(
    tmp_path: Path, additional_entry: int
) -> None:
    maximum_entries = 4096
    index = Index()
    verifier, candidate = deployment(golden_manifest(), SELECTOR).roots
    verifier_bytes = golden_inventory(b"v")
    candidate_bytes = golden_inventory(b"c", maximum_entries - 8 + additional_entry)
    inventory_digest = sha256(DOMAIN + candidate_bytes).hexdigest()
    candidate = replace(candidate, inventory=inventory_digest)
    verifier_path = tmp_path / "verifier.json"
    candidate_path = tmp_path / "candidate.json"
    verifier_path.write_bytes(verifier_bytes)
    candidate_path.write_bytes(candidate_bytes)
    expected_bounds = None if additional_entry else (4, maximum_entries)
    second = None

    with verifier_path.open("rb") as source:
        first = index.inventory(source, length(source.fileno()), verifier)
    with candidate_path.open("rb") as source:
        if additional_entry:
            with pytest.raises(Refusal, match="^capacity$"):
                index.inventory(source, length(source.fileno()), candidate)
        else:
            second = index.inventory(source, length(source.fileno()), candidate)

    assert first == (0, 4)
    assert second == expected_bounds
    assert index.count == maximum_entries
    assert index.path(0) == b"bin/python"


def test_file_byte_limit_counts_verified_files_and_refuses_next_byte(
    tmp_path: Path,
) -> None:
    maximum_file_bytes = 256 * 1024 * 1024
    files = ((b"large", maximum_file_bytes - 1), (b"last", 1), (b"overflow", 1))
    index = Index()
    for name, size in files:
        path = tmp_path / os.fsdecode(name)
        with path.open("w+b") as source:
            source.truncate(size)
            digest = file_digest(source, "sha256").digest()
        stabilize_fixture_access_time(path)
        index.add(name, size, digest)

    with ExitStack() as owned:
        root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        owned.callback(os.close, root)

        for item, (name, _) in enumerate(files[:2]):
            saved = os.stat(name, dir_fd=root, follow_symlinks=False)
            index.file(root, name, saved, item)
        name, _ = files[2]
        saved = os.stat(name, dir_fd=root, follow_symlinks=False)
        with pytest.raises(Refusal, match="^capacity$"):
            index.file(root, name, saved, 2)

    assert index.hashed == maximum_file_bytes
    assert index.found[:3] == b"\x01\x01\x00"


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        pytest.param("missing", "entry.py", id="missing-listed-file"),
        pytest.param("regular", "additional.py", id="unlisted-file"),
        pytest.param("directory", "additional", id="unlisted-directory"),
        pytest.param("symlink", "entry.py", id="listed-symlink"),
        pytest.param("fifo", "entry.py", id="listed-fifo"),
    ],
)
def test_walk_refuses_tree_divergence(tmp_path: Path, kind: str, name: str) -> None:
    expected = b"original"
    tree = tmp_path / "root"
    tree.mkdir()
    entry = tree / name
    has_extra_entry = kind in ("regular", "directory")
    if has_extra_entry:
        listed_file = tree / "entry.py"
        listed_file.write_bytes(expected)
        stabilize_fixture_access_time(listed_file)
    if kind == "regular":
        entry.write_bytes(b"unlisted bytes")
    elif kind == "directory":
        entry.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.py"
        target.write_bytes(expected)
        entry.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(entry)
    index = Index()
    index.add(b"entry.py", len(expected), sha256(expected).digest())

    with ExitStack() as owned:
        root = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
        owned.callback(os.close, root)
        deadline = time.monotonic_ns() + 15_000_000_000

        with pytest.raises(Refusal, match="^tree$"):
            index.walk(root, 0, (0, index.count), deadline)

    if has_extra_entry:
        assert index.found[0] in (0, 1)
        assert index.hashed == len(expected) * index.found[0]
    else:
        assert index.hashed == 0
        assert not index.found[0]


@pytest.mark.parametrize(
    ("content", "hardlink"),
    [
        pytest.param(b"original", True, id="hardlink"),
        pytest.param(b"original plus", False, id="wrong-length"),
        pytest.param(b"modified", False, id="same-length-digest-mismatch"),
    ],
)
def test_walk_refuses_divergent_listed_file(
    tmp_path: Path, content: bytes, hardlink: bool
) -> None:
    expected = b"original"
    tree = tmp_path / "root"
    tree.mkdir()
    entry = tree / "entry.py"
    entry.write_bytes(content)
    if hardlink:
        os.link(entry, tmp_path / "alias.py")
    stabilize_fixture_access_time(entry)
    index = Index()
    index.add(b"entry.py", len(expected), sha256(expected).digest())

    with ExitStack() as owned:
        root = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
        owned.callback(os.close, root)
        deadline = time.monotonic_ns() + 15_000_000_000

        with pytest.raises(Refusal, match="^file$"):
            index.walk(root, 0, (0, index.count), deadline)

    assert index.examined == 1
    assert index.hashed == 0
    assert not index.found[0]


def test_wide_root_walk_observes_exact_scanners_reopens_and_file_reads(
    tmp_path: Path,
) -> None:
    entry_limit = 4096
    content = b"one chunk"
    digest = sha256(content).digest()
    index = Index()
    for item in range(entry_limit):
        name = f"entry-{item:04d}"
        path = tmp_path / name
        path.write_bytes(content)
        stabilize_fixture_access_time(path)
        index.add(os.fsencode(name), len(content), digest)
    calls: Counter[str] = Counter()
    reads: Counter[int] = Counter()

    def observe(frame: FrameType, event: str, function: object) -> None:
        if event != "c_call":
            return
        if frame.f_code is Index.walk.__code__ and function is os.scandir:
            calls["scanner"] += 1
        if frame.f_code is open_directory.__code__ and function is os.dup:
            calls["held_root_reopen"] += 1
        if frame.f_code is open_directory.__code__ and function is os.open:
            calls["descendant_open"] += 1
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

    with ExitStack() as owned:
        root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        owned.callback(os.close, root)
        original = os.fstat(root)
        deadline = time.monotonic_ns() + 15_000_000_000
        profile = cast(
            "Callable[[FrameType, str, object], object] | None", sys.getprofile()
        )

        sys.setprofile(observe)
        try:
            index.walk(root, 0, (0, entry_limit), deadline)
        finally:
            sys.setprofile(profile)
        retained = os.fstat(root)

    assert index.examined == entry_limit
    assert index.hashed == entry_limit * len(content)
    assert calls == {"scanner": 1, "held_root_reopen": 1, "file_open": entry_limit}
    assert reads == {item: 2 for item in range(entry_limit)}
    assert (retained.st_dev, retained.st_ino) == (original.st_dev, original.st_ino)


def test_walk_refuses_inherited_deadline_before_inspecting_held_root(
    tmp_path: Path,
) -> None:
    payload = b"observed only on traversal"
    (tmp_path / "entry.py").write_bytes(payload)
    index = Index()
    index.add(b"entry.py", len(payload), sha256(payload).digest())

    with ExitStack() as owned:
        root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        owned.callback(os.close, root)
        original_root = os.fstat(root)
        inherited_deadline = time.monotonic_ns()

        with pytest.raises(Refusal, match="^deadline$"):
            index.walk(root, 0, (0, 1), inherited_deadline)
        retained_root = os.fstat(root)

    assert index.examined == index.hashed == 0
    assert not index.found[0]
    assert retained_root == original_root


def test_compare_records_preflight_refusal_before_later_cancellation() -> None:
    selector = "f" * 64
    if os.environ.get("KORA_PREPARATION_SELECTOR") == selector:
        selector = SELECTOR
    job = Job()

    with pytest.raises((Refusal, OSError)) as failure:
        compare(job, selector)
    cause = failure.value
    reason = str(cause) if isinstance(cause, Refusal) else "unavailable"
    refusal_time = job.first_refusal
    job.interrupt(signal.SIGTERM, None)

    assert reason in {"boot", "document", "root", "selector", "unavailable"}
    with pytest.raises(Refusal, match=f"^{reason}$"):
        job.finish()
    assert job.first_refusal == refusal_time > 0


def test_job_records_input_refusal_before_cleanup_observes_cancellation(
    tmp_path: Path,
) -> None:
    job = Job()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def check_input() -> None:
        with job.resources() as owned:
            owned.callback(os.close, fd)
            owned.callback(job.interrupt, signal.SIGTERM, None)
            length(fd)

    with pytest.raises(Refusal, match="^input$"):
        check_input()

    with pytest.raises(Refusal, match="^input$"):
        job.finish()
    assert job.first_refusal > 0
    with pytest.raises(OSError, match=rf"\[Errno {errno.EBADF}\]"):
        os.fstat(fd)


def test_job_records_close_failure_before_later_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "owned"
    path.touch()
    job = Job()
    fd = os.open(path, os.O_RDONLY)
    bad_descriptor = rf"\[Errno {errno.EBADF}\]"

    def release_descriptors() -> None:
        with job.resources() as owned:
            owned.callback(os.close, fd)
            owned.callback(os.close, -1)

    with pytest.raises(OSError, match=bad_descriptor):
        release_descriptors()
    refusal_time = job.first_refusal
    job.interrupt(signal.SIGTERM, None)

    with pytest.raises(Refusal, match="^unavailable$"):
        job.finish()
    assert job.first_refusal == refusal_time > 0
    with pytest.raises(OSError, match=bad_descriptor):
        os.fstat(fd)


def test_refusal_before_activation_leaves_incomplete_frame_and_eof() -> None:
    frame = b"fixture-frame!"
    with TemporaryFile(buffering=0) as staged, ExitStack() as channels:
        staged.write(frame[-1:])
        reader_fd, writer_fd = os.pipe()
        reader = channels.enter_context(os.fdopen(reader_fd, "rb", buffering=0))
        writer = channels.enter_context(os.fdopen(writer_fd, "wb", buffering=0))
        os.set_blocking(writer_fd, False)
        job = Job()

        os.write(writer_fd, frame[:-1])
        job.interrupt(signal.SIGTERM, None)
        with pytest.raises(Refusal, match="^cancelled$"):
            transfer(job, staged.fileno(), 1, writer_fd, last=True)
        writer.close()
        received = reader.read(len(frame))
        eof = reader.read(1)

    assert received == frame[:-1]
    assert eof == b""
    assert job.refusal == "cancelled"


def test_definite_malformed_response_refuses_before_original_child_is_reaped() -> None:
    with ExitStack() as channels:
        reader_fd, result_fd = os.pipe()
        hold_fd, release_fd = os.pipe()
        reader = channels.enter_context(os.fdopen(reader_fd, "rb", buffering=0))
        result = channels.enter_context(os.fdopen(result_fd, "wb", buffering=0))
        hold = channels.enter_context(os.fdopen(hold_fd, "rb", buffering=0))
        release = channels.enter_context(os.fdopen(release_fd, "wb", buffering=0))
        os.set_blocking(reader.fileno(), False)
        job = Job()
        source = (
            "import os, sys; "
            "os.write(int(sys.argv[1]), b'\\xff' * 4); "
            "os.read(int(sys.argv[2]), 1)"
        )

        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                source,
                str(result_fd),
                str(hold_fd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(result_fd, hold_fd),
            env={"LC_ALL": "C"},
        ) as child:
            result.close()
            hold.close()
            with job.own(child):
                try:
                    with pytest.raises(Refusal, match="^channel$"), job.checked():
                        receive(job, child, reader.fileno(), "0" * 64, SELECTOR)
                    first_refusal = job.first_refusal
                    retained = job.active is child
                    outstanding = child.poll() is None
                finally:
                    release.close()
        job.interrupt(signal.SIGTERM, None)
        job.observe()

    assert retained
    assert outstanding
    assert child.returncode == 0
    assert job.active is None
    assert job.refusal == "channel"
    assert job.first_refusal == first_refusal > 0


@pytest.mark.parametrize(
    ("suffix", "exit_status", "reason"),
    [(b"x", 0, "channel"), (b"", 17, "child")],
    ids=["trailing-byte", "nonzero-exit"],
)
def test_complete_reply_refuses_trailing_input_or_failed_child(
    suffix: bytes, exit_status: int, reason: str
) -> None:
    correlation = "0" * 64
    payload = canonical_bytes(
        {"job": correlation, "selector": SELECTOR, "entries": 1, "bytes": 9}
    )
    frame = pack("!I", len(payload)) + payload + suffix
    source = (
        "import os, sys; "
        "fd = int(sys.argv[1]); frame = bytes.fromhex(sys.argv[2]); "
        "assert os.write(fd, frame) == len(frame); os.close(fd); "
        "sys.exit(int(sys.argv[3]))"
    )
    job = Job()
    with ExitStack() as channels:
        reader_fd, result_fd = os.pipe()
        reader = channels.enter_context(os.fdopen(reader_fd, "rb", buffering=0))
        result = channels.enter_context(os.fdopen(result_fd, "wb", buffering=0))
        os.set_blocking(reader.fileno(), False)
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                source,
                str(result_fd),
                frame.hex(),
                str(exit_status),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(result_fd,),
            env={"LC_ALL": "C"},
        ) as child:
            result.close()

            with pytest.raises(Refusal, match=f"^{reason}$"), job.own(child):
                receive(job, child, reader.fileno(), correlation, SELECTOR)
            first_refusal = job.first_refusal
            job.interrupt(signal.SIGTERM, None)
            job.observe()

            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
            assert child.returncode == exit_status
            assert job.active is None
            assert job.refusal == reason
            assert job.first_refusal == first_refusal > 0

    assert reader.closed
    assert result.closed


def test_empty_result_waits_for_the_original_child_exit_to_classify_refusal() -> None:
    source = (
        "import os, signal, sys; signal.alarm(3); os.close(1); "
        "assert os.read(0, 1) == b''; sys.exit(int(sys.argv[1]))"
    )
    job = Job()
    observations: list[tuple[bool, int | None, str | None, int]] = []
    with subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", source, str(READ_REFUSAL_EXIT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"LC_ALL": "C"},
    ) as child:
        reader, hold = child.stdout, child.stdin
        assert reader is not None
        assert hold is not None
        os.set_blocking(reader.fileno(), False)

        def observe(stack: FrameType, event: str, argument: object) -> None:
            if (
                not observations
                and event == "c_call"
                and stack.f_code is Job.settle_result.__code__
                and argument is select.select
            ):
                observations.append(
                    (
                        job.active is child,
                        child.returncode,
                        job.refusal,
                        job.first_refusal,
                    )
                )
                hold.close()

        prior_profile = cast(
            "Callable[[FrameType, str, object], object] | None", sys.getprofile()
        )

        with ExitStack() as observers:
            sys.setprofile(observe)
            observers.callback(sys.setprofile, prior_profile)
            with pytest.raises(Refusal, match="^read$"), job.own(child):
                receive(job, child, reader.fileno(), "0" * 64, SELECTOR)
        first_refusal = job.first_refusal
        job.interrupt(signal.SIGTERM, None)
        job.observe()

        assert observations == [(True, None, None, 0)]
        assert child.returncode == READ_REFUSAL_EXIT
        assert job.active is None
        assert reader.closed
        assert hold.closed
        assert job.refusal == "read"
        assert job.first_refusal == first_refusal > 0


def test_cancel_while_complete_reply_awaits_owned_child_eof() -> None:
    correlation = "0" * 64
    payload = canonical_bytes(
        {"job": correlation, "selector": SELECTOR, "entries": 1, "bytes": 9}
    )
    frame = pack("!I", len(payload)) + payload
    source = (
        "import os, signal, sys; "
        "signal.alarm(15); frame = bytes.fromhex(sys.argv[1]); "
        "assert os.write(1, frame) == len(frame); os.read(0, 1)"
    )
    reply_read = False
    awaiting_eof = False
    first_refusal = 0
    job = Job()

    def observe(stack: FrameType, event: str, argument: object) -> None:
        nonlocal reply_read, awaiting_eof, first_refusal
        if (
            event == "return"
            and stack.f_code is read_exact.__code__
            and argument == payload
        ):
            reply_read = True
        elif (
            reply_read
            and not awaiting_eof
            and event == "c_call"
            and stack.f_code is read_pipe.__code__
            and argument is select.select
        ):
            awaiting_eof = True
            signal.raise_signal(signal.SIGTERM)
        elif event == "return" and stack.f_code is receive.__code__:
            first_refusal = job.first_refusal

    with subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", source, frame.hex()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"LC_ALL": "C"},
    ) as child:
        reader, hold = child.stdout, child.stdin
        assert reader is not None
        assert hold is not None
        os.set_blocking(reader.fileno(), False)

        with ExitStack() as observers:
            prior_handler = signal.signal(signal.SIGTERM, job.interrupt)
            observers.callback(signal.signal, signal.SIGTERM, prior_handler)
            prior_profile = cast(
                "Callable[[FrameType, str, object], object] | None", sys.getprofile()
            )
            sys.setprofile(observe)
            observers.callback(sys.setprofile, prior_profile)
            with pytest.raises(Refusal, match="^cancelled$"), job.own(child):
                receive(job, child, reader.fileno(), correlation, SELECTOR)

        assert reply_read
        assert awaiting_eof
        assert child.returncode == -signal.SIGTERM
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert reader.closed
        assert hold.closed
        assert job.active is None
        assert job.refusal == "cancelled"
        assert job.first_refusal == first_refusal > 0
