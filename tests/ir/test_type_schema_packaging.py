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
    names = (
        "VcsRef",
        "StructuredOutput",
        "AgentSession",
        "CommandResult",
        "AgentSession.cel",
    )

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
            "import sys",
            "from copy import deepcopy",
            "from types import FrameType",
            "from kora.ir import ToolPolicy, validate_tool_policies, export_ir",
            "from kora.ir import GraphDocument, validate_type_schemas",
            "from kora.ir import validate_inline_schemas, validate_output_schemas",
            "from kora.ir import validate_edge_types, validate_export_types",
            "from kora.ir import validate_subgraph_interfaces",
            "from kora.ir import OutputDecl",
            "from kora.ir import validate_fan_out_types",
            "from kora.ir import validate_expression_syntax, validate_input_paths",
            "from kora.ir import validate_approval_response_predicates",
            "from kora.ir.agent_session_view import read_agent_session_view",
            "from kora.ir.approval_predicates import "
            "ApprovalResponsePredicateResult as PredicateResult",
            "from pathlib import Path",
            "direct = {'ir_version': '1.0',",
            " 'type_contracts': {'AgentSession': {'builtin_revision': '1'}},",
            " 'graph': {'id': 'g', 'namespace': 'example', 'nodes': {",
            " 'agent': {'kind': 'agent',",
            " 'outputs': {'session': {'type': 'AgentSession'}},",
            " 'config': {'harness': 'claude', 'prompt': '',",
            " 'workspace': {'repo': ''}}},",
            " 'gate': {'kind': 'human_input',",
            " 'inputs': {'session': {'type': 'AgentSession',",
            " 'from': 'agent', 'output': 'session'}},",
            " 'outputs': {'response': {'type': 'StructuredOutput'}},",
            " 'config': {'channel': 'cli', 'timeout': '0s', 'on_expiry': 'fail',",
            " 'approve_when': 'inputs.session.turns > 0'}}}},",
            " 'runtime': {'runtime': 'local', 'vcs': 'git'}}",
            "checked = validate_approval_response_predicates(",
            "    GraphDocument.model_validate(direct))",
            "assert checked.approval_predicates_valid",
            "direct['graph']['nodes']['gate']['config']['approve_when'] = (",
            "    'inputs.session.transcript')",
            "checked = validate_approval_response_predicates(",
            "    GraphDocument.model_validate(direct))",
            "assert not checked.approval_predicates_valid",
            f"assert Path(kora.__file__).is_relative_to({str(tmp_path)!r})",
            "direct_nodes = direct['graph']['nodes']",
            "gate_config = direct_nodes['gate']['config']",
            "gate_config['approve_when'] = 'inputs.session.turns > 0'",
            "direct_nodes['agent2'] = deepcopy(direct_nodes['agent'])",
            "direct_nodes['gate2'] = deepcopy(direct_nodes['gate'])",
            "direct_nodes['gate2']['inputs']['session']['from'] = 'agent2'",
            "direct_nodes['independent'] = deepcopy(direct_nodes['gate'])",
            "del direct_nodes['independent']['inputs']",
            "direct_nodes['independent']['config']['approve_when'] = 'true'",
            "def checked_views(raw: object) -> tuple[PredicateResult, int]:",
            "    calls = 0",
            "    view_code = read_agent_session_view.__code__",
            "    def observe(frame: FrameType, event: str, argument: object) -> None:",
            "        nonlocal calls",
            "        if event == 'call' and frame.f_code is view_code:",
            "            calls += 1",
            "    assert sys.getprofile() is None",
            "    try:",
            "        sys.setprofile(observe)",
            "        checked = validate_approval_response_predicates(",
            "            GraphDocument.model_validate(raw))",
            "    finally:",
            "        sys.setprofile(None)",
            "    return checked, calls",
            "checked, calls = checked_views(direct)",
            "assert checked.approval_predicates_valid and calls == 1",
            "unavailable = deepcopy(direct)",
            "unavailable_nodes = unavailable['graph']['nodes']",
            "del unavailable_nodes['gate2']",
            "unavailable_nodes['gate']['inputs']['absent'] = {",
            "    'type': 'AgentSession', 'from': 'absent', 'output': 'session'}",
            "checked, calls = checked_views(unavailable)",
            "assert checked.context_unresolved == 1 and calls == 0",
            "schema = Path(kora.__file__).parent / 'ir' / 'artifact_schemas'",
            "schema = schema / 'AgentSession.cel.schema.json'",
            "original = schema.read_bytes()",
            "changed = original.replace(b'\\n', b' ', 1)",
            "assert len(changed) == len(original) and changed != original",
            "try:",
            "    schema.write_bytes(changed)",
            "    checked, calls = checked_views(direct)",
            "    assert checked.schema_unresolved == 2 and calls == 1",
            "    assert checked.predicates_checked == 1 and checked.diagnostics.valid",
            "finally:",
            "    schema.write_bytes(original)",
            "checked, calls = checked_views(direct)",
            "assert checked.approval_predicates_valid and calls == 1",
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
            "def approval(predicate: str) -> GraphDocument:",
            "    return GraphDocument.model_validate({",
            "        'ir_version': '1.0',",
            "        'type_contracts': {",
            "            'StructuredOutput': {'builtin_revision': '1'}},",
            "        'graph': {'id': 'approval', 'namespace': 'example',",
            "            'params': {'properties': {'ok': {'type': 'boolean'}},",
            "                'required': ['ok']},",
            "            'nodes': {'gate': {'kind': 'human_input', 'config': {",
            "                'channel': 'cli', 'timeout': '0s', 'on_expiry': 'fail',",
            "                'approve_when': predicate},",
            "                'outputs': {'response': {'type': 'StructuredOutput'}}}}},",
            "        'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "for predicate, complete, valid, errors in [",
            "    (\"response.decision == 'approve'\", True, True, 0),",
            "    ('params.ok', True, True, 0), ('params.ok == 1', True, False, 1),",
            "    ('0', True, False, 1), ('inputs.ok', True, False, 1)]:",
            "    approval_result = validate_approval_response_predicates(",
            "        approval(predicate))",
            "    assert approval_result.approval_predicates_complete is complete",
            "    assert approval_result.approval_predicates_valid is valid",
            "    assert len(approval_result.diagnostics.errors) == errors",
            "for predicate, valid in [('params == {}', True),",
            "                         ('params.ok', False),",
            "                         ('params[response.decision]', True)]:",
            "    omitted = export_ir(approval(predicate))",
            "    del omitted['graph']['params']",
            "    omitted_document = GraphDocument.model_validate(omitted)",
            "    omitted_result = validate_approval_response_predicates(",
            "        omitted_document)",
            "    assert omitted_result.approval_predicates_complete",
            "    assert omitted_result.approval_predicates_valid is valid",
            "    assert export_ir(omitted_document) == omitted",
            "assert 'cel' not in sys.modules",
            "lists = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {'VcsRef': {'builtin_revision': '1'}},",
            "'graph': {'id': 'graph', 'namespace': 'example', 'nodes': {",
            "'producer': {'kind': 'timer', 'config': {'duration': '0s'},",
            "'outputs': {'value': {'type': 'List[List[VcsRef]]'}}},",
            "'consumer': {'kind': 'timer', 'config': {'duration': '0s'},",
            "'inputs': {",
            "'known': {'type': 'List[List[VcsRef]]',",
            "'from': 'producer', 'output': 'value'},",
            "'bad': {'type': 'VcsRef', 'from': 'producer', 'output': 'value'}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "list_edges = validate_edge_types(lists)",
            "assert not list_edges.edge_types_valid and list_edges.edges_checked == 2",
            "assert list_edges.edges_unresolved == list_edges.edges_excluded == 0",
            "assert len(list_edges.diagnostics.errors) == 1",
            "assert list_edges.diagnostics.errors[0].code == 'IR003'",
            "assert list_edges.diagnostics.errors[0].path[-2:] == ('bad', 'type')",
            "exports_document = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {'VcsRef': {'builtin_revision': '1'}},",
            "'graph': {'id': 'graph', 'namespace': 'example',",
            "'nodes': {'timer': {'kind': 'timer', 'config': {'duration': '0s'}}},",
            "'subgraphs': {'body': {'nodes': {",
            "'producer': {'kind': 'timer', 'config': {'duration': '0s'},",
            "'outputs': {'value': {'type': 'List[List[VcsRef]]'}}}},",
            "'outputs': {'known': {'type': 'List[List[VcsRef]]',",
            "'from': 'producer', 'output': 'value'},",
            "'bad': {'type': 'VcsRef', 'from': 'producer', 'output': 'value'}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "exports = validate_export_types(exports_document)",
            "assert not exports.export_types_valid",
            "assert exports.exports_checked == exports.exports_compared == 2",
            "assert exports.exports_unresolved == 0",
            "assert len(exports.diagnostics.errors) == 1",
            "error = exports.diagnostics.errors[0]",
            "assert error.code == 'IR003' and error.node is error.offset is None",
            "assert error.path == "
            "('graph', 'subgraphs', 'body', 'outputs', 'bad', 'type')",
            "interfaces_document = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {'VcsRef': {'builtin_revision': '1'}},",
            "'graph': {'id': 'graph', 'namespace': 'example', 'nodes': {",
            "'call': {'kind': 'subgraph', 'config': {'body': 'body'},",
            "'inputs': {'value': {'type': 'List[VcsRef]'}},",
            "'outputs': {'value': {'type': 'List[List[VcsRef]]'}}}},",
            "'subgraphs': {'body': {'nodes': {",
            "'timer': {'kind': 'timer', 'config': {'duration': '0s'}}},",
            "'inputs': {'value': {'type': 'List[VcsRef]'}},",
            "'outputs': {'value': {'type': 'List[List[VcsRef]]',",
            "'from': 'timer', 'output': 'missing'}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "interfaces = validate_subgraph_interfaces(interfaces_document)",
            "assert interfaces.interfaces_valid and interfaces.types_compared == 2",
            "interfaces_document.graph.nodes['call'].outputs['value'] = "
            "OutputDecl(type='VcsRef')",
            "interfaces = validate_subgraph_interfaces(interfaces_document)",
            "assert not interfaces.interfaces_valid",
            "assert interfaces.types_compared == 2",
            "assert interfaces.types_unresolved == 0",
            "error, = interfaces.diagnostics.errors",
            "assert error.code == 'IR003' and error.node == 'call'",
            "assert error.path == "
            "('graph', 'nodes', 'call', 'outputs', 'value', 'type')",
            "fan_out_document = GraphDocument.model_validate({",
            "'ir_version': '1.0',",
            "'type_contracts': {'VcsRef': {'builtin_revision': '1'}},",
            "'graph': {'id': 'graph', 'namespace': 'example', 'nodes': {",
            "'fan': {'kind': 'fan_out',",
            "'config': {'body': 'body', 'over': 'items', 'max_parallel': 2},",
            "'inputs': {'items': {'type': 'VcsRef', 'from': '$params'}},",
            "'outputs': {",
            "'known': {'type': 'List[List[VcsRef]]', 'collect': 'value'},",
            "'bad': {'type': 'List[VcsRef]', 'collect': 'value'}}}},",
            "'subgraphs': {'body': {'nodes': {",
            "'producer': {'kind': 'timer', 'config': {'duration': '0s'},",
            "'outputs': {'value': {'type': 'List[VcsRef]'}}}},",
            "'outputs': {'value': {'type': 'List[VcsRef]',",
            "'from': 'producer', 'output': 'value'}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "fan_out = validate_fan_out_types(fan_out_document)",
            "assert not fan_out.fan_out_types_valid",
            "assert fan_out.fan_outs_checked == fan_out.mapped_inputs_checked == 1",
            "assert fan_out.collections_checked == fan_out.collections_compared == 2",
            "assert fan_out.collections_unresolved == 0",
            "assert fan_out.mapped_inputs_unresolved == 0",
            "assert [e.code for e in fan_out.diagnostics.errors] == ['IR004', 'IR003']",
            "assert fan_out.diagnostics.errors[0].path == "
            "('graph', 'nodes', 'fan', 'config', 'over')",
            "assert fan_out.diagnostics.errors[1].path == "
            "('graph', 'nodes', 'fan', 'outputs', 'bad', 'type')",
            "policies_document = GraphDocument.model_validate({",
            "'ir_version': '1.0', 'type_contracts': {},",
            "'graph': {'id': 'graph', 'namespace': 'example', 'nodes': {",
            "'agent': {'kind': 'agent',",
            "'outputs': {'session': {'type': 'AgentSession'}},",
            "'config': {'harness': 'claude', 'prompt': '', 'workspace': {'repo': ''},",
            "'tool_policy': {'allow': ['Bash(git *)', 'Structured*']}}}}},",
            "'runtime': {'runtime': 'local', 'vcs': 'git'}})",
            "assert validate_tool_policies(policies_document).tool_policies_valid",
            "policy_data = export_ir(policies_document)",
            "policy_data['graph']['nodes']['agent']['config']['tool_policy'] = "
            "{'deny': ['StructuredOutput']}",
            "policies = validate_tool_policies("
            "GraphDocument.model_validate(policy_data))",
            "assert not policies.tool_policies_valid",
            "assert policies.policies_checked == policies.patterns_checked == 1",
            "error, = policies.diagnostics.errors",
            "assert error.code == 'IR013' and error.node == 'agent'",
            "assert error.path[-2:] == ('deny', 0)",
            "assert not any(name == 'kora.protocols' or "
            "name.startswith('kora.plugins') "
            "for name in sys.modules)",
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
