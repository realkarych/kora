import base64
import hashlib
from collections.abc import Sequence
from typing import Final, Protocol

from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.deadlines import duration_ns
from kora.protocols.vcs import (
    CheckoutPlan,
    InspectionManifest,
    PlanFile,
    PlanStep,
    PublishPlan,
)

DIGEST_PREFIX: Final[str] = "sha256:"
OPERATION_PREFIX: Final[str] = "op-"
OPERATION_HEX: Final[int] = 32


class Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


def sha256_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def hasher_digest(hasher: Hasher) -> str:
    return DIGEST_PREFIX + hasher.hexdigest()


def object_digest(document: JsonObject) -> str:
    return sha256_digest(canonical_bytes(document))


def encoded(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def file_document(plan_file: PlanFile) -> JsonObject:
    return {
        "area": plan_file.area,
        "path": plan_file.path,
        "content": encoded(plan_file.content),
        "mode": plan_file.mode,
    }


def step_document(step: PlanStep) -> JsonObject:
    stdin: JsonValue = None if step.stdin is None else encoded(step.stdin)
    return {
        "argv": list(step.argv),
        "cwd": step.cwd,
        "env": dict(step.env),
        "stdin": stdin,
        "timeout": duration_ns(step.timeout),
        "capture": step.capture,
        "failure_class": step.failure_class,
    }


def steps_document(steps: Sequence[PlanStep]) -> list[JsonValue]:
    return [step_document(step) for step in steps]


def plan_digest(plan: CheckoutPlan | PublishPlan) -> str:
    common: JsonObject = {
        "required_tools": list(plan.required_tools),
        "files": [file_document(plan_file) for plan_file in plan.files],
    }
    if isinstance(plan, CheckoutPlan):
        return object_digest(
            {
                **common,
                "kind": "checkout",
                "credentials": dict(plan.credentials),
                "steps": steps_document(plan.steps),
            }
        )
    return object_digest(
        {
            **common,
            "kind": "publish",
            "export": steps_document(plan.export),
            "prepare": steps_document(plan.prepare),
            "inspect": steps_document(plan.inspect),
            "publish": steps_document(plan.publish),
        }
    )


def input_digest(file_digests: Sequence[str]) -> str:
    return sha256_digest("\n".join(file_digests).encode("ascii"))


def prepared_digest(
    commit: str,
    commit_bytes: bytes,
    base_commit: str,
    sealed_digest: str,
    publish_plan_digest: str,
) -> str:
    return object_digest(
        {
            "commit": commit,
            "commit_bytes": encoded(commit_bytes),
            "base_commit": base_commit,
            "input_digest": sealed_digest,
            "plan_digest": publish_plan_digest,
        }
    )


def inspection_digest(index_digest: str, manifest: InspectionManifest) -> str:
    return object_digest(
        {
            "content_index": index_digest,
            "manifest": manifest.model_dump(mode="json"),
        }
    )


def destination_digest(
    provider: str,
    remote: str,
    repo: str,
    branch: str,
    base_commit: str,
    transport: JsonObject,
) -> str:
    return object_digest(
        {
            "provider": provider,
            "remote": remote,
            "repo": repo,
            "branch": branch,
            "base_commit": base_commit,
            "transport": transport,
        }
    )


def operation_id(attempt_id: str, output: str) -> str:
    digest = object_digest({"attempt_id": attempt_id, "output": output})
    return OPERATION_PREFIX + digest.removeprefix(DIGEST_PREFIX)[:OPERATION_HEX]
