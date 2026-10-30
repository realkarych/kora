from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic, SourceLocation
from kora.ir.document import GraphDocument
from kora.ir.graph_links import outputs, safe_source
from kora.ir.json import JsonObject
from kora.ir.schema_catalogue import contract_schema
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import Location, SavedReferences
from kora.ir.schema_validation import validate_schema
from kora.ir.topology import AgentNode, Graph, HumanInputNode, Subgraph
from kora.ir.type_declarations import COMPONENT
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.inline_schemas.completed",
        COMPONENT,
        Level.INFO,
        "inline schema validation completed",
        {
            "inline_schemas_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "inline_schemas": FieldKind.INTEGER,
            "schemas": FieldKind.INTEGER,
            "references": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class InlineSchemaResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    inline_schemas_checked: int
    schemas_checked: int
    references_checked: int

    @property
    def inline_schemas_valid(self) -> bool:
        return self.diagnostics.valid


@dataclass(frozen=True)
class InlineSchema:
    root: JsonObject
    location: Location
    source: SourceLocation | None
    node: str | None = None


def body_schemas(body: Graph | Subgraph, location: Location) -> Iterator[InlineSchema]:
    if isinstance(body, Graph) and body.params is not MISSING:
        yield InlineSchema(
            body.params, location.child("params"), safe_source(body.source)
        )
    if isinstance(body, Subgraph) and body.inputs is not MISSING:
        for name, declaration in body.inputs.items():
            if declaration.schema_ is not MISSING:
                yield InlineSchema(
                    declaration.schema_,
                    location.child("inputs", name, "schema"),
                    safe_source(body.source),
                )
    for name, node in body.nodes.items():
        field = location.child("nodes", name)
        source = safe_source(node.source)
        for output, declaration in outputs(node).items():
            if declaration.schema_ is not MISSING:
                yield InlineSchema(
                    declaration.schema_,
                    field.child("outputs", output, "schema"),
                    source,
                    name,
                )
        if isinstance(node, AgentNode) and node.config.output_schema is not MISSING:
            yield InlineSchema(
                node.config.output_schema,
                field.child("config", "output_schema"),
                source,
                name,
            )
        if isinstance(node, HumanInputNode) and node.config.form is not MISSING:
            yield InlineSchema(
                node.config.form, field.child("config", "form"), source, name
            )


def validate_inline_schemas(document: GraphDocument) -> InlineSchemaResult:
    collector = DiagnosticCollector()
    node_name: str | None = None
    source: SourceLocation | None = None

    def fault(location: Location) -> None:
        collector.add(
            Diagnostic(
                code="IR022",
                message="The inline schema or reference is invalid.",
                path=location.path,
                node=node_name,
                location=source,
            )
        )

    roots = {
        name: root
        for name, contract in document.type_contracts.items()
        if (root := contract_schema(name, contract)) is not None
    }
    references = SavedReferences(roots)
    keywords = KeywordValidation(fault)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, Location(("graph",))),),
        (
            (body, Location(("graph", "subgraphs", name)))
            for name, body in definitions.items()
        ),
    )
    node_count = 0
    root_count = 0
    schema_count = 0
    reference_count = 0
    for body, location in bodies:
        node_count += len(body.nodes)
        for schema in body_schemas(body, location):
            node_name = schema.node
            source = schema.source
            counts = validate_schema(
                schema.root, schema.location, references, keywords, fault
            )
            root_count += 1
            schema_count += counts.schemas
            reference_count += counts.references
    result = InlineSchemaResult(
        collector.finish(),
        node_count,
        len(definitions),
        root_count,
        schema_count,
        reference_count,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            inline_schemas_valid=result.inline_schemas_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=node_count,
            definitions=result.definitions_checked,
            inline_schemas=root_count,
            schemas=schema_count,
            references=reference_count,
        )
    return result
