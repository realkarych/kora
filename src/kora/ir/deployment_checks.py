from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.agent_secrets import (
    NamespaceSecretPolicy,
    RuntimeEnforcement,
    agent_secret_findings,
)
from kora.ir.approval_defaults import validate_approval_defaults
from kora.ir.approval_predicates import validate_approval_response_predicates
from kora.ir.branch_expressions import validate_branch_expressions
from kora.ir.config import AgentConfig, CommandConfig
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.edge_types import validate_edge_types
from kora.ir.export_types import validate_export_types
from kora.ir.expression_syntax import validate_expression_syntax
from kora.ir.fan_out_types import validate_fan_out_types
from kora.ir.first_slice_configuration import validate_first_slice_configuration
from kora.ir.graph_links import COMPONENT, safe_source, validate_graph_links
from kora.ir.inline_schemas import validate_inline_schemas
from kora.ir.input_paths import validate_input_paths
from kora.ir.output_schemas import validate_output_schemas
from kora.ir.retry_expressions import validate_retry_expressions
from kora.ir.root_previous_attempts import validate_root_previous_attempt_refs
from kora.ir.secret_aliases import validate_secret_aliases
from kora.ir.subgraph_interfaces import validate_subgraph_interfaces
from kora.ir.target_parameter_types import validate_known_target_parameter_types
from kora.ir.tool_policies import validate_tool_policies
from kora.ir.topology import Node
from kora.ir.trigger_expressions import validate_sensor_trigger_expressions
from kora.ir.type_declarations import validate_type_declarations
from kora.ir.type_schemas import validate_type_schemas
from kora.ir.wait_expressions import validate_wait_expressions
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

Path = tuple[str | int, ...]
LOCAL_RUNTIME: str = "local"
INSTALLATION_HINT: str = (
    "Remove the unsupported configuration or use an installation that implements it."
)
HARNESS_HINT: str = "Select the harness this installation is configured with."
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.deployment_checks.completed",
        COMPONENT,
        Level.DEBUG,
        "deployment document checks completed",
        {
            "complete": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "warnings": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True, slots=True)
class DeploymentSelection:
    harness: str
    runtime: RuntimeEnforcement


@dataclass(frozen=True, slots=True)
class DeploymentCheckResult:
    diagnostics: DiagnosticCollection
    complete: bool

    @property
    def deployable(self) -> bool:
        return self.complete and self.diagnostics.valid


def _pass_outcomes(
    document: GraphDocument,
) -> tuple[tuple[DiagnosticCollection, bool], ...]:
    links = validate_graph_links(document)
    declarations = validate_type_declarations(document)
    type_schemas = validate_type_schemas(document)
    inline_schemas = validate_inline_schemas(document)
    output_schemas = validate_output_schemas(document)
    edges = validate_edge_types(document)
    exports = validate_export_types(document)
    interfaces = validate_subgraph_interfaces(document)
    fan_outs = validate_fan_out_types(document)
    syntax = validate_expression_syntax(document)
    input_paths = validate_input_paths(document)
    secrets = validate_secret_aliases(document)
    policies = validate_tool_policies(document)
    previous = validate_root_previous_attempt_refs(document)
    targets = validate_known_target_parameter_types(document)
    defaults = validate_approval_defaults(document)
    predicates = validate_approval_response_predicates(document)
    branches = validate_branch_expressions(document)
    retries = validate_retry_expressions(document)
    waits = validate_wait_expressions(document)
    sensors = validate_sensor_trigger_expressions(document)
    configuration = validate_first_slice_configuration(document)
    return (
        (links.diagnostics, links.links_valid),
        (declarations.diagnostics, declarations.declarations_valid),
        (type_schemas.diagnostics, type_schemas.schemas_valid),
        (inline_schemas.diagnostics, inline_schemas.inline_schemas_valid),
        (output_schemas.diagnostics, output_schemas.output_schemas_valid),
        (edges.diagnostics, edges.edge_types_valid),
        (exports.diagnostics, exports.export_types_valid),
        (interfaces.diagnostics, interfaces.interfaces_valid),
        (fan_outs.diagnostics, fan_outs.fan_out_types_valid),
        (syntax.diagnostics, syntax.syntax_valid),
        (input_paths.diagnostics, input_paths.input_paths_valid),
        (secrets.diagnostics, secrets.secret_aliases_valid),
        (policies.diagnostics, policies.tool_policies_valid),
        (previous.diagnostics, previous.root_previous_attempt_refs_valid),
        (targets.diagnostics, targets.known_target_parameter_types_valid),
        (defaults.diagnostics, defaults.approval_defaults_valid),
        (predicates.diagnostics, predicates.approval_predicates_complete),
        (branches.diagnostics, branches.branch_expressions_complete),
        (retries.diagnostics, retries.retry_expressions_complete),
        (waits.diagnostics, waits.wait_expressions_complete),
        (sensors.diagnostics, sensors.expressions_valid),
        (configuration.diagnostics, configuration.authored_configuration_supported),
    )


class InstallationChecks:
    def __init__(self, selection: DeploymentSelection) -> None:
        self.selection = selection
        self.collector = DiagnosticCollector()

    def refuse(
        self, code: str, message: str, path: Path, node: Node, name: str
    ) -> None:
        self.collector.add(
            Diagnostic(
                code=code,
                message=message,
                path=path,
                location=safe_source(node.source),
                node=name,
                hint=HARNESS_HINT if code == "IR018" else INSTALLATION_HINT,
            )
        )

    def node(self, node: Node, name: str, path: Path) -> None:
        config = node.config
        if isinstance(config, AgentConfig) and config.harness != self.selection.harness:
            self.refuse(
                "IR018",
                "The harness is not configured in this installation.",
                path + ("config", "harness"),
                node,
                name,
            )
        if not isinstance(config, AgentConfig | CommandConfig):
            return
        if config.runtime is not MISSING and config.runtime != LOCAL_RUNTIME:
            self.refuse(
                "IR028",
                "Only the local runtime is available in this installation.",
                path + ("config", "runtime"),
                node,
                name,
            )
        if (
            isinstance(config, CommandConfig)
            and config.resources is not MISSING
            and config.resources
        ):
            self.refuse(
                "IR028",
                "Command files are unavailable in this installation.",
                path + ("config", "resources"),
                node,
                name,
            )


def installation_findings(
    document: GraphDocument, selection: DeploymentSelection
) -> DiagnosticCollection:
    checks = InstallationChecks(selection)
    graph = document.graph
    if document.runtime.runtime != LOCAL_RUNTIME:
        checks.collector.add(
            Diagnostic(
                code="IR028",
                message="Only the local runtime is available in this installation.",
                path=("runtime", "runtime"),
                hint=INSTALLATION_HINT,
            )
        )
    for name, node in graph.nodes.items():
        checks.node(node, name, ("graph", "nodes", name))
    bodies = {} if graph.subgraphs is MISSING else graph.subgraphs
    for body_name, body in bodies.items():
        for name, node in body.nodes.items():
            checks.node(node, name, ("graph", "subgraphs", body_name, "nodes", name))
    return checks.collector.finish()


def merge_diagnostics(
    collections: tuple[DiagnosticCollection, ...],
) -> DiagnosticCollection:
    collector = DiagnosticCollector()
    errors_omitted = 0
    warnings_omitted = 0
    for collection in collections:
        for error in collection.errors:
            collector.add(error)
        for warning in collection.warnings:
            collector.add(warning, "warning")
        errors_omitted += collection.errors_omitted
        warnings_omitted += collection.warnings_omitted
    merged = collector.finish()
    return DiagnosticCollection(
        merged.errors,
        merged.warnings,
        merged.errors_omitted + errors_omitted,
        merged.warnings_omitted + warnings_omitted,
    )


def check_deployment_document(
    document: GraphDocument,
    selection: DeploymentSelection,
    policy: NamespaceSecretPolicy,
) -> DeploymentCheckResult:
    outcomes = (
        *_pass_outcomes(document),
        (installation_findings(document, selection), True),
        (
            agent_secret_findings(document, {LOCAL_RUNTIME: selection.runtime}, policy),
            True,
        ),
    )
    diagnostics = merge_diagnostics(tuple(collection for collection, _ in outcomes))
    complete = not diagnostics.valid or all(established for _, established in outcomes)
    observed = current_diagnostics()
    if observed is not None:
        observed.logger(COMPONENT).emit(
            COMPLETED,
            complete=complete,
            errors=len(diagnostics.errors) + diagnostics.errors_omitted,
            warnings=len(diagnostics.warnings) + diagnostics.warnings_omitted,
        )
    return DeploymentCheckResult(diagnostics, complete)
