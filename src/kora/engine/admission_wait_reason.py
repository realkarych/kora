import re
from datetime import datetime

from kora.db.attempt_capacity import RunRecovery
from kora.db.identifiers import is_identifier
from kora.db.native_values import native_value_bound
from kora.db.types import JsonObject, canonical_json, normalise_timestamp
from kora.engine.attempt_capacity import CapacityLimit
from kora.engine.graph_resource_references import GraphResourceScope

MAX_REASON_BYTES: int = 4096
MAX_REASON_NATIVE_UNITS: int = 16_384
MAX_OBSERVED_COUNT: int = 2**63 - 1
LIMIT_NAMES: dict[str, tuple[str, ...]] = {
    "global": ("max_running_attempts",),
    "namespace": ("max_running_attempts",),
    "graph": ("max_running_nodes", "max_running_runs"),
    "runtime": ("max_concurrent",),
}


class AdmissionReasonRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the admission reason is unavailable")


def _count(value: int) -> bool:
    return type(value) is int and 0 < value <= MAX_OBSERVED_COUNT


def admission_wait_reason(
    observation: CapacityLimit | RunRecovery,
    scope: GraphResourceScope,
    runtime: str,
    observed_at: datetime,
) -> JsonObject:
    reason: JsonObject = {
        "version": 1,
        "observed_at": normalise_timestamp(observed_at).isoformat(
            timespec="microseconds"
        ),
    }
    if isinstance(observation, RunRecovery):
        ids = observation.attempt_ids
        if (
            not _count(observation.count)
            or len(ids) != min(observation.count, 20)
            or not all(is_identifier(identifier) for identifier in ids)
            or tuple(sorted(set(ids))) != ids
        ):
            raise AdmissionReasonRejected()
        reason.update(
            kind="execution_recovery",
            blockers={
                "count": observation.count,
                "ids": list(ids),
                "truncated": observation.count > len(ids),
            },
        )
    else:
        source = observation.source_graph_version_id
        identifier = {
            "global": None,
            "namespace": scope.namespace_id,
            "graph": scope.graph_id,
            "runtime": runtime,
        }[observation.scope]
        if (
            observation.name not in LIMIT_NAMES[observation.scope]
            or not _count(observation.limit)
            or not _count(observation.used)
            or observation.limit > observation.used
            or (
                source is None or not is_identifier(source)
                if observation.scope == "graph"
                else source is not None
            )
            or (
                not isinstance(identifier, str) or not is_identifier(identifier)
                if observation.scope in ("namespace", "graph")
                else observation.scope == "runtime"
                and re.fullmatch(r"[\x21-\x7e]{1,255}", runtime) is None
            )
        ):
            raise AdmissionReasonRejected()
        reason.update(
            kind="concurrency_limit",
            scope={"kind": observation.scope, "id": identifier},
            limit_name=observation.name,
            limit=observation.limit,
            used=observation.used,
            source_graph_version_id=source,
        )
    if len(canonical_json(reason).encode("utf-8")) > MAX_REASON_BYTES:
        raise AdmissionReasonRejected()
    native_value_bound(reason, MAX_REASON_NATIVE_UNITS)
    return reason
