from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.approval_form import default_approval_form
from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import safe_source
from kora.ir.json import JsonObject
from kora.ir.schema_equality import compare_json
from kora.ir.schema_locations import Path
from kora.ir.topology import AgentNode, HumanInputNode
from kora.ir.type_declarations import COMPONENT, PUBLISHED_BUILTINS
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.output_schemas.completed",
        COMPONENT,
        Level.INFO,
        "output schema consistency validation completed",
        {
            "output_schemas_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "outputs": FieldKind.INTEGER,
            "ancestry_steps": FieldKind.INTEGER,
            "comparisons": FieldKind.INTEGER,
            "values": FieldKind.INTEGER,
        },
    )
)


class StructuredAncestry:
    def __init__(self, contracts: dict[str, TypeContract]) -> None:
        self.contracts = contracts
        self.results: dict[str, bool | None] = {}
        self.steps: int = 0

    def contains(self, name: str) -> bool | None:
        if name.startswith("List["):
            return False
        pending: set[str] = set()
        result: bool | None = None
        while name not in pending:
            if name in self.results:
                result = self.results[name]
                break
            pending.add(name)
            self.steps += 1
            contract = self.contracts.get(name)
            if contract is None:
                break
            if isinstance(contract, BuiltinTypeContract):
                if name in PUBLISHED_BUILTINS and contract.builtin_revision == "1":
                    result = name == "StructuredOutput"
                break
            if "/" not in name:
                break
            if contract.parent is None:
                result = False
                break
            name = contract.parent
        for visited in pending:
            self.results[visited] = result
        return result


@dataclass(frozen=True)
class OutputSchemaResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    outputs_checked: int
    ancestry_steps: int
    comparisons_checked: int
    values_checked: int

    @property
    def output_schemas_valid(self) -> bool:
        return self.diagnostics.valid


class OutputSchemaChecks:
    def __init__(self, contracts: dict[str, TypeContract]) -> None:
        self.ancestry = StructuredAncestry(contracts)
        self.collector = DiagnosticCollector()
        self.default_form = default_approval_form()
        self.outputs: int = 0
        self.comparisons: int = 0
        self.values: int = 0

    def fault(
        self, node: AgentNode | HumanInputNode, name: str, path: Path, message: str
    ) -> None:
        self.collector.add(
            Diagnostic(
                code="IR003",
                message=message,
                path=path,
                node=name,
                location=safe_source(node.source),
            )
        )

    def compare(
        self,
        left: JsonObject,
        right: JsonObject,
        node: AgentNode | HumanInputNode,
        name: str,
        path: Path,
    ) -> None:
        comparison = compare_json(left, right)
        self.comparisons += 1
        self.values += comparison.values_checked
        if not comparison.equal:
            self.fault(
                node,
                name,
                path,
                "The declared output schema does not match its source schema.",
            )

    def agent(self, node: AgentNode, name: str, path: Path) -> None:
        structured = 0
        vcs = 0
        for output, declaration in node.outputs.items():
            self.outputs += 1
            if declaration.type == "VcsRef":
                vcs += 1
            elif self.ancestry.contains(declaration.type) is True:
                structured += 1
                if (
                    node.config.output_schema is not MISSING
                    and declaration.schema_ is not MISSING
                ):
                    self.compare(
                        node.config.output_schema,
                        declaration.schema_,
                        node,
                        name,
                        path + ("outputs", output, "schema"),
                    )
        if structured > 1:
            self.fault(
                node,
                name,
                path + ("outputs",),
                "An agent may declare only one structured output.",
            )
        if vcs > 1:
            self.fault(
                node,
                name,
                path + ("outputs",),
                "An agent may declare only one VcsRef output.",
            )

    def human_input(self, node: HumanInputNode, name: str, path: Path) -> None:
        self.outputs += 1
        declaration = node.outputs["response"]
        if declaration.schema_ is MISSING:
            return
        form = self.default_form if node.config.form is MISSING else node.config.form
        self.compare(
            form,
            declaration.schema_,
            node,
            name,
            path + ("outputs", "response", "schema"),
        )


def validate_output_schemas(document: GraphDocument) -> OutputSchemaResult:
    checks = OutputSchemaChecks(document.type_contracts)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    for body, path in bodies:
        for name, node in body.nodes.items():
            nodes += 1
            if isinstance(node, AgentNode):
                checks.agent(node, name, path + ("nodes", name))
            elif isinstance(node, HumanInputNode):
                checks.human_input(node, name, path + ("nodes", name))
    result = OutputSchemaResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.outputs,
        checks.ancestry.steps,
        checks.comparisons,
        checks.values,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            output_schemas_valid=result.output_schemas_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            outputs=result.outputs_checked,
            ancestry_steps=result.ancestry_steps,
            comparisons=result.comparisons_checked,
            values=result.values_checked,
        )
    return result
