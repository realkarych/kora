from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from kora.dsl.source import SOURCE_LABEL, SourceRefusal, decode_source, read_source
from kora.dsl.syntax import GraphSyntax, graph_syntax
from kora.ir.deployment_checks import check_authored_document
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.document import GraphDocument
from kora.ir.export import export_ir, render_ir_json
from kora.ir.json import JsonObject

BUILTIN_TYPES = ("AgentSession", "CommandResult", "StructuredOutput")


@dataclass(frozen=True, slots=True)
class CompiledGraph:
    _ir: GraphDocument

    @property
    def document(self) -> JsonObject:
        return export_ir(self._ir)

    @property
    def text(self) -> str:
        return render_ir_json(self._ir)


@dataclass(frozen=True, slots=True)
class CompileFindings:
    diagnostics: DiagnosticCollection


class CompileIncomplete(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Authored graph validation did not complete.")


def location(line: int) -> JsonObject:
    return {"file": SOURCE_LABEL, "line": line}


def lower(syntax: GraphSyntax, source: bytes) -> GraphDocument:
    agent, command, approval = syntax.agent, syntax.command, syntax.approval
    workspace: JsonObject = {"repo": syntax.repo, "rev": syntax.rev}
    type_contracts: JsonObject = {
        name: {"builtin_revision": "1"} for name in BUILTIN_TYPES
    }
    agent_config: JsonObject = {
        "harness": agent.harness,
        "prompt": agent.prompt,
        "workspace": workspace,
    }
    agent_outputs: JsonObject = {"session": {"type": "AgentSession"}}
    command_config: JsonObject = {
        "cmd": list(command.arguments),
        "workspace": workspace,
    }
    command_node: JsonObject = {
        "kind": "command",
        "source": location(command.line),
        "depends_on": [agent.name],
        "config": command_config,
        "outputs": {"result": {"type": "CommandResult"}},
    }
    approval_inputs: JsonObject = {
        approval.input_name: {
            "type": "CommandResult",
            "from": command.name,
            "output": "result",
        }
    }
    approval_config: JsonObject = {
        "channel": "cli",
        "on_expiry": "fail",
        "timeout": approval.timeout,
    }
    if syntax.commit_reference:
        change_input: JsonObject = {
            "type": "VcsRef",
            "from": agent.name,
            "output": "change",
        }
        type_contracts["VcsRef"] = {"builtin_revision": "1"}
        agent_config["trust_repo_config"] = False
        agent_outputs["change"] = {"type": "VcsRef"}
        command_config["workspace"] = {
            "repo": syntax.repo,
            "from_ref": "inputs.change",
        }
        command_node["inputs"] = {"change": change_input}
        approval_inputs["change"] = change_input
        approval_config["binds"] = {
            "vcs_ref": "inputs.change",
            "gate_digests": ["inputs.gate"],
        }
    else:
        agent_outputs["summary"] = {"type": "StructuredOutput"}
    return GraphDocument.model_validate(
        {
            "ir_version": "1.0",
            "meta": {
                "compiler": "kora early Python DSL v1",
                "source_digest": "sha256:" + sha256(source).hexdigest(),
            },
            "type_contracts": type_contracts,
            "graph": {
                "id": syntax.name,
                "namespace": syntax.namespace,
                "source": location(syntax.line),
                "params": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "triggers": [{"kind": "manual"}],
                "nodes": {
                    agent.name: {
                        "kind": "agent",
                        "source": location(agent.line),
                        "config": agent_config,
                        "outputs": agent_outputs,
                    },
                    command.name: command_node,
                    approval.name: {
                        "kind": "human_input",
                        "source": location(approval.line),
                        "inputs": approval_inputs,
                        "config": approval_config,
                        "outputs": {"response": {"type": "StructuredOutput"}},
                    },
                },
            },
            "runtime": {"runtime": "local", "vcs": "git"},
        }
    )


def compile_graph(path: Path) -> CompiledGraph | CompileFindings:
    try:
        source = read_source(path)
        typed = lower(graph_syntax(decode_source(source)), source)
    except SourceRefusal as error:
        findings = DiagnosticCollector()
        findings.add(error.diagnostic)
        return CompileFindings(findings.finish())
    checked = check_authored_document(typed)
    if not checked.diagnostics.valid:
        return CompileFindings(checked.diagnostics)
    if not checked.complete:
        raise CompileIncomplete
    return CompiledGraph(typed)
