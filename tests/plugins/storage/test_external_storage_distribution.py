import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.parsers.expat import ParserCreate

from kora.plugins.storage.local import CONFIG_SCHEMA
from tests.plugins.storage.pressure_native import collect_process


def run(arguments: list[str], directory: Path, environment: dict[str, str]) -> bytes:
    process = subprocess.Popen(
        arguments,
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        output, error_output, status = collect_process(process, 90)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    assert status == 0, error_output + output
    return output


def results(path: Path) -> list[str]:
    names: list[str] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        assert name not in {"failure", "error", "skipped"}
        if name == "testcase":
            names.append(attributes["name"])

    parser = ParserCreate()
    parser.StartElementHandler = start
    parser.Parse(path.read_bytes(), True)
    return names


def test_built_external_storage_distribution_runs_public_conformance(
    tmp_path: Path,
) -> None:
    sources = Path(__file__).parent
    package = tmp_path / "package"
    module = package / "kora_storage_extension"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    shutil.copyfile(sources / "extension_provider.py", module / "providers.py")
    shutil.copyfile(sources / "extension_cases.py", module / "cases.py")
    (module / "schema.json").write_text(json.dumps(CONFIG_SCHEMA))
    (package / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling==1.32.4"]\n'
        'build-backend = "hatchling.build"\n'
        '[project]\nname = "kora-storage-extension"\nversion = "1.0"\n'
        '[project.entry-points."kora.provider"]\n'
        '"artifact_storage.fixture_local" = '
        '"kora_storage_extension.providers:provider_metadata"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["kora_storage_extension"]\n'
    )
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    shutil.copyfile(
        sources / "extension_composition.py", bootstrap / "storage_composition.py"
    )
    site = tmp_path / "installed"
    output = tmp_path / "evidence"
    output.mkdir()
    environment = {
        "PATH": os.environ["PATH"],
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "UV_OFFLINE": "1",
    }

    run(
        [sys.executable, "-I", "-m", "hatchling", "build", "-t", "wheel"],
        package,
        environment,
    )
    wheels = tuple((package / "dist").glob("*.whl"))
    assert len(wheels) == 1
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--no-index",
            "--target",
            str(site),
            str(wheels[0]),
        ],
        tmp_path,
        environment,
    )
    run(
        [
            sys.executable,
            "-I",
            str(sources / "extension_probe.py"),
            str(site),
            str(bootstrap),
            str(output),
        ],
        tmp_path,
        environment,
    )
    collected = results(output / "conformance.xml")

    assert len(collected) == 17
    assert len(set(collected)) == len(collected)
    assert (output / "extension-report.json").is_file()
