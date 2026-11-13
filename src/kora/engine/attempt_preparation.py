from pydantic import MISSING, TypeAdapter

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import prepare_claim_sources
from kora.db.claim_sources import ClaimSourceBinding, ClaimSourceReadLimits
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.engine.attempt_admission import PreparedAttemptAdmission
from kora.engine.attempt_inputs import prepare_attempt_inputs
from kora.engine.attempt_preparation_budget import (
    AttemptPreparationLimits,
    admit_attempt_preparation,
)
from kora.engine.claim_material import ClaimRuntimeSelection, prepare_claim_material
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_types import read_document
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.readiness import PreparedReadiness
from kora.engine.run_input_reads import ReadRunInput
from kora.engine.start_facts import declares_agent_secrets
from kora.ir.agent_secrets import NamespaceSecretPolicy, agent_secret_findings
from kora.ir.config import AgentConfig, CommandConfig
from kora.ir.cost import PriceRevision
from kora.ir.deployment_checks import DeploymentSelection, installation_findings
from kora.ir.graph_json import TextBudget
from kora.ir.json import JsonObject
from kora.ir.node import ToolPolicy
from kora.protocols.identity import Identifier
from kora.protocols.worker_claim_policy import prepare_claim_policy
from kora.protocols.worker_claim_wire import ClaimWireRejected

IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)


def _compatible(
    checked: CheckedExecutionContract,
    policy: JsonObject,
    installation: DeploymentSelection,
) -> None:
    document = read_document(
        checked.canonical_ir, TextBudget(len(checked.canonical_ir))
    )
    namespace_rules = ToolPolicy.model_validate(policy.get("tool_policy", {}))
    if (
        declares_agent_secrets(document)
        or not installation_findings(document, installation).valid
        or "daily_budget_usd" in policy
        or any(
            policy.get(name) is True
            for name in ("require_protected_execution", "require_protected_resources")
        )
        or namespace_rules.allow
        or namespace_rules.deny
        or document.runtime.protected_paths not in (MISSING, ())
    ):
        raise ClaimWireRejected()
    secret_policy = NamespaceSecretPolicy(
        policy.get("allow_unenforced_agent_secrets") is True,
        policy.get("require_network_enforcement") is True,
    )
    if not agent_secret_findings(
        document, {"local": installation.runtime}, secret_policy
    ).valid:
        raise ClaimWireRejected()
    for node in document.graph.nodes.values():
        config = node.config
        if not isinstance(config, AgentConfig | CommandConfig):
            continue
        if node.protected_paths not in (MISSING, ()) or (
            config.network_policy is not MISSING
            and not installation.runtime.enforces_network_policy
        ):
            raise ClaimWireRejected()
        if isinstance(config, AgentConfig) and config.tool_policy is not MISSING:
            if config.tool_policy.allow or config.tool_policy.deny:
                raise ClaimWireRejected()


def prepare_attempt_admission(
    checked: CheckedExecutionContract,
    readiness: PreparedReadiness,
    params: ReadRunInput,
    binding: ClaimSourceBinding,
    price: PriceRevision,
    policy: NamespacePolicyConfiguration,
    selection: ClaimRuntimeSelection,
    installation: DeploymentSelection,
    *,
    sources: ClaimSourceReadLimits,
    limits: AttemptPreparationLimits,
) -> PreparedAttemptAdmission:
    try:
        admit_attempt_preparation(checked, limits)
        scope, pin = checked.scope, checked.pin
        node = next(
            (node for node in readiness.nodes if node.path == binding.node_path), None
        )
        if (
            not readiness.matches(scope, pin)
            or node is None
            or node.kind not in ("agent", "command")
            or node.predecessors
            or node.inputs
            or node.trigger_rule != "all_success"
            or params.binding != binding.run
            or policy.namespace_id != scope.namespace_id
            or price.id != binding.price_table_id
            or not 1 <= len(binding.node_path) <= 1024
            or any(not "!" <= character <= "~" for character in binding.node_path)
        ):
            raise ClaimWireRejected()
        for identifier in (
            binding.run.namespace_id,
            binding.run.run_id,
            binding.attempt_id,
            selection.runtime,
        ):
            IDENTIFIER.validate_python(identifier)
        value = policy.value()
        _compatible(checked, value, installation)
        inputs = prepare_attempt_inputs({}, {})
        material = prepare_claim_material(
            checked, params, binding.node_path, inputs.claim_map(), price, selection
        )
        prepare_claim_policy(value, policy.revision, material.node_policy)
        prepared_sources = prepare_claim_sources(
            {
                name: inputs.storage if name == "input_bindings" else None
                for name in SOURCE_FIELDS
            },
            dict(zip(SOURCE_FIELDS, sources.fields, strict=True)),
        )
        return PreparedAttemptAdmission(
            binding,
            pin,
            node.kind,
            selection.runtime,
            policy.revision,
            prepared_sources,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        del checked, readiness, params, binding, price, policy, selection, installation
        raise_safe(ClaimWireRejected())
