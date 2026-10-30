from typing import TYPE_CHECKING

from kora.ir.diagnostics import (
    ConfigWarning,
    ContractViolation,
    Diagnostic,
    SourceLocation,
)

if TYPE_CHECKING:
    from kora.ir.agent_session import (
        AgentSession,
        AgentSessionValidationError,
        agent_session_digest,
        canonical_agent_session,
        parse_agent_session,
        parse_agent_session_json,
        require_agent_session_revision,
        summarize_agent_session,
        verify_agent_session_transcript,
    )
    from kora.ir.command_result import (
        CommandResult,
        CommandResultValidationError,
        canonical_command_result,
        command_result_digest,
        parse_command_result,
        parse_command_result_json,
        require_command_result_revision,
        summarize_command_result,
        verify_command_result_captures,
    )
    from kora.ir.config import (
        AgentConfig,
        BranchCase,
        BranchConfig,
        CommandConfig,
        CommandResource,
        EventSelector,
        FanOutConfig,
        HumanInputBinds,
        HumanInputConfig,
        McpServer,
        NetworkPolicy,
        NodeLimits,
        Retry,
        SensorPoll,
        SubagentConfig,
        SubgraphNodeConfig,
        TimerConfig,
        WaitEventConfig,
        Workspace,
    )
    from kora.ir.contracts import (
        BuiltinTypeContract,
        CustomTypeContract,
        TypeBehavior,
        TypeContract,
    )
    from kora.ir.data import IrJsonObjectField, IrJsonValueField
    from kora.ir.declarations import (
        InputRef,
        IrSourceLocation,
        OutputDecl,
        OutputTarget,
        ParameterTarget,
        SubgraphOutput,
        TargetConfig,
    )
    from kora.ir.diagnostic_collection import (
        DiagnosticCollection,
        DiagnosticCollectionError,
        DiagnosticCollector,
    )
    from kora.ir.document import GraphDocument, Provenance, inspect_ir_root
    from kora.ir.export import export_ir, render_ir_json
    from kora.ir.graph_links import GraphLinkResult, validate_graph_links
    from kora.ir.node import ErrorClass, ToolPolicy
    from kora.ir.runtime import (
        Concurrency,
        ProviderRetry,
        Retention,
        RuntimeSection,
        SecretsSection,
    )
    from kora.ir.structured_output import (
        StructuredOutput,
        StructuredOutputValidationError,
        canonical_structured_output,
        parse_structured_output,
        parse_structured_output_json,
        require_structured_output_revision,
        structured_output_digest,
        summarize_structured_output,
    )
    from kora.ir.topology import (
        AgentNode,
        BranchNode,
        CommandNode,
        FanOutNode,
        Graph,
        GraphBody,
        HumanInputNode,
        Node,
        Subgraph,
        SubgraphNode,
        TimerNode,
        WaitEventNode,
    )
    from kora.ir.trigger import (
        CronTrigger,
        GraphTrigger,
        ManualTrigger,
        SensorTrigger,
        Trigger,
        WebhookAuth,
        WebhookTrigger,
    )
    from kora.ir.type_declarations import (
        TypeDeclarationResult,
        validate_type_declarations,
    )
    from kora.ir.vcs_ref import PrincipalRef, VcsRef

__all__ = [
    "TypeDeclarationResult",
    "validate_type_declarations",
    "DiagnosticCollection",
    "DiagnosticCollectionError",
    "DiagnosticCollector",
    "GraphLinkResult",
    "validate_graph_links",
    "AgentSession",
    "AgentSessionValidationError",
    "canonical_agent_session",
    "agent_session_digest",
    "parse_agent_session",
    "parse_agent_session_json",
    "require_agent_session_revision",
    "summarize_agent_session",
    "verify_agent_session_transcript",
    "CommandResult",
    "CommandResultValidationError",
    "canonical_command_result",
    "command_result_digest",
    "parse_command_result",
    "parse_command_result_json",
    "require_command_result_revision",
    "summarize_command_result",
    "verify_command_result_captures",
    "StructuredOutput",
    "StructuredOutputValidationError",
    "canonical_structured_output",
    "parse_structured_output",
    "parse_structured_output_json",
    "require_structured_output_revision",
    "structured_output_digest",
    "summarize_structured_output",
    "PrincipalRef",
    "VcsRef",
    "GraphDocument",
    "Provenance",
    "inspect_ir_root",
    "AgentNode",
    "CommandNode",
    "HumanInputNode",
    "WaitEventNode",
    "TimerNode",
    "BranchNode",
    "FanOutNode",
    "SubgraphNode",
    "Node",
    "GraphBody",
    "Subgraph",
    "Graph",
    "BuiltinTypeContract",
    "CustomTypeContract",
    "TypeBehavior",
    "TypeContract",
    "InputRef",
    "IrSourceLocation",
    "OutputDecl",
    "OutputTarget",
    "ParameterTarget",
    "SubgraphOutput",
    "TargetConfig",
    "Concurrency",
    "ProviderRetry",
    "Retention",
    "RuntimeSection",
    "SecretsSection",
    "CronTrigger",
    "GraphTrigger",
    "ManualTrigger",
    "SensorTrigger",
    "Trigger",
    "WebhookAuth",
    "WebhookTrigger",
    "IrJsonObjectField",
    "IrJsonValueField",
    "AgentConfig",
    "SubagentConfig",
    "HumanInputBinds",
    "HumanInputConfig",
    "EventSelector",
    "SensorPoll",
    "WaitEventConfig",
    "TimerConfig",
    "export_ir",
    "render_ir_json",
    "BranchCase",
    "BranchConfig",
    "CommandConfig",
    "CommandResource",
    "ConfigWarning",
    "ContractViolation",
    "Diagnostic",
    "ErrorClass",
    "FanOutConfig",
    "McpServer",
    "NetworkPolicy",
    "NodeLimits",
    "Retry",
    "SourceLocation",
    "SubgraphNodeConfig",
    "ToolPolicy",
    "Workspace",
]


def __getattr__(name: str) -> object:
    if name in {"TypeDeclarationResult", "validate_type_declarations"}:
        from kora.ir import type_declarations

        return getattr(type_declarations, name)
    if name in {
        "DiagnosticCollection",
        "DiagnosticCollectionError",
        "DiagnosticCollector",
    }:
        from kora.ir import diagnostic_collection

        return getattr(diagnostic_collection, name)
    if name in {"GraphLinkResult", "validate_graph_links"}:
        from kora.ir import graph_links

        return getattr(graph_links, name)
    if name in {
        "CommandResource",
        "NetworkPolicy",
        "McpServer",
        "BranchConfig",
        "FanOutConfig",
        "Workspace",
        "CommandConfig",
        "EventSelector",
        "SubagentConfig",
        "AgentConfig",
        "NodeLimits",
        "SensorPoll",
        "HumanInputBinds",
        "SubgraphNodeConfig",
        "WaitEventConfig",
        "Retry",
        "BranchCase",
        "TimerConfig",
        "HumanInputConfig",
    }:
        from kora.ir import config

        return getattr(config, name)
    if name in {
        "CustomTypeContract",
        "TypeContract",
        "BuiltinTypeContract",
        "TypeBehavior",
    }:
        from kora.ir import contracts

        return getattr(contracts, name)
    if name in {"IrJsonObjectField", "IrJsonValueField"}:
        from kora.ir import data

        return getattr(data, name)
    if name in {
        "OutputDecl",
        "SubgraphOutput",
        "IrSourceLocation",
        "OutputTarget",
        "TargetConfig",
        "InputRef",
        "ParameterTarget",
    }:
        from kora.ir import declarations

        return getattr(declarations, name)
    if name in {"inspect_ir_root", "GraphDocument", "Provenance"}:
        from kora.ir import document

        return getattr(document, name)
    if name in {"render_ir_json", "export_ir"}:
        from kora.ir import export

        return getattr(export, name)
    if name in {"ToolPolicy", "ErrorClass"}:
        from kora.ir import node

        return getattr(node, name)
    if name in {
        "Concurrency",
        "RuntimeSection",
        "SecretsSection",
        "ProviderRetry",
        "Retention",
    }:
        from kora.ir import runtime

        return getattr(runtime, name)
    if name in {
        "CommandNode",
        "SubgraphNode",
        "BranchNode",
        "GraphBody",
        "HumanInputNode",
        "Subgraph",
        "WaitEventNode",
        "Node",
        "TimerNode",
        "FanOutNode",
        "AgentNode",
        "Graph",
    }:
        from kora.ir import topology

        return getattr(topology, name)
    if name in {
        "Trigger",
        "GraphTrigger",
        "WebhookAuth",
        "WebhookTrigger",
        "ManualTrigger",
        "CronTrigger",
        "SensorTrigger",
    }:
        from kora.ir import trigger

        return getattr(trigger, name)
    if name in {
        "StructuredOutput",
        "StructuredOutputValidationError",
        "canonical_structured_output",
        "parse_structured_output",
        "parse_structured_output_json",
        "require_structured_output_revision",
        "structured_output_digest",
        "summarize_structured_output",
    }:
        from kora.ir import structured_output

        return getattr(structured_output, name)
    if name in {
        "AgentSession",
        "AgentSessionValidationError",
        "canonical_agent_session",
        "agent_session_digest",
        "parse_agent_session",
        "parse_agent_session_json",
        "require_agent_session_revision",
        "summarize_agent_session",
        "verify_agent_session_transcript",
    }:
        from kora.ir import agent_session

        return getattr(agent_session, name)
    if name in {
        "CommandResult",
        "CommandResultValidationError",
        "canonical_command_result",
        "command_result_digest",
        "parse_command_result",
        "parse_command_result_json",
        "require_command_result_revision",
        "summarize_command_result",
        "verify_command_result_captures",
    }:
        from kora.ir import command_result

        return getattr(command_result, name)
    if name in {"VcsRef", "PrincipalRef"}:
        from kora.ir import vcs_ref

        return getattr(vcs_ref, name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
