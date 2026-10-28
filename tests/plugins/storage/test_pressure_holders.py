import ast
import inspect
import os
import select
import stat
import subprocess
import sys
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from tests.plugins.storage.pressure_holders import (
    Binding,
    BoundTarget,
    FileStamp,
    Holder,
    HolderLimits,
    HolderObserver,
    Observation,
    ObservationContext,
    Scan,
    binding_changed,
    classify_volume,
    file_identity,
    inspect_path,
    render,
    volume_matching,
)
from tests.plugins.storage.pressure_models import child_diagnostic
from tests.plugins.storage.pressure_native import process_token


def test_child_diagnostic_is_bounded_after_escaping() -> None:
    hostile = b"hidden\xff\x1b\r\x00\n::error::" * 8192

    result = child_diagnostic(hostile)
    tiny = child_diagnostic(b"\xff" * 100, 64)

    assert result.startswith("[truncated] ")
    assert len(result.encode()) <= 24576
    assert len(tiny.encode()) <= 64
    assert "::" not in result
    assert "\\xff" in result
    assert all(character == "\n" or 32 <= ord(character) <= 126 for character in result)


def test_holder_note_rendering_is_bounded_and_content_free() -> None:
    paths = (
        "/private/test-python",
        "/System/Volumes/Data/Users/x/bin/y",
        "/System/Library/../../Users/x",
        "/System/Library//tool",
        "/System/Library/./tool",
        "/System/Library/a:b",
        "/System/Library/" + "x" * 256,
    )
    observation = Observation()
    observation.holders = [
        Holder(index + 1, "stable", uid=501, comm=b"safe\0private", executable=path)
        for index, path in enumerate(paths)
    ]
    observation.holders.append(
        Holder(99, "stable", comm=b"safe", executable="/System/Library/a b")
    )

    result = render(observation, HolderLimits())
    limited = render(observation, HolderLimits(note=700))

    assert result.count("executable=other") == len(paths)
    assert "executable=/System/Library/a%20b" in result
    assert "comm=safe" in result
    observation.binding = "changed"
    assert "holder pid=" not in render(observation, HolderLimits())
    assert "private" not in result
    assert len(limited.encode()) <= 700
    assert "truncated=note" in limited
    assert "errors layout=none" in limited
    assert "volume_entries=unbound" in limited
    assert "::" not in result


def volume_stat(
    device: int, inode: int = 2, mode: int = 0o40700, uid: int = 501
) -> FileStamp:
    return FileStamp(mode, device, inode, 0, uid)


def test_volume_state_classification(tmp_path: Path) -> None:
    mounted = volume_stat(10)
    directory = volume_stat(20)
    cases = (
        (mounted, "mounted"),
        (volume_stat(10, inode=3), "changed"),
        (directory, "unmounted"),
        (volume_stat(20, mode=0o40755), "changed"),
        (volume_stat(20, uid=502), "changed"),
        (volume_stat(20, mode=stat.S_IFLNK | 0o700), "changed"),
        (volume_stat(30), "foreign_mount"),
        ("absent", "absent"),
        ("denied", "denied"),
        ("error.EIO", "error.EIO"),
    )

    states = tuple(classify_volume(value, 10, (10, 2), 20, 501) for value, _ in cases)

    assert states == tuple(expected for _, expected in cases)
    assert not binding_changed(
        "mounted", "unmounted", "unmounted", (("bound", "bound"),)
    )
    assert not volume_matching("mounted", "unmounted", "unmounted")
    assert volume_matching("mounted", "mounted", "mounted")
    assert not binding_changed("unmounted", "unmounted", "unmounted", ())
    assert binding_changed("unmounted", "mounted", "mounted", ())
    assert binding_changed("mounted", "mounted", "unmounted", ())
    assert binding_changed("mounted", "foreign_mount", "foreign_mount", ())
    assert binding_changed("mounted", "mounted", "mounted", (("bound", "changed"),))


def close_holder(process: subprocess.Popen[bytes]) -> None:
    try:
        process.communicate(b"c", timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def owned_holders(
    directory: Path,
    kinds: tuple[str, ...],
) -> Generator[tuple[Binding, tuple[subprocess.Popen[bytes], ...]]]:
    target = directory / "target"
    sibling = directory / "sibling"
    target.write_bytes(b"x")
    sibling.write_bytes(b"y")
    info = inspect_path(target)
    assert isinstance(info, FileStamp)
    binding = Binding(
        (BoundTarget("target", target, file_identity(info)),),
        euid=os.geteuid(),
    )
    children: list[subprocess.Popen[bytes]] = []
    with ExitStack() as cleanup:
        for kind in kinds:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import os,sys; "
                    "flags=0x8000 if sys.argv[2]=='event' else os.O_RDONLY; "
                    "fd=os.open(sys.argv[1], flags); "
                    "print('ready',flush=True); sys.stdin.buffer.read(1); os.close(fd)",
                    str(sibling if kind == "sibling" else target),
                    kind,
                ],
                cwd=directory,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            cleanup.callback(close_holder, child)
            children.append(child)
            assert child.stdout is not None
            ready, _, _ = select.select([child.stdout], [], [], 10)
            assert ready
            assert child.stdout.readline(64) == b"ready\n"
        yield binding, tuple(children)


def observation_context() -> ObservationContext:
    return ObservationContext(os.getpid(), None, os.path.realpath(sys.executable))


def test_holder_pass_excludes_event_only_descriptors_and_matches_exact_vnodes(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        assert "status=unsupported_platform" in HolderObserver().observe(
            Binding(), observation_context()
        )
        return
    with owned_holders(tmp_path, ("event", "sibling", "target")) as (binding, children):
        token = process_token(children[2].pid)
        assert token is not None

        result = HolderObserver(budget_clock=lambda: 0).observe(
            binding, observation_context()
        )

        assert f"holder pid={children[0].pid} " not in result
        assert f"holder pid={children[1].pid} " not in result
        line = next(
            line
            for line in result.splitlines()
            if line.startswith(f"holder pid={children[2].pid} ")
        )
        assert "identity=stable" in line
        assert f"uniqueid={token.identity.split(':')[1]} " in line
        assert "/REG/target" in line
        assert "errors=" not in line
        assert "errors layout=none" in result


class CheckpointBudget:
    def __init__(self, point: str, holder_pid: int) -> None:
        self.point = point
        self.holder_pid = holder_pid
        self.triggered = False

    def __call__(self) -> float:
        frame = inspect.currentframe()
        try:
            caller = None if frame is None else frame.f_back
            scan_value: object = None if caller is None else caller.f_locals.get("self")
            checkpoint = None if caller is None else caller.f_back
            pid_value: object = (
                None if checkpoint is None else checkpoint.f_locals.get("pid")
            )
            if isinstance(scan_value, Scan):
                if (
                    self.point == "before"
                    or (self.point == "pid" and scan_value.observation.visited == 1)
                    or (
                        self.point == "fd"
                        and checkpoint is not None
                        and checkpoint.f_code.co_name == "process_references"
                        and pid_value == self.holder_pid
                    )
                ):
                    self.triggered = True
            return 6 if self.triggered else 0
        finally:
            del frame


@pytest.mark.parametrize("point", ["before", "pid", "fd"])
def test_holder_pass_deadlines_keep_final_revalidation_and_rendering(
    tmp_path: Path, point: str
) -> None:
    if sys.platform != "darwin":
        assert "status=unsupported_platform" in HolderObserver().observe(
            Binding(), observation_context()
        )
        return
    with owned_holders(tmp_path, ("target",)) as (binding, children):
        clock = CheckpointBudget(point, children[0].pid)
        observer = HolderObserver(HolderLimits(descriptor_check=1), budget_clock=clock)
        before = tuple(sorted(os.listdir("/dev/fd")))

        result = observer.observe(binding, observation_context())
        after = tuple(sorted(os.listdir("/dev/fd")))

        assert clock.triggered
        assert before == after
        assert "target=target before=bound after=bound" in result
        assert "visited=" in result
        assert "truncated=deadline" in result
        assert "elapsed_ms " in result
        if point == "before":
            assert "visited=0/0" in result
        elif point == "pid":
            assert "visited=1/" in result


def test_holder_pass_limits_are_reported(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        assert "status=unsupported_platform" in HolderObserver().observe(
            Binding(), observation_context()
        )
        return
    with owned_holders(tmp_path, ("target", "target", "target")) as (binding, _):
        context = observation_context()

        holders = HolderObserver(
            HolderLimits(holders=1), budget_clock=lambda: 0
        ).observe(binding, context)
        descriptors = HolderObserver(
            HolderLimits(descriptors=4), budget_clock=lambda: 0
        ).observe(binding, context)
        census = HolderObserver(
            HolderLimits(processes=2), budget_clock=lambda: 0
        ).observe(binding, context)

        assert "holders=3" in holders
        assert "truncated=holders" in holders
        assert "truncated=fds" in descriptors
        assert "truncated=census" in census


def module_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    handles = {
        target.id
        for assignment in ast.walk(tree)
        if isinstance(assignment, ast.Assign)
        and isinstance(assignment.value, ast.Call)
        and isinstance(assignment.value.func, ast.Attribute)
        and isinstance(assignment.value.func.value, ast.Name)
        and assignment.value.func.value.id == "ctypes"
        and assignment.value.func.attr == "CDLL"
        for target in assignment.targets
        if isinstance(target, ast.Name)
    }
    for assignment in ast.walk(tree):
        if (
            isinstance(assignment, ast.Assign)
            and isinstance(assignment.value, ast.Name)
            and assignment.value.id in handles
        ):
            handles.update(
                target.id
                for target in assignment.targets
                if isinstance(target, ast.Name)
            )
    allowed_imports = {
        "collections.abc",
        "ctypes",
        "dataclasses",
        "errno",
        "os",
        "pathlib",
        "re",
        "stat",
        "struct",
        "sys",
        "time",
        "typing",
    }
    os_attributes = {
        "open",
        "close",
        "lstat",
        "stat",
        "fstat",
        "getpid",
        "geteuid",
        "path",
        "O_RDONLY",
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
    }
    ctypes_attributes = {
        "CDLL",
        "c_int",
        "c_uint32",
        "c_uint64",
        "c_void_p",
        "create_string_buffer",
        "get_errno",
        "set_errno",
    }
    native_symbols = {
        "proc_listallpids",
        "proc_pidinfo",
        "proc_pidfdinfo",
        "proc_pidpath",
    }
    denied = {
        "getattr",
        "setattr",
        "delattr",
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "vars",
        "globals",
        "locals",
        "PressureFailure",
    }
    denied_attributes = {
        "__dict__",
        "from_buffer",
        "from_buffer_copy",
        "from_address",
        "from_param",
    }
    violations: set[str] = set()
    loads = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name not in allowed_imports or name.asname is not None:
                    violations.add("import")
        if isinstance(node, ast.ImportFrom) and (
            node.module not in allowed_imports
            or node.module in {"os", "ctypes"}
            or any(name.asname is not None for name in node.names)
        ):
            violations.add("import")
        if isinstance(node, ast.Name) and node.id in denied:
            violations.add("builtin")
        if isinstance(node, ast.Attribute):
            if node.attr in denied_attributes:
                violations.add("attribute")
            if isinstance(node.value, ast.Name):
                allowed = {
                    "os": os_attributes,
                    "ctypes": ctypes_attributes,
                }.get(node.value.id)
                if node.value.id in handles:
                    allowed = native_symbols
                if allowed is not None and node.attr not in allowed:
                    violations.add("attribute")
            if (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
                and node.value.attr == "path"
                and node.attr != "realpath"
            ):
                violations.add("attribute")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctypes"
                and node.func.attr == "CDLL"
            ):
                loads += 1
                if (
                    len(node.args) != 1
                    or not isinstance(node.args[0], ast.Constant)
                    or node.args[0].value != "/usr/lib/libproc.dylib"
                ):
                    violations.add("load")
                if (
                    len(node.keywords) != 1
                    or node.keywords[0].arg != "use_errno"
                    or not isinstance(node.keywords[0].value, ast.Constant)
                    or node.keywords[0].value.value is not True
                ):
                    violations.add("load")
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "open"
            ):
                flags = node.args[1] if len(node.args) >= 2 else None
                names: set[str] = (
                    set()
                    if flags is None
                    else {
                        entry.attr
                        for entry in ast.walk(flags)
                        if isinstance(entry, ast.Attribute)
                        and isinstance(entry.value, ast.Name)
                        and entry.value.id == "os"
                    }
                )
                if (
                    "O_RDONLY" not in names
                    or not names
                    <= {"O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"}
                    or flags is None
                    or any(
                        isinstance(entry, (ast.Constant, ast.Call, ast.Name))
                        and not (isinstance(entry, ast.Name) and entry.id == "os")
                        for entry in ast.walk(flags)
                    )
                ):
                    violations.add("flags")
                if flags is not None and any(
                    not isinstance(
                        entry, (ast.BinOp, ast.BitOr, ast.Attribute, ast.Name, ast.Load)
                    )
                    for entry in ast.walk(flags)
                ):
                    violations.add("flags")
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in handles
        ):
            violations.add("attribute")
    if loads != 1:
        violations.add("load-count")
    return violations


def test_holder_observer_module_contract() -> None:
    source = Path(__file__).with_name("pressure_holders.py").read_text()
    conforming = (
        "import ctypes\n"
        "library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)\n"
    )
    escapes = (
        *(f"os.{name}" for name in ("chdir", "fchdir", "kill", "killpg", "O_WRONLY")),
        *(
            f"ctypes.{name}"
            for name in (
                "cdll",
                "pythonapi",
                "PyDLL",
                "CFUNCTYPE",
                "memmove",
                "string_at",
                "addressof",
                "pointer",
                "POINTER",
                "byref",
                "from_buffer",
            )
        ),
        "library.proc_listpidspath",
        "ctypes.CDLL(None)",
        *(
            f"{name}(x)"
            for name in (
                "getattr",
                "setattr",
                "delattr",
                "eval",
                "exec",
                "compile",
                "__import__",
                "open",
                "vars",
                "globals",
                "locals",
            )
        ),
        "import subprocess",
        "import ctypes.util",
        "from os import open",
        "from ctypes import CDLL",
        "PressureFailure",
        "thing.__dict__",
        *(
            f"thing.{name}"
            for name in ("from_buffer_copy", "from_address", "from_param")
        ),
        "os.path.exists",
        "os.open(path, os.O_WRONLY)",
        "os.open(path, flags)",
        "os.open(path, ~os.O_RDONLY)",
        "renamed=library\nrenamed.proc_listpidspath",
        "library['system']",
    )

    actual = module_violations(source)
    rejected = tuple(module_violations(conforming + escape) for escape in escapes)

    assert not actual
    assert not module_violations(conforming)
    assert all(rejected)
