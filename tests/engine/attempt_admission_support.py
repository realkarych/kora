from dataclasses import dataclass, replace
from datetime import timedelta
from typing import cast

from sqlalchemy import select

from kora.db.claim_sources import ClaimSourceBinding
from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.run_input_reads import RunInputBinding, RunInputField
from kora.db.tables import graph_version, node_instance
from kora.engine.attempt_admission import PreparedAttemptAdmission
from kora.engine.attempt_preparation import prepare_attempt_admission
from kora.engine.attempt_preparation_budget import AttemptPreparationLimits
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.engine.price_initialization import (
    EARLIEST_PRICE_TIME,
    PriceConfiguration,
    import_price_revision,
)
from kora.engine.readiness import PreparedReadiness, prepare_readiness
from kora.engine.run_input_reads import ReadRunInput, read_run_input
from kora.ir.agent_secrets import RuntimeEnforcement
from kora.ir.cost import PriceRevision
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from tests.engine.admission_wait_support import LIMITS as SOURCE_LIMITS
from tests.engine.promotion_support import PromotionCase, append_pending, promotion_case
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS

SELECTION = ClaimRuntimeSelection("local", "git", timedelta(minutes=5))
INSTALLATION = DeploymentSelection("claude", RuntimeEnforcement(False, False))
PREPARATION_LIMITS = AttemptPreparationLimits(
    LIMITS, 150_000_000, 2_000_000_000, 10**18
)


@dataclass(frozen=True)
class AttemptAdmissionCase:
    promotion: PromotionCase
    checked: CheckedExecutionContract
    readiness: PreparedReadiness
    params: ReadRunInput
    binding: ClaimSourceBinding
    price: PriceRevision
    policy: NamespacePolicyConfiguration

    def prepare(
        self,
        installation: DeploymentSelection = INSTALLATION,
        limits: AttemptPreparationLimits = PREPARATION_LIMITS,
    ) -> PreparedAttemptAdmission:
        return prepare_attempt_admission(
            self.checked,
            self.readiness,
            self.params,
            self.binding,
            self.price,
            self.policy,
            SELECTION,
            installation,
            sources=SOURCE_LIMITS,
            limits=limits,
        )


async def attempt_admission_case(database: Database) -> AttemptAdmissionCase:
    promotion = await promotion_case(database)
    await promotion.promote(database)
    scope = promotion.scope
    policy = NamespacePolicyConfiguration.prepare(
        scope.namespace_id, 1, {"max_pending_runs": 1000}
    )
    price = PriceConfiguration(None, EARLIEST_PRICE_TIME, {}).prepare(
        promotion.identifiers.new()
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(namespace_policy_scopes(scope.namespace_id))
        await write_namespace_policy(writer, locks, policy, promotion.clock.now())
        price = await import_price_revision(writer, locks, price, promotion.clock.now())
        node_id = await writer.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == "agent",
            )
        )
    assert isinstance(node_id, str)
    binding = ClaimSourceBinding(
        RunInputBinding(
            scope.namespace_id, scope.graph_id, scope.version_id, promotion.run_id
        ),
        node_id,
        promotion.identifiers.new(),
        price.id,
        "agent",
    )
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        references = await reader.scalar(
            select(graph_version.c.resources).where(
                graph_version.c.id == scope.version_id
            )
        )
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, promotion.pin, scope, cast(JsonObject, references), (), limits=LIMITS
    )
    params = await read_run_input(
        database, binding.run, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert params is not None
    readiness = prepare_readiness(checked, max_ir_bytes=len(checked.canonical_ir))
    return AttemptAdmissionCase(
        promotion, checked, readiness, params, binding, price, policy
    )


async def append_admission_case(
    database: Database, previous: AttemptAdmissionCase
) -> AttemptAdmissionCase:
    promotion = await append_pending(database, previous.promotion)
    await promotion.promote(database)
    async with database.read_transaction() as reader:
        node_id = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == previous.binding.node_path,
            )
        )
    assert isinstance(node_id, str)
    binding = replace(
        previous.binding,
        run=replace(previous.binding.run, run_id=promotion.run_id),
        node_id=node_id,
        attempt_id=promotion.identifiers.new(),
    )
    params = await read_run_input(
        database, binding.run, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert params is not None
    return replace(previous, promotion=promotion, params=params, binding=binding)
