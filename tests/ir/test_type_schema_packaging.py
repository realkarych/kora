import subprocess
from pathlib import Path
from zipfile import ZipFile

from tests.support.provider_tooling import (
    copy_core_wheel,
    create_virtual_environment,
    prepare_wheelhouse,
    run_setup,
)

REPOSITORY: Path = Path(__file__).resolve().parents[2]


def test_type_schema_catalogue_is_published_unchanged_in_core_wheel(
    core_wheel: Path,
) -> None:
    names = ("VcsRef", "StructuredOutput", "AgentSession", "CommandResult")

    with ZipFile(core_wheel) as archive:
        packaged = {
            name: archive.read(f"kora/ir/artifact_schemas/{name}.schema.json")
            for name in names
        }
        metadata = archive.read(
            next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
        ).decode()

    assert all(
        payload
        == (
            REPOSITORY / "knowledge/specs/artifacts" / f"{name}.schema.json"
        ).read_bytes()
        for name, payload in packaged.items()
    )
    assert "Requires-Dist: jsonschema==4.26.0\n" in metadata
    assert "Requires-Dist: referencing==0.37.0\n" in metadata


def test_type_schema_validation_works_from_installed_core_only_wheel(
    core_wheel: Path, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    python = create_virtual_environment(tmp_path / "environment")
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
    program = "\n".join(
        [
            "import kora",
            "from kora.ir import GraphDocument, validate_type_schemas",
            "from pathlib import Path",
            f"assert Path(kora.__file__).is_relative_to({str(tmp_path)!r})",
            "document = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {name: {'builtin_revision': '1'} for name in",
            "('VcsRef', 'StructuredOutput', 'AgentSession', 'CommandResult')},",
            "'graph': {'id': 'graph', 'namespace': 'example',",
            "'nodes': {'timer': {'kind': 'timer', 'config': {'duration': '1s'}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "result = validate_type_schemas(document)",
            "assert result.schemas_valid and result.contracts_checked == 4",
        ]
    )

    completed = subprocess.run(
        [str(python), "-I", "-c", program],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == b""
