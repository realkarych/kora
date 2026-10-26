from dataclasses import dataclass
from typing import Final, cast

from kora.protocols.evidence import DispatcherClosure
from kora.protocols.history import (
    CLOSURE_LABEL,
    GRANT_HISTORY_LABEL,
    chain_seed,
    extend,
    record_bytes,
)
from kora.protocols.identity import GrantBinding
from tests.protocols.harness_documents import (
    COVERAGE,
    DIGEST,
    SESSION_SPEC,
    WORKSPACE_TARGET,
)

AT: Final[str] = "2026-10-25T12:00:00.000000Z"
SHA: Final[str] = "0" * 64
NAMESPACE: Final[str] = "ns-1"
ATTEMPT: Final[str] = "attempt-1"
DOMAIN: Final[str] = "domain-1"
LOCAL_PRINCIPAL: Final[dict[str, object]] = {
    "provider_instance": "local-git",
    "stable_id": "service-1",
    "kind": "local_service",
    "display_name": "kora local",
}
APP_PRINCIPAL: Final[dict[str, object]] = {
    "provider_instance": "github-app",
    "stable_id": "app-1",
    "kind": "app",
    "display_name": "kora app",
}
VCS_REF: Final[dict[str, object]] = {
    "repo": "/srv/git/repo.git",
    "branch": "kora/graph/run/node/1",
    "commit": "c0ffee",
    "base_commit": "beef",
    "changed_paths": ["src/app.py"],
    "diff_stat": "1 file changed",
    "pushed_by": LOCAL_PRINCIPAL,
}


def handle(**changes: object) -> dict[str, object]:
    return {
        "runtime": "local",
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "host_hint": None,
        "data": {"format": "local/1"},
        **changes,
    }


def quiescence(**changes: object) -> dict[str, object]:
    return {
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "dispatcher_id": "dispatcher-1",
        "worker_incarnation_id": "incarnation-1",
        "lease_generation": 1,
        "closed_at": AT,
        "evidence_ref": "registry-1",
        **changes,
    }


def termination(**changes: object) -> dict[str, object]:
    return {
        "kind": "termination",
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "retirement_id": "retirement-1",
        "stopped_at": AT,
        "evidence_ref": "registry-2",
        "scope": "env_sweep",
        **changes,
    }


def normal_completion(**changes: object) -> dict[str, object]:
    return {
        "kind": "normal_completion",
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "record_ref": "record-1",
        "observed_at": AT,
        "scope": "leader_group+env_sweep",
        **changes,
    }


def closure(**changes: object) -> dict[str, object]:
    return {
        "quiescence": quiescence(),
        "provenance": "registry_acknowledgement",
        "process": None,
        **changes,
    }


def outcome(**changes: object) -> dict[str, object]:
    return {
        "state": "not_applicable",
        "operation_id": None,
        "ref": None,
        "code": None,
        **changes,
    }


def closure_evidence(**changes: object) -> dict[str, object]:
    return {
        "runtime": normal_completion(),
        "dispatchers": [closure()],
        "publication": outcome(),
        **changes,
    }


def attempt_error(**changes: object) -> dict[str, object]:
    return {
        "code": "command_failed",
        "message": "the command exited with 2",
        "details": {"exit_code": 2},
        **changes,
    }


def terminal_report(**changes: object) -> dict[str, object]:
    return {
        "outcome": "succeeded",
        "error_class": None,
        "error": None,
        "lost_reason": None,
        "evidence": closure_evidence(),
        **changes,
    }


def grant(epoch: int, **changes: object) -> dict[str, object]:
    return {
        "dispatcher_id": f"dispatcher-{epoch}",
        "worker_id": "worker-1",
        "incarnation_id": "incarnation-1",
        "lease_epoch": epoch,
        "execution_domain_id": DOMAIN,
        "process": None,
        **changes,
    }


@dataclass(frozen=True)
class Scope:
    namespace_id: str
    attempt_id: str
    execution_domain_id: str


SCOPE: Final[Scope] = Scope(NAMESPACE, ATTEMPT, DOMAIN)


def scoped(scope: Scope) -> dict[str, object]:
    return {
        "namespace_id": scope.namespace_id,
        "attempt_id": scope.attempt_id,
        "execution_domain_id": scope.execution_domain_id,
    }


def grant_seed(scope: Scope = SCOPE) -> str:
    return chain_seed(
        GRANT_HISTORY_LABEL,
        scope.namespace_id,
        scope.attempt_id,
        scope.execution_domain_id,
    )


def closure_seed(scope: Scope = SCOPE) -> str:
    return chain_seed(
        CLOSURE_LABEL, scope.namespace_id, scope.attempt_id, scope.execution_domain_id
    )


def grant_digest(grants: list[dict[str, object]], scope: Scope = SCOPE) -> str:
    records = (record_bytes(GrantBinding.model_validate(item)) for item in grants)
    return extend(grant_seed(scope), records)


def closure_digest(closures: list[dict[str, object]], scope: Scope = SCOPE) -> str:
    records = (
        record_bytes(DispatcherClosure.model_validate(item)) for item in closures
    )
    return extend(closure_seed(scope), records)


def grant_snapshot(
    grants: list[dict[str, object]], scope: Scope = SCOPE
) -> dict[str, object]:
    return {
        "version": 1,
        **scoped(scope),
        "upper_epoch": grants[-1]["lease_epoch"] if grants else None,
        "count": len(grants),
        "digest": grant_digest(grants, scope),
    }


def grant_position(
    grants: list[dict[str, object]], scope: Scope = SCOPE
) -> dict[str, object]:
    return {
        "after_epoch": grants[-1]["lease_epoch"] if grants else None,
        "received_count": len(grants),
        "prefix_digest": grant_digest(grants, scope),
    }


def grant_page(
    history: list[dict[str, object]], start: int, end: int, scope: Scope = SCOPE
) -> dict[str, object]:
    return {
        "snapshot": grant_snapshot(history, scope),
        "start": grant_position(history[:start], scope),
        "grants": history[start:end],
        "end": grant_position(history[:end], scope),
        "complete": end == len(history),
    }


def closure_snapshot(
    closures: list[dict[str, object]], scope: Scope = SCOPE
) -> dict[str, object]:
    return {
        "version": 1,
        **scoped(scope),
        "generation": 1,
        "grant_snapshot": grant_snapshot(
            [grant(1, execution_domain_id=scope.execution_domain_id)], scope
        ),
        "count": len(closures),
        "digest": closure_digest(closures, scope),
    }


def closure_position(
    closures: list[dict[str, object]], scope: Scope = SCOPE
) -> dict[str, object]:
    return {
        "sequence": len(closures),
        "count": len(closures),
        "prefix_digest": closure_digest(closures, scope),
    }


def recovery_evidence(
    closures: list[dict[str, object]], scope: Scope = SCOPE, **changes: object
) -> dict[str, object]:
    pair = {
        "attempt_id": scope.attempt_id,
        "execution_domain_id": scope.execution_domain_id,
    }
    return {
        **pair,
        "handle": handle(**pair),
        "retired": True,
        "termination": termination(**pair),
        "closed_dispatchers": closure_snapshot(closures, scope),
        "normal_completion": None,
        "publication_outcome": outcome(),
        "observed_at": AT,
        **changes,
    }


def recovery_page(
    closures: list[dict[str, object]], start: int, end: int, scope: Scope = SCOPE
) -> dict[str, object]:
    complete = end == len(closures)
    return {
        "generation": 1,
        "evidence_snapshot": closure_snapshot(closures, scope),
        "start": closure_position(closures[:start], scope),
        "closed_dispatchers": closures[start:end],
        "end": closure_position(closures[:end], scope),
        "complete": complete,
        "observation": recovery_evidence(closures, scope) if complete else None,
    }


def dispatcher_closures(count: int) -> list[dict[str, object]]:
    return [
        closure(quiescence=quiescence(dispatcher_id=f"dispatcher-{index}"))
        for index in range(1, count + 1)
    ]


def acknowledge(
    closures: list[dict[str, object]], **changes: object
) -> dict[str, object]:
    return {
        "kind": "acknowledge",
        "generation": 1,
        "evidence_snapshot": closure_snapshot(closures),
        "end": closure_position(closures),
        "dispositions": [
            {
                "sequence": index,
                "item_digest": SHA,
                "status": "accepted",
                "reason": None,
            }
            for index in range(1, len(closures) + 1)
        ],
        **changes,
    }


def recovery_progress(**changes: object) -> dict[str, object]:
    return {
        "generation": 1,
        "snapshot": grant_snapshot([grant(1)]),
        "scanned": 1,
        "phase": "scanning",
        **changes,
    }


def plan_file(**changes: object) -> dict[str, object]:
    return {
        "area": "control",
        "path": "helpers/askpass",
        "content": "_wA=",
        "mode": 0o700,
        **changes,
    }


def plan_step(**changes: object) -> dict[str, object]:
    return {
        "argv": ["git", "clone", "{control_root}/repo"],
        "cwd": "control",
        "env": {"GIT_TERMINAL_PROMPT": "0"},
        "stdin": None,
        "timeout": "PT30S",
        "capture": "commit",
        "failure_class": None,
        **changes,
    }


def checkout_plan(**changes: object) -> dict[str, object]:
    return {
        "required_tools": ["git"],
        "credentials": {},
        "files": [plan_file()],
        "steps": [plan_step()],
        **changes,
    }


def publish_plan(**changes: object) -> dict[str, object]:
    return {
        "required_tools": ["git"],
        "files": [plan_file(area="publication")],
        "export": [plan_step(cwd="candidate", capture=None)],
        "prepare": [plan_step(cwd="publication", capture="commit")],
        "inspect": [plan_step(cwd="publication", capture="inspection")],
        "publish": [plan_step(cwd="publication", capture=None)],
        **changes,
    }


def prepared_publication(**changes: object) -> dict[str, object]:
    return {
        "attempt_id": ATTEMPT,
        "operation_id": "operation-1",
        "input_digest": DIGEST,
        "prepared_digest": DIGEST,
        "inspection_digest": DIGEST,
        "repo": "/srv/git/repo.git",
        "branch": "kora/graph/run/node/1",
        "commit": "c0ffee",
        "base_commit": "beef",
        "destination_digest": DIGEST,
        "changed_paths": ["src/app.py"],
        "diff_stat": "1 file changed",
        **changes,
    }


def credential_request(**changes: object) -> dict[str, object]:
    return {
        "binding": "local-bare",
        "binding_revision": 1,
        "credential_mode": "local_credential_free",
        "repo": "/srv/git/repo.git",
        "purpose": "checkout",
        "attempt_id": ATTEMPT,
        "operation_id": None,
        "prepared_digest": None,
        "destination_digest": None,
        **changes,
    }


def rights(**changes: object) -> dict[str, object]:
    return {
        "resource_scope": "repository",
        "native_permissions": ["contents_write"],
        "operations": ["write_refs"],
        **changes,
    }


def issued_credential(**changes: object) -> dict[str, object]:
    return {
        "binding": "local-bare",
        "binding_revision": 1,
        "credential_mode": "local_credential_free",
        "value": None,
        "principal": LOCAL_PRINCIPAL,
        "rights": None,
        "expires_at": None,
        "replacement": "not_applicable",
        "revocation": "not_applicable",
        **changes,
    }


def authorization(**changes: object) -> dict[str, object]:
    return {
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "dispatcher_id": "dispatcher-1",
        "operation_id": "operation-1",
        "prepared_digest": DIGEST,
        "destination_digest": DIGEST,
        "binding": "local-bare",
        "binding_revision": 1,
        "credential_mode": "local_credential_free",
        "principal": LOCAL_PRINCIPAL,
        "credential": None,
        **changes,
    }


def workload(**changes: object) -> dict[str, object]:
    return {
        "attempt_id": ATTEMPT,
        "execution_domain_id": DOMAIN,
        "dispatcher_id": "dispatcher-1",
        "execution_request": {"node": "fix"},
        "execution_request_digest": DIGEST,
        "kind": "agent",
        "resolved_config": {"model": "model-1"},
        "workspace": WORKSPACE_TARGET,
        "checkout": checkout_plan(),
        "publish": publish_plan(),
        "plan_executor": "runner",
        "env": {"LANG": "C.UTF-8"},
        "secret_refs": {"API_TOKEN": "secret://env/API_TOKEN"},
        "harness_credentials": {"ANTHROPIC_API_KEY": "secret://env/ANTHROPIC"},
        "artifact_inputs": {
            "plan": {
                "artifact_version_id": "version-1",
                "payload_digest": DIGEST,
                "type_name": "Plan",
            }
        },
        "resources": [resource_ref()],
        "limits": {"max_events": 100},
        "deadline": None,
        "session_spec": SESSION_SPEC,
        "dispatcher_process": None,
        **changes,
    }


def resource_ref(**changes: object) -> dict[str, object]:
    return {
        "name": "scripts/check.sh",
        "sha256": DIGEST,
        "size": 128,
        "executable": True,
        "storage_ref": "blob-1",
        **changes,
    }


def liveness(**changes: object) -> dict[str, object]:
    return {
        "observed_at": AT,
        "supervisor_alive": True,
        "candidate_alive": True,
        "journal_next": 4,
        "last_event_at": AT,
        "limited_events": 2,
        "events": 3,
        "input_tokens": 10,
        "output_tokens": 20,
        "usd": "0.01",
        "known_usd": "0.01",
        "pricing_status": "estimated",
        "usage_coverage": COVERAGE,
        "last_event_kind": "turn",
        **changes,
    }


def member(document: dict[str, object], key: str) -> dict[str, object]:
    value = document[key]
    if not isinstance(value, dict):
        raise TypeError(f"{key} is not an object")
    return cast("dict[str, object]", value)


def members(document: dict[str, object], key: str) -> list[dict[str, object]]:
    value = document[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} is not an array")
    items = cast("list[object]", value)
    if not all(isinstance(item, dict) for item in items):
        raise TypeError(f"{key} holds a non-object")
    return cast("list[dict[str, object]]", items)


WIDEST_EPOCH: Final[int] = 2**62
WIDEST: Final[str] = '"' * 256
WIDEST_PROCESS: Final[dict[str, object]] = {
    "host_id": WIDEST,
    "boot_id": WIDEST,
    "pid_namespace": WIDEST,
    "pid": 2**31 - 1,
    "start_time": 2**63 - 1,
    "leads_group": False,
}


def widest_unique(index: int) -> str:
    return "".join('"' if bit == "1" else "\\" for bit in f"{index:0256b}")


WIDEST_SCOPE: Final[Scope] = Scope(WIDEST, WIDEST, WIDEST)


def widest_grant(index: int) -> dict[str, object]:
    return grant(
        WIDEST_EPOCH + index,
        dispatcher_id=widest_unique(index),
        worker_id=WIDEST,
        incarnation_id=WIDEST,
        execution_domain_id=WIDEST,
        process=WIDEST_PROCESS,
    )


def widest_closure(index: int) -> dict[str, object]:
    return closure(
        quiescence=quiescence(
            attempt_id=WIDEST,
            execution_domain_id=WIDEST,
            dispatcher_id=widest_unique(index),
            worker_incarnation_id=WIDEST,
            lease_generation=2**63 - 1,
            evidence_ref=WIDEST,
        ),
        provenance="runtime_observed_process_exit",
        process=WIDEST_PROCESS,
    )
