from dataclasses import dataclass

from kora.db.graph_value_json import TextBudget
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_types import read_document
from kora.engine.usage_acceptance import UsageMapping, UsageRefusal, UsageRejected
from kora.ir.config import AgentConfig, CommandConfig
from kora.ir.cost import Basis, BillingClass


@dataclass(frozen=True, repr=False)
class UsageContext:
    namespace_id: str
    run_id: str
    graph_id: str
    graph_version_id: str
    execution_contract_digest: str
    node_path: str
    mapping: UsageMapping


def prepare_usage_context(
    checked: CheckedExecutionContract,
    run_id: str,
    node_path: str,
    *,
    max_ir_bytes: int,
) -> UsageContext:
    document = read_document(checked.canonical_ir, TextBudget(max_ir_bytes))
    selected = document.graph.nodes.get(node_path)
    allowed: frozenset[tuple[Basis, BillingClass]] = frozenset()
    corrections: frozenset[tuple[Basis, Basis]] = frozenset()
    if selected is None:
        raise UsageRejected(UsageRefusal.MAPPING)
    config = selected.config
    if isinstance(config, AgentConfig):
        family = config.harness.partition(":")[0]
        if family == "claude":
            allowed = frozenset(
                (basis, billing)
                for basis in ("price_table", "harness_list")
                for billing in ("api_key", "subscription", "managed")
            )
            corrections = frozenset({("price_table", "harness_list")})
        elif family in {"codex", "fake"}:
            allowed = frozenset({("price_table", "api_key")})
        else:
            raise UsageRejected(UsageRefusal.MAPPING)
    elif not isinstance(config, CommandConfig):
        raise UsageRejected(UsageRefusal.MAPPING)
    return UsageContext(
        checked.scope.namespace_id,
        run_id,
        checked.scope.graph_id,
        checked.scope.version_id,
        checked.pin.execution_contract_digest,
        node_path,
        UsageMapping(allowed, corrections, False),
    )
