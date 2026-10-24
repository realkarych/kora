import asyncio
import errno
import gc
import hashlib
import os
import resource
import stat
import sys
import threading
import tracemalloc
from pathlib import Path

from tests.measurements.harness import ExtraValue
from tests.plugins.storage.inspection import InspectedStorage
from tests.plugins.storage.pressure_native import command, process_token


def host_process_identity() -> tuple[str, str]:
    token = process_token(os.getpid())
    if token is None:
        raise AssertionError("the running measurement process has no native identity")
    boot = (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if sys.platform == "linux"
        else command(("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"))
        .decode()
        .strip()
    )
    return boot.lower(), token.identity


def rss_bytes() -> int:
    if sys.platform == "linux":
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    if sys.platform == "darwin":
        return int(command(("/bin/ps", "-o", "rss=", "-p", str(os.getpid())))) * 1024
    raise RuntimeError("storage measurements require Linux or macOS")


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [
        *root.glob("src/kora/plugins/storage/*.py"),
        *root.glob("src/kora/protocols/storage*.py"),
        *root.glob("src/kora/conformance/*storage*.py"),
        *root.glob("tests/measurements/storage_*.py"),
        *root.glob("tests/plugins/storage/*.py"),
        root / "tests/measurements/test_artifact_storage_lifetime.py",
        root / "pyproject.toml",
        root / "uv.lock",
        root / ".github/workflows/storage.yml",
        *root.glob("tests/measurements/storage_*.json"),
    ]
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def disk_counts(root: Path) -> dict[str, ExtraValue]:
    unique: dict[tuple[int, int], os.stat_result] = {}
    private_files = 0
    links = 0
    root_device = root.stat().st_dev
    for path in root.rglob("*"):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            continue
        unique[(info.st_dev, info.st_ino)] = info
        relative = path.relative_to(root)
        if relative.parts[0] == "stages":
            private_files += 1
        if relative.parts[0] in {"stages", "artifacts"}:
            links += 1
    unlinked = 0
    for name in os.listdir("/dev/fd"):
        try:
            info = os.fstat(int(name))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        key = (info.st_dev, info.st_ino)
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_dev == root_device
            and info.st_nlink == 0
        ):
            if key not in unique:
                unlinked += info.st_size
            unique[key] = info
    database = root / "control.sqlite"
    journal = root / "control.sqlite-journal"
    return {
        "database_bytes": database.stat().st_size,
        "journal_bytes": journal.stat().st_size if journal.exists() else 0,
        "unique_bytes": sum(info.st_size for info in unique.values()),
        "allocated_bytes": sum(info.st_blocks * 512 for info in unique.values()),
        "private_files": private_files,
        "payload_links": links,
        "unlinked_bytes": unlinked,
    }


def process_ownership() -> tuple[
    frozenset[tuple[int, int, int]], frozenset[int], frozenset[int]
]:
    descriptors: set[tuple[int, int, int]] = set()
    for name in os.listdir("/dev/fd"):
        try:
            info = os.fstat(int(name))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        descriptors.add((int(name), info.st_dev, info.st_ino))
    return (
        frozenset(descriptors),
        frozenset(
            thread.ident for thread in threading.enumerate() if thread.ident is not None
        ),
        frozenset(id(task) for task in asyncio.all_tasks()),
    )


def sample_resources(root: Path) -> dict[str, int]:
    current, peak = tracemalloc.get_traced_memory()
    values = {
        "python_current_bytes": current,
        "python_peak_bytes": peak,
        "tracer_bytes": tracemalloc.get_tracemalloc_memory(),
        "rss_bytes": rss_bytes(),
        "cumulative_maxrss_bytes": int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        )
        * (1 if sys.platform == "darwin" else 1024),
        "threads": threading.active_count(),
        "descriptors": len(os.listdir("/dev/fd")),
        "tasks": len(asyncio.all_tasks()),
    }
    for name, value in disk_counts(root).items():
        if not isinstance(value, int):
            raise AssertionError("physical resource counters must be integers")
        values[name] = value
    gc.collect()
    values["python_post_gc_bytes"] = tracemalloc.get_traced_memory()[0]
    return values


def retaining_paths(output: Path, name: str) -> None:
    snapshot = tracemalloc.take_snapshot()
    snapshot.dump(str(output / f"{name}.snapshot"))
    paths = snapshot.statistics("traceback")[:40]
    (output / f"{name}.txt").write_text(
        "\n\n".join(
            f"{item.size} bytes, {item.count} allocations\n"
            + "\n".join(f"{frame.filename}:{frame.lineno}" for frame in item.traceback)
            for item in paths
        ),
        encoding="utf-8",
    )
    del paths, snapshot


async def close_provider(provider: InspectedStorage) -> None:
    await provider.aclose()
    provider.assert_closed()
