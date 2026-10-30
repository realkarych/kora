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
            "from kora.ir import validate_inline_schemas, validate_output_schemas",
            "from kora.ir import validate_edge_types",
            "from kora.ir import validate_expression_syntax, validate_input_paths",
            "from pathlib import Path",
            f"assert Path(kora.__file__).is_relative_to({str(tmp_path)!r})",
            "document = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {name: {'builtin_revision': '1'} for name in",
            "('VcsRef', 'StructuredOutput', 'AgentSession', 'CommandResult')},",
            "'graph': {'id': 'graph', 'namespace': 'example',",
            "'params': {'$ref': 'urn:kora:artifact:StructuredOutput'},",
            "'nodes': {'gate': {'kind': 'human_input',",
            "'config': {'channel': 'cli', 'timeout': '0s',",
            "'on_expiry': 'fail', 'form': {}, 'approve_when': 'true &&',",
            "'binds': {'vcs_ref': 'inputs.missing'}},",
            "'outputs': {'response': {'type': 'StructuredOutput', 'schema': {}}}},",
            "'consumer': {'kind': 'timer', 'config': {'duration': '1s'},",
            "'inputs': {'data': {'type': 'StructuredOutput',",
            "'from': 'gate', 'output': 'response'}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "result = validate_type_schemas(document)",
            "assert result.schemas_valid and result.contracts_checked == 4",
            "inline = validate_inline_schemas(document)",
            "assert inline.inline_schemas_valid and inline.references_checked == 1",
            "outputs = validate_output_schemas(document)",
            "assert outputs.output_schemas_valid and outputs.comparisons_checked == 1",
            "edges = validate_edge_types(document)",
            "assert edges.edge_types_valid and edges.edges_checked == 1",
            "syntax = validate_expression_syntax(document)",
            "assert not syntax.syntax_valid and syntax.expressions_checked == 1",
            "assert syntax.diagnostics.errors[0].code == 'IR011'",
            "assert syntax.diagnostics.errors[0].offset == 7",
            "paths = validate_input_paths(document)",
            "assert not paths.input_paths_valid",
            "assert paths.paths_checked == paths.bindings_checked == 1",
            "assert paths.paths_unresolved == 0",
            "assert paths.diagnostics.errors[0].code == 'IR005'",
            "assert paths.diagnostics.errors[0].offset is None",
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
