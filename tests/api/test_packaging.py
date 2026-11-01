import os
import shutil
import subprocess
from pathlib import Path

from tests.support.provider_tooling import (
    copy_core_wheel,
    create_virtual_environment,
    prepare_wheelhouse,
    run_setup,
)


def test_installed_api_application_uses_only_runtime_dependencies(
    core_wheel: Path, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    environment = tmp_path / "environment"
    python = create_virtual_environment(environment)
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
