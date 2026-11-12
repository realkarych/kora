import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.support.provider_tooling import (
    copy_core_wheel,
    core_wheel_build_stop,
    core_wheel_workspace,
    create_virtual_environment,
    prepare_wheelhouse,
    run_core_wheel_build,
    run_setup,
)

BUILD_TREE = """
import os
import signal
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, '-c', 'import os, signal; os.write(1, b"r"); signal.pause()'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
assert child.stdout is not None
assert child.stdout.read(1) == b'r'
child.stdout.close()

def settle(number: int, frame: object) -> None:
    child.wait(timeout=1)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, settle)
Path('kora-partial.whl').write_bytes(b'partial')
with open(sys.argv[1], 'w') as ready:
    ready.write(f'{os.getpid()} {child.pid}')
if sys.argv[2] == 'failure':
    sys.stdout.write('core build stdout\\n')
    sys.stderr.write('core build stderr\\n')
    raise SystemExit(23)
signal.pause()
"""


def observe_build_tree(
    descriptor: int, workspace: Path, interruption: signal.Signals | None
) -> tuple[int, ...]:
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        assert selector.select(6)
        pids = tuple(int(value) for value in os.read(descriptor, 128).split())
    if interruption is not None:
        assert (workspace / "kora-partial.whl").is_file()
        os.kill(os.getpid(), interruption)
    return pids


def installed_core(core_wheel: Path, root: Path) -> Path:
    wheelhouse = root / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    python = create_virtual_environment(root / "environment")
    run_setup(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--python",
            str(python),
            str(wheel),
        ]
    )
    return python


def test_installed_api_application_uses_only_runtime_dependencies(
    core_wheel: Path, tmp_path: Path
) -> None:
    python = installed_core(core_wheel, tmp_path)
    environment = tmp_path / "environment"
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = outside / "probe.py"
    shutil.copyfile(Path(__file__).with_name("installed_probe.py"), probe)

    process = subprocess.run(
        [str(python), "-I", str(probe), str(environment)],
        cwd=outside,
        env={"PATH": os.defpath},
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout == b"installed-api-ok\n"
    assert process.stderr == b""
    assert len(list(core_wheel.parent.glob("kora-*.tar.gz"))) == 1


def test_installed_core_refuses_mcp_without_its_extra(
    core_wheel: Path, tmp_path: Path
) -> None:
    python = installed_core(core_wheel, tmp_path)

    process = subprocess.run(
        [str(python), "-I", "-m", "kora", "mcp", "--server", "authoring"],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "KORA_URL": "http://127.0.0.1:9",
            "KORA_MCP_TOKEN": "token",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 78
    assert process.stdout == b""
    assert b"requires the kora[mcp] extra" in process.stderr


@pytest.mark.parametrize(
    ("case", "interruption", "error"),
    [
        ("failure", None, ValueError),
        ("timeout", None, TimeoutError),
        ("interrupt", signal.SIGINT, KeyboardInterrupt),
        ("terminate", signal.SIGTERM, KeyboardInterrupt),
    ],
)
def test_core_wheel_build_refuses_partial_artifacts_and_settles(
    tmp_path: Path,
    case: str,
    interruption: signal.Signals | None,
    error: type[BaseException],
) -> None:
    ready = tmp_path / "ready"
    os.mkfifo(ready)
    descriptor = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    handlers = tuple(
        signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    )
    arguments = [sys.executable, "-c", BUILD_TREE, str(ready), case]
    try:
        with (
            core_wheel_workspace() as output,
            core_wheel_build_stop() as stop,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(
                observe_build_tree, descriptor, output, interruption
            )
            deadline = time.monotonic() + (2 if case == "timeout" else 10)

            with pytest.raises(error) as refusal:
                run_core_wheel_build(
                    arguments, output, dict(os.environ), deadline, stop
                )
            pids = pending.result(timeout=6)

            assert (output / "kora-partial.whl").is_file()
            assert len(set(pids)) == 2
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
    finally:
        os.close(descriptor)

    assert not output.exists()
    assert (
        tuple(signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM))
        == handlers
    )
    if case == "failure":
        assert (
            str(refusal.value)
            == "core wheel build failed (completed, 23)\ncore build stderr\n"
        )
