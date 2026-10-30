from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.declarations import IrSourceLocation
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import safe_source
from kora.ir.link_cycles import cyclic_vertices
from kora.ir.topology import Graph, Subgraph
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

Path = tuple[str | int, ...]
PUBLISHED_BUILTINS: frozenset[str] = frozenset(
    {"VcsRef", "StructuredOutput", "AgentSession", "CommandResult"}
)
COMPONENT: str = CATALOG.register_component("ir.validation")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.type_declarations.completed",
        COMPONENT,
        Level.INFO,
        "type declaration validation completed",
        {
            "declarations_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "declarations": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class TypeShape:
    base: str
    list_depth: int


def parse_type_shape(value: str) -> TypeShape | None:
    start = 0
    depth = 0
    while value.startswith("List[", start):
        start += 5
        depth += 1
    end = len(value)
    unmatched = depth
    while end > start and value[end - 1] == "]":
        end -= 1
        unmatched -= 1
    return TypeShape(value[start:end], depth) if unmatched == 0 else None


def base_type(value: str) -> str | None:
    shape = parse_type_shape(value)
    return None if shape is None else shape.base


def contract_path(name: str, field: str | None = None) -> Path:
    if len(name) > 256:
        return ("type_contracts",)
    return ("type_contracts", name) + (() if field is None else (field,))


@dataclass(frozen=True)
class TypeDeclarationResult:
    diagnostics: DiagnosticCollection
    declarations_checked: int
    contracts_checked: int

    @property
    def declarations_valid(self) -> bool:
        return self.diagnostics.valid


@dataclass(frozen=True)
class Declaration:
    spelling: str
    path: Path
    source: IrSourceLocation | MISSING
    node: str | None = None


def declarations(body: Graph | Subgraph, path: Path) -> Iterator[Declaration]:
    for name, node in body.nodes.items():
        node_path = path + ("nodes", name)
        if node.inputs is not MISSING:
            for input_name, value in node.inputs.items():
                yield Declaration(
                    value.type,
                    node_path + ("inputs", input_name, "type"),
                    node.source,
                    name,
                )
        if node.outputs is not MISSING:
            for output_name, value in node.outputs.items():
                yield Declaration(
                    value.type,
                    node_path + ("outputs", output_name, "type"),
                    node.source,
                    name,
                )
    if isinstance(body, Subgraph):
        if body.inputs is not MISSING:
            for name, value in body.inputs.items():
                yield Declaration(
                    value.type, path + ("inputs", name, "type"), body.source
                )
        if body.outputs is not MISSING:
            for name, value in body.outputs.items():
                yield Declaration(
                    value.type, path + ("outputs", name, "type"), body.source
                )


def check_declarations(
    body: Graph | Subgraph,
    path: Path,
    contracts: dict[str, TypeContract],
    collector: DiagnosticCollector,
) -> int:
    count = 0
    for declaration in declarations(body, path):
        count += 1
        name = base_type(declaration.spelling)
        if name is None:
            code = "IR003"
            message = "The artifact type has unbalanced list brackets."
        elif name not in contracts:
            code = "IR022"
            message = "The artifact type is not declared in the saved contracts."
        else:
            continue
        collector.add(
            Diagnostic(
                code=code,
                message=message,
                path=declaration.path,
                location=safe_source(declaration.source),
                node=declaration.node,
            )
        )
    return count


def check_contracts(
    contracts: dict[str, TypeContract], collector: DiagnosticCollector
) -> None:
    edges: dict[str, set[str]] = {name: set() for name in contracts}
    for name, contract in contracts.items():
        if isinstance(contract, BuiltinTypeContract):
            if name not in PUBLISHED_BUILTINS or contract.builtin_revision != "1":
                collector.add(
                    Diagnostic(
                        code="IR022",
                        message="The built-in name and revision are not published.",
                        path=contract_path(name, "builtin_revision"),
                    )
                )
            continue
        if "/" not in name:
            collector.add(
                Diagnostic(
                    code="IR022",
                    message="A custom type requires a namespace-qualified name.",
                    path=contract_path(name),
                )
            )
        if contract.parent is None:
            continue
        if contract.parent in contracts:
            edges[name].add(contract.parent)
        else:
            collector.add(
                Diagnostic(
                    code="IR022",
                    message="The parent type is not declared in the saved contracts.",
                    path=contract_path(name, "parent"),
                )
            )
    for name in cyclic_vertices(edges):
        collector.add(
            Diagnostic(
                code="IR022",
                message="The type participates in a parent cycle.",
                path=contract_path(name, "parent"),
            )
        )


def validate_type_declarations(document: GraphDocument) -> TypeDeclarationResult:
    collector = DiagnosticCollector()
    contracts = document.type_contracts
    check_contracts(contracts, collector)
    count = check_declarations(document.graph, ("graph",), contracts, collector)
    if document.graph.subgraphs is not MISSING:
        for name, body in document.graph.subgraphs.items():
            count += check_declarations(
                body, ("graph", "subgraphs", name), contracts, collector
            )
    result = TypeDeclarationResult(collector.finish(), count, len(contracts))
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            declarations_valid=result.declarations_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            declarations=count,
            contracts=len(contracts),
        )
    return result
