from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import MISSING, ValidationError

from kora.ir.declarations import IrSourceLocation, OutputDecl, OutputTarget
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic, SourceLocation
from kora.ir.document import GraphDocument
from kora.ir.link_cycles import cyclic_vertices
from kora.ir.topology import (
    BranchNode,
    FanOutNode,
    Graph,
    Node,
    Subgraph,
    SubgraphNode,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

Path = tuple[str | int, ...]
COMPONENT: str = CATALOG.register_component("ir.validation")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.graph_links.completed",
        COMPONENT,
        Level.INFO,
        "graph link validation completed",
        {
            "links_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
        },
    )
)


def safe_source(source: IrSourceLocation | MISSING) -> SourceLocation | None:
    if source is MISSING:
        return None
    try:
        return SourceLocation(file=source.file, line=source.line)
    except ValidationError:
        return None


def outputs(node: Node) -> dict[str, OutputDecl]:
    return {} if node.outputs is MISSING else node.outputs


@dataclass(frozen=True)
class GraphLinkResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int

    @property
    def links_valid(self) -> bool:
        return self.diagnostics.valid


class Scope:
    def __init__(
        self,
        body: Graph | Subgraph,
        path: Path,
        definitions: dict[str, Subgraph],
        collector: DiagnosticCollector,
    ) -> None:
        self.body = body
        self.path = path
        self.definitions = definitions
        self.collector = collector
        self.edges: dict[str, set[str]] = {name: set() for name in body.nodes}
        self.controls: dict[str, set[str]] = {
            name: set() if node.depends_on is MISSING else set(node.depends_on)
            for name, node in body.nodes.items()
        }
        self.calls: set[str] = set()

    def error(
        self,
        code: str,
        message: str,
        suffix: Path,
        node_name: str | None = None,
    ) -> None:
        source = safe_source(self.body.source)
        if node_name is not None:
            source = safe_source(self.body.nodes[node_name].source)
        self.collector.add(
            Diagnostic(
                code=code,
                message=message,
                path=self.path + suffix,
                node=node_name,
                location=source,
            )
        )

    def reference(
        self,
        producer: str | MISSING,
        output: str | MISSING,
        path: Path,
        node_name: str | None = None,
        code: str = "IR002",
    ) -> None:
        if producer is MISSING or producer not in self.body.nodes:
            self.error(
                code,
                "The producer does not resolve in this scope.",
                path + ("from",),
                node_name,
            )
            return
        if output is MISSING or output not in outputs(self.body.nodes[producer]):
            self.error(
                code,
                "The producer output is not declared.",
                path + ("output",),
                node_name,
            )

    def inputs(self, name: str, node: Node) -> None:
        inputs = {} if node.inputs is MISSING else node.inputs
        path = ("nodes", name)
        for input_name, value in inputs.items():
            source = value.from_
            field = path + ("inputs", input_name)
            if source in ("$params", "$previous_attempt"):
                continue
            if source == "$inputs":
                if not isinstance(self.body, Subgraph):
                    self.error(
                        "IR002",
                        "Body inputs are unavailable in this scope.",
                        field + ("from",),
                        name,
                    )
                continue
            if source is not MISSING and source in self.body.nodes:
                self.edges[name].add(source)
            self.reference(source, value.output, field, name)
        if node.depends_on is not MISSING:
            for index, dependency in enumerate(node.depends_on):
                if dependency in self.body.nodes:
                    self.edges[name].add(dependency)
                else:
                    self.error(
                        "IR002",
                        "The control dependency does not resolve in this scope.",
                        path + ("depends_on", index),
                        name,
                    )
        if node.independent_of is not MISSING:
            for index, independent in enumerate(node.independent_of):
                if independent not in inputs:
                    self.error(
                        "IR023",
                        "The independent input is not declared.",
                        path + ("independent_of", index),
                        name,
                    )

    def wrapper(self, name: str, node: FanOutNode | SubgraphNode) -> None:
        path = ("nodes", name)
        body = self.definitions.get(node.config.body)
        if isinstance(node, FanOutNode):
            if node.inputs is MISSING or node.config.over not in node.inputs:
                self.error(
                    "IR004",
                    "The mapped input is not declared.",
                    path + ("config", "over"),
                    name,
                )
            for output_name, output in outputs(node).items():
                if output.collect is MISSING:
                    self.error(
                        "IR004",
                        "The collected body output is not declared.",
                        path + ("outputs", output_name, "collect"),
                        name,
                    )
        if body is None:
            self.error(
                "IR002",
                "The body definition does not resolve.",
                path + ("config", "body"),
                name,
            )
            return
        self.calls.add(node.config.body)
        declared = {} if body.outputs is MISSING else body.outputs
        for output_name, output in outputs(node).items():
            if isinstance(node, FanOutNode):
                if output.collect is not MISSING and output.collect not in declared:
                    self.error(
                        "IR004",
                        "The collected body output is not declared.",
                        path + ("outputs", output_name, "collect"),
                        name,
                    )
            elif output_name not in declared:
                self.error(
                    "IR002",
                    "The body output is not declared.",
                    path + ("outputs", output_name),
                    name,
                )

    def branch_targets(self, name: str, node: BranchNode) -> Iterator[tuple[str, Path]]:
        path = ("nodes", name, "config")
        for case_index, case in enumerate(node.config.cases):
            for index, target in enumerate(case.to):
                yield target, path + ("cases", case_index, "to", index)
        if node.config.else_ is not MISSING:
            for index, target in enumerate(node.config.else_):
                yield target, path + ("else", index)

    def branches(self) -> None:
        owners: dict[str, set[str]] = {}
        targeted: set[str] = set()
        for name, node in self.body.nodes.items():
            if isinstance(node, BranchNode):
                for target, _ in self.branch_targets(name, node):
                    if target in self.body.nodes:
                        owners.setdefault(target, set()).add(name)
        for name, node in self.body.nodes.items():
            if not isinstance(node, BranchNode):
                continue
            targeted.add(name)
            for target, path in self.branch_targets(name, node):
                if target == "$fail_attempt":
                    if not isinstance(self.body, Subgraph):
                        self.error(
                            "IR010",
                            "The failure action requires a body scope.",
                            path,
                            name,
                        )
                    continue
                if target not in self.body.nodes:
                    self.error(
                        "IR010",
                        "The branch target does not resolve in this scope.",
                        path,
                        name,
                    )
                    continue
                targeted.add(target)
                if name not in self.controls[target]:
                    self.error(
                        "IR010",
                        "The target requires a direct branch control dependency.",
                        path,
                        name,
                    )
                if len(owners[target]) > 1:
                    self.error(
                        "IR010", "The target has multiple branch owners.", path, name
                    )
        for name in targeted:
            if self.body.nodes[name].attempt_invariant is True:
                self.error(
                    "IR010",
                    "Branches and direct targets cannot be attempt invariant.",
                    ("nodes", name, "attempt_invariant"),
                    name,
                )

    def validate(self) -> None:
        for name, node in self.body.nodes.items():
            self.inputs(name, node)
            if isinstance(node, FanOutNode | SubgraphNode):
                self.wrapper(name, node)
        self.branches()
        if isinstance(self.body, Subgraph) and self.body.outputs is not MISSING:
            for name, export in self.body.outputs.items():
                self.reference(export.from_, export.output, ("outputs", name))
        for name in cyclic_vertices(self.edges):
            self.error(
                "IR001",
                "The node participates in a dependency cycle.",
                ("nodes", name),
                name,
            )


def validate_graph_links(document: GraphDocument) -> GraphLinkResult:
    collector = DiagnosticCollector()
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    root = Scope(graph, ("graph",), definitions, collector)
    root.validate()
    if graph.target is not MISSING and isinstance(graph.target.key, OutputTarget):
        target = graph.target.key
        root.reference(target.from_, target.output, ("target", "key"), code="IR020")
    calls: dict[str, set[str]] = {}
    count = len(graph.nodes)
    for name, body in definitions.items():
        scope = Scope(body, ("graph", "subgraphs", name), definitions, collector)
        scope.validate()
        calls[name] = scope.calls
        count += len(body.nodes)
    for name in cyclic_vertices(calls):
        collector.add(
            Diagnostic(
                code="IR001",
                message="The body participates in a definition cycle.",
                path=("graph", "subgraphs", name),
                location=safe_source(definitions[name].source),
            )
        )
    result = GraphLinkResult(collector.finish(), count, len(definitions))
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            links_valid=result.links_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=count,
        )
    return result
