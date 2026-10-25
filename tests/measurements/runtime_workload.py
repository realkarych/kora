import json
from collections.abc import Callable
from typing import Final

from pydantic import BaseModel, ValidationError

from kora.ir.json import MAX_CANONICAL_BYTES
from kora.ir.vcs_ref_json import MAX_JSON_BYTES
from kora.protocols.evidence import ClosureEvidence, DispatcherClosure
from kora.protocols.harness import MAX_PROMPT_BYTES, MAX_SESSION_FILES
from kora.protocols.history import (
    CLOSURE_LABEL,
    GRANT_HISTORY_LABEL,
    GrantHistoryPage,
    RecoveryEvidence,
    RecoveryPage,
    chain_seed,
    extend,
    record_bytes,
)
from kora.protocols.identity import GrantBinding
from kora.protocols.report import MAX_ERROR_BYTES, AttemptError, TerminalReport
from kora.protocols.runtime import (
    MAX_REFERENCE_BYTES,
    MAX_RESOURCES,
    MAX_TYPE_NAME_LENGTH,
    AdoptedSession,
    Workload,
)
from kora.protocols.vcs import (
    MAX_ARGUMENT_BYTES,
    MAX_MAP_ENTRIES,
    MAX_NAMED_ITEMS,
    MAX_PLAN_CONTENT_BYTES,
    MAX_PLAN_STEPS,
)
from tests.protocols import runtime_documents as documents
from tests.protocols.harness_documents import SESSION_SPEC
from tests.protocols.runtime_documents import (
    WIDEST,
    WIDEST_SCOPE,
    widest_closure,
    widest_grant,
)

Action = Callable[[], object]
JSON_OBJECT_ROOM: Final[int] = MAX_CANONICAL_BYTES - 64
CHAIN_LENGTHS: Final[tuple[int, ...]] = (1, 128, 129, 1000, 10_000)
CONTROL_FRAME_BYTES: Final[int] = 1_048_576
BODY_CAP_BYTES: Final[int] = 4_000_000
ESCAPED_ARGUMENT: Final[str] = '"' * MAX_ARGUMENT_BYTES
PLAN_ARGV_STEPS: Final[int] = MAX_PLAN_CONTENT_BYTES // (16 * MAX_ARGUMENT_BYTES) - 1
WIDEST_PATH: Final[str] = '"' * 4096


def encoded(document: dict[str, object]) -> bytes:
    return json.dumps(document).encode("utf-8")


def full_object() -> dict[str, object]:
    return {"blob": "x" * JSON_OBJECT_ROOM}


def full_reference() -> dict[str, object]:
    return {**documents.VCS_REF, "diff_stat": "d" * (MAX_JSON_BYTES - 4096)}


def env_name(index: int) -> str:
    return "K" + "X" * 60 + f"{index:03d}"


def widest_plan() -> dict[str, object]:
    argv = [ESCAPED_ARGUMENT] * 16
    budgeted = [documents.plan_step(argv=argv, capture=None)] * PLAN_ARGV_STEPS
    plain = [documents.plan_step(capture=None)] * (MAX_PLAN_STEPS - PLAN_ARGV_STEPS)
    return documents.checkout_plan(
        required_tools=[WIDEST] * MAX_NAMED_ITEMS,
        credentials={env_name(index): WIDEST for index in range(MAX_MAP_ENTRIES)},
        files=[],
        steps=budgeted + plain,
    )


def widest_publish_plan() -> dict[str, object]:
    plan = widest_plan()
    steps = documents.members(plan, "steps")
    publication = [{**step, "cwd": "publication"} for step in steps[1:]]
    return documents.publish_plan(
        required_tools=plan["required_tools"],
        files=[],
        export=[{**steps[0], "cwd": "candidate"}],
        prepare=publication[:21],
        inspect=publication[21:42],
        publish=publication[42:],
    )


def widest_session_spec() -> dict[str, object]:
    session_file = {
        "name": "f",
        "digest": "sha256:" + "0" * 64,
        "placement": "skill",
        "storage_ref": WIDEST,
    }
    return {
        **SESSION_SPEC,
        "options": full_object(),
        "price_table": full_object(),
        "prompt": "p" * (MAX_PROMPT_BYTES - 64),
        "files": [
            {**session_file, "name": f"file-{index}"}
            for index in range(MAX_SESSION_FILES)
        ],
    }


def maximal_workload() -> dict[str, object]:
    reference = "secret://env/" + "r" * (MAX_REFERENCE_BYTES - 13)
    artifact = {
        "artifact_version_id": WIDEST,
        "payload_digest": "sha256:" + "0" * 64,
        "type_name": "T" * MAX_TYPE_NAME_LENGTH,
    }
    resource = documents.resource_ref(
        name="/".join(['"' * 255] * 16), storage_ref=WIDEST
    )
    return documents.workload(
        execution_request=full_object(),
        resolved_config=full_object(),
        limits=full_object(),
        workspace={"root": WIDEST_PATH, "host_path": WIDEST_PATH},
        checkout=widest_plan(),
        publish=widest_publish_plan(),
        env={env_name(index): ESCAPED_ARGUMENT for index in range(MAX_MAP_ENTRIES)},
        secret_refs={env_name(index): reference for index in range(MAX_MAP_ENTRIES)},
        harness_credentials={
            env_name(index): reference for index in range(MAX_MAP_ENTRIES)
        },
        artifact_inputs={
            f"input-{index}": artifact for index in range(MAX_MAP_ENTRIES)
        },
        resources=[resource] * MAX_RESOURCES,
        session_spec=widest_session_spec(),
    )


def maximal_adopted_session() -> dict[str, object]:
    return {
        "handle": documents.handle(data=full_object()),
        "phase": "awaiting_authorization",
        "prepared": documents.prepared_publication(
            changed_paths=[
                f"src/{index:06d}.py".ljust(255, "p") for index in range(2000)
            ]
        ),
        "journal_next": 7,
        "observational": False,
    }


def maximal_recovery_evidence() -> dict[str, object]:
    return documents.recovery_evidence(
        [documents.closure()],
        handle=documents.handle(data=full_object()),
        publication_outcome=documents.outcome(
            state="completed", operation_id="op-1", ref=full_reference()
        ),
    )


def maximal_report() -> dict[str, object]:
    closures = [widest_closure(index) for index in range(1, 59)]
    widest = {"attempt_id": WIDEST, "execution_domain_id": WIDEST}
    message = "m" * 4096
    room = MAX_ERROR_BYTES - 4096 - 64
    return documents.terminal_report(
        outcome="failed",
        error_class="command_failed",
        error=documents.attempt_error(message=message, details={"t": "x" * room}),
        evidence=documents.closure_evidence(
            runtime=documents.normal_completion(**widest),
            dispatchers=closures,
            publication=documents.outcome(
                state="completed", operation_id="op-1", ref=full_reference()
            ),
        ),
    )


MAXIMAL: Final[dict[str, tuple[type[BaseModel], Callable[[], dict[str, object]]]]] = {
    "workload": (Workload, maximal_workload),
    "adopted_session": (AdoptedSession, maximal_adopted_session),
    "recovery_evidence": (RecoveryEvidence, maximal_recovery_evidence),
    "terminal_report": (TerminalReport, maximal_report),
}


def validating(model: type[BaseModel], payload: bytes) -> Action:
    return lambda: model.model_validate_json(payload)


def refusing(model: type[BaseModel], payload: bytes) -> Action:
    def action() -> object:
        try:
            model.model_validate_json(payload)
        except ValidationError as error:
            return error.error_count()
        raise AssertionError("the refusal workload was accepted")

    return action


def small_grants() -> list[dict[str, object]]:
    return [documents.grant(epoch) for epoch in range(1, 129)]


def widest_grants(count: int) -> list[dict[str, object]]:
    return [widest_grant(index) for index in range(1, count + 1)]


def widest_closures(count: int) -> list[dict[str, object]]:
    return [widest_closure(index) for index in range(1, count + 1)]


def broken_chain() -> dict[str, object]:
    page = documents.grant_page(widest_grants(67), 0, 67, WIDEST_SCOPE)
    grants = documents.members(page, "grants")
    grants[-1] = {**grants[-1], "worker_id": "changed"}
    return page


def oversized_error() -> dict[str, object]:
    return documents.attempt_error(details={"t": "x" * MAX_ERROR_BYTES})


def relayed_closures() -> dict[str, object]:
    return documents.closure_evidence(runtime=None, dispatchers=widest_closures(58))


def final_page(closures: list[dict[str, object]], count: int) -> bytes:
    return encoded(documents.recovery_page(closures[:count], 0, count, WIDEST_SCOPE))


def fits(model: type[BaseModel], payload: bytes) -> bool:
    try:
        model.model_validate_json(payload)
    except ValidationError:
        return False
    return True


def final_page_capacity(closures: list[dict[str, object]]) -> int:
    return max(
        count
        for count in range(1, len(closures) + 1)
        if fits(RecoveryPage, final_page(closures, count))
    )


def page_rows() -> dict[str, tuple[Action, int]]:
    small = small_grants()
    widest = widest_grants(68)
    closures = widest_closures(58)
    capacity = final_page_capacity(closures)
    return {
        "grant_page_128_small": (
            validating(GrantHistoryPage, encoded(documents.grant_page(small, 0, 128))),
            128,
        ),
        "grant_page_67_near_cap": (
            validating(
                GrantHistoryPage,
                encoded(documents.grant_page(widest[:67], 0, 67, WIDEST_SCOPE)),
            ),
            67,
        ),
        "closure_evidence_58_near_cap": (
            validating(ClosureEvidence, encoded(relayed_closures())),
            58,
        ),
        "recovery_page_57_near_cap": (
            validating(
                RecoveryPage,
                encoded(documents.recovery_page(closures, 0, 57, WIDEST_SCOPE)),
            ),
            57,
        ),
        "recovery_final_page_near_cap": (
            validating(RecoveryPage, final_page(closures, capacity)),
            capacity,
        ),
        "refused_page_68_near_cap": (
            refusing(
                GrantHistoryPage,
                encoded(documents.grant_page(widest, 0, 68, WIDEST_SCOPE)),
            ),
            68,
        ),
        "refused_broken_chain": (
            refusing(GrantHistoryPage, encoded(broken_chain())),
            67,
        ),
        "refused_oversized_error": (
            refusing(AttemptError, encoded(oversized_error())),
            1,
        ),
    }


def chain_rows() -> dict[str, tuple[Action, int]]:
    grant = record_bytes(GrantBinding.model_validate(widest_grant(1)))
    closure = record_bytes(DispatcherClosure.model_validate(widest_closure(1)))
    seeds = {
        "grant": (chain_seed(GRANT_HISTORY_LABEL, "ns", "att", "dom"), grant),
        "closure": (chain_seed(CLOSURE_LABEL, "ns", "att", "dom"), closure),
    }
    return {
        f"chain_{kind}_{length}": (
            chain_action(seed, record, length),
            length,
        )
        for kind, (seed, record) in seeds.items()
        for length in CHAIN_LENGTHS
    }


def chain_action(seed: str, record: bytes, length: int) -> Action:
    records = [record] * length
    return lambda: extend(seed, records)
