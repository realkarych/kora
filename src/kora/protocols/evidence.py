from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self, cast

from pydantic import Field, StringConstraints, Tag, model_validator
from pydantic_core import PydanticCustomError

from kora.ir import VcsRef
from kora.protocols.boundary import refusal_discriminator
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import ERROR_CODE
from kora.protocols.identity import (
    Count,
    Identifier,
    Instant,
    ProcessIdentity,
    encoded_size,
    object_size,
)

MAX_DISPATCHERS: Final[int] = 128
MAX_ENVELOPE_BYTES: Final[int] = 262_144

Code = Annotated[str, StringConstraints(strict=True, pattern=f"^{ERROR_CODE.pattern}$")]


def evidence_refused() -> PydanticCustomError:
    return PydanticCustomError("evidence_invalid", "the evidence is invalid")


class EvidenceScope(StrEnum):
    SUBREAPER_LEADER_GROUP_ENV_SWEEP = "subreaper+leader_group+env_sweep"
    LEADER_GROUP_ENV_SWEEP = "leader_group+env_sweep"
    ENV_SWEEP = "env_sweep"
    NO_RUNNER_STARTED = "no_runner_started"
    FAKE_RUNTIME = "fake_runtime"


class DispatcherQuiescence(BoundaryModel):
    attempt_id: Identifier
    execution_domain_id: Identifier
    dispatcher_id: Identifier
    worker_incarnation_id: Identifier
    lease_generation: Count
    closed_at: Instant
    evidence_ref: Identifier


class TerminationReceipt(BoundaryModel):
    kind: Literal["termination"]
    attempt_id: Identifier
    execution_domain_id: Identifier
    retirement_id: Identifier
    stopped_at: Instant
    evidence_ref: Identifier
    scope: EvidenceScope


class NormalCompletionQuiescence(BoundaryModel):
    kind: Literal["normal_completion"]
    attempt_id: Identifier
    execution_domain_id: Identifier
    record_ref: Identifier
    observed_at: Instant
    scope: EvidenceScope


class DispatcherClosure(BoundaryModel):
    quiescence: DispatcherQuiescence
    provenance: Literal["registry_acknowledgement", "runtime_observed_process_exit"]
    process: ProcessIdentity | None

    @model_validator(mode="after")
    def process_matches_provenance(self) -> Self:
        observed = self.provenance == "runtime_observed_process_exit"
        if observed != (self.process is not None):
            raise evidence_refused()
        return self

    def dispatcher_key(self) -> tuple[str, int]:
        return self.quiescence.dispatcher_id, self.quiescence.lease_generation


def dispatchers_envelope_size(closures: tuple[DispatcherClosure, ...]) -> int:
    return object_size({"dispatchers": encoded_size(closures)})


class PublicationOutcome(BoundaryModel):
    state: Literal["not_applicable", "not_dispatched", "unknown", "completed", "failed"]
    operation_id: Identifier | None
    ref: VcsRef | None
    code: Code | None

    @model_validator(mode="after")
    def fields_match_state(self) -> Self:
        if (self.state == "completed") != (self.ref is not None):
            raise evidence_refused()
        if (self.state == "failed") != (self.code is not None):
            raise evidence_refused()
        if self.state in {"completed", "failed"} and self.operation_id is None:
            raise evidence_refused()
        if self.state == "not_applicable" and self.operation_id is not None:
            raise evidence_refused()
        return self


def record_kind(value: object) -> object:
    if isinstance(value, Mapping):
        return cast("Mapping[object, object]", value).get("kind")
    return getattr(value, "kind", None)


RuntimeRecord = Annotated[
    Annotated[TerminationReceipt, Tag("termination")]
    | Annotated[NormalCompletionQuiescence, Tag("normal_completion")],
    refusal_discriminator(record_kind, evidence_refused),
]


def unique_dispatchers(closures: tuple[DispatcherClosure, ...]) -> bool:
    keys = {closure.dispatcher_key() for closure in closures}
    return len(keys) == len(closures)


class ClosureEvidence(BoundaryModel):
    runtime: RuntimeRecord | None
    dispatchers: Annotated[
        tuple[DispatcherClosure, ...], Field(max_length=MAX_DISPATCHERS)
    ]
    publication: PublicationOutcome | None

    @model_validator(mode="after")
    def bounded_dispatchers(self) -> Self:
        if not unique_dispatchers(self.dispatchers):
            raise evidence_refused()
        if dispatchers_envelope_size(self.dispatchers) > MAX_ENVELOPE_BYTES:
            raise evidence_refused()
        if len(self.scopes()) > 1:
            raise evidence_refused()
        return self

    def scopes(self) -> set[tuple[str, str]]:
        scopes = {
            (closure.quiescence.attempt_id, closure.quiescence.execution_domain_id)
            for closure in self.dispatchers
        }
        if self.runtime is not None:
            scopes.add((self.runtime.attempt_id, self.runtime.execution_domain_id))
        return scopes
