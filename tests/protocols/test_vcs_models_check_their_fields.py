import base64
import json
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir import VcsRef
from kora.ir.vcs_ref import MAX_PATH_BYTES
from kora.protocols.vcs import (
    MAX_ARGV_ITEMS,
    MAX_FILE_CONTENT_BYTES,
    MAX_PLAN_FILES,
    MAX_PLAN_STEPS,
    MAX_STDIN_BYTES,
    CheckoutPlan,
    CheckoutRequest,
    CredentialRequest,
    InspectionContent,
    InspectionManifest,
    PlanExecutor,
    PlanFile,
    PlanStep,
    PreparedPublication,
    PublishPlan,
    PublishRequest,
    VcsCapabilities,
)
from tests.protocols import runtime_documents as documents
from tests.protocols.harness_documents import DIGEST


def encoded(size: int) -> str:
    return base64.urlsafe_b64encode(b"x" * size).decode()


def files(count: int, size: int = 0) -> list[dict[str, object]]:
    return [
        documents.plan_file(path=f"helpers/file-{index}", content=encoded(size))
        for index in range(count)
    ]


REFUSED: list[tuple[type[BaseModel], dict[str, object]]] = [
    (PlanFile, documents.plan_file(path="../escape")),
    (PlanFile, documents.plan_file(path="/absolute")),
    (PlanFile, documents.plan_file(path="a//b")),
    (PlanFile, documents.plan_file(path="a/" + "b" * 256)),
    (PlanFile, documents.plan_file(path="/".join(["p" * 200] * 21))),
    (PlanFile, documents.plan_file(mode=0o1000)),
    (PlanFile, documents.plan_file(content=encoded(MAX_FILE_CONTENT_BYTES + 1))),
    (PlanStep, documents.plan_step(argv=[])),
    (PlanStep, documents.plan_step(argv=["nul\x00"])),
    (PlanStep, documents.plan_step(argv=["a"] * (MAX_ARGV_ITEMS + 1))),
    (PlanStep, documents.plan_step(argv=["a" * 4097])),
    (PlanStep, documents.plan_step(argv=["a" * 4096] * 17)),
    (PlanStep, documents.plan_step(capture="stdout")),
    (PlanStep, documents.plan_step(timeout="PT0S")),
    (PlanStep, documents.plan_step(timeout="PT1H0.000001S")),
    (PlanStep, documents.plan_step(env={"lower": "v"})),
    (PlanStep, documents.plan_step(failure_class="timeout")),
    (PlanStep, documents.plan_step(failure_class="")),
    (PlanStep, documents.plan_step(stdin=encoded(MAX_STDIN_BYTES + 1))),
    (CheckoutPlan, documents.checkout_plan(files=files(MAX_PLAN_FILES + 1))),
    (
        CheckoutPlan,
        documents.checkout_plan(steps=[documents.plan_step()] * (MAX_PLAN_STEPS + 1)),
    ),
    (CheckoutPlan, documents.checkout_plan(files=[documents.plan_file()] * 2)),
    (
        CheckoutPlan,
        documents.checkout_plan(files=files(3, MAX_FILE_CONTENT_BYTES)),
    ),
    (CheckoutPlan, documents.checkout_plan(required_tools=["has space"])),
    (
        CheckoutPlan,
        documents.checkout_plan(
            files=files(1, MAX_FILE_CONTENT_BYTES),
            steps=[
                documents.plan_step(
                    env={f"K{index}": "v" * 4096 for index in range(64)}
                )
            ],
        ),
    ),
    (
        CheckoutPlan,
        documents.checkout_plan(
            files=[],
            steps=[documents.plan_step(argv=["a" * 4096] * 16)] * 9,
        ),
    ),
    *(
        (
            PublishPlan,
            documents.publish_plan(**{phase: [documents.plan_step(cwd="candidate")]}),
        )
        for phase in ("prepare", "inspect", "publish")
    ),
    (
        PublishPlan,
        documents.publish_plan(export=[documents.plan_step(cwd="candidate")] * 62),
    ),
    (
        PublishPlan,
        documents.publish_plan(export=[documents.plan_step(cwd="control")]),
    ),
    (
        InspectionContent,
        {
            "revision": "c0ffee",
            "path": None,
            "kind": "file",
            "content_location": "blob",
            "content_digest": DIGEST,
            "size_bytes": 1,
        },
    ),
    (
        InspectionContent,
        {
            "revision": "c0ffee",
            "path": "src/app.py",
            "kind": "revision_metadata",
            "content_location": "blob",
            "content_digest": DIGEST,
            "size_bytes": 1,
        },
    ),
    (
        InspectionManifest,
        {
            "prepared_digest": DIGEST,
            "revision": "c0ffee",
            "content_index_location": "index",
            "content_index_digest": DIGEST,
            "complete": 0,
        },
    ),
    (PreparedPublication, documents.prepared_publication(repo="r" * 4097)),
    (PreparedPublication, documents.prepared_publication(branch="b" * 1025)),
    (PreparedPublication, documents.prepared_publication(commit="c" * 256)),
    (PreparedPublication, documents.prepared_publication(diff_stat="d" * 4097)),
    (PreparedPublication, documents.prepared_publication(diff_stat="nul\x00")),
    (
        PreparedPublication,
        documents.prepared_publication(changed_paths=["a.py", "a.py"]),
    ),
    (
        PreparedPublication,
        documents.prepared_publication(changed_paths=["a/../b"]),
    ),
    (
        PreparedPublication,
        documents.prepared_publication(prepared_digest="0" * 64),
    ),
    (CredentialRequest, documents.credential_request(purpose="publish")),
    (CredentialRequest, documents.credential_request(attempt_id=None)),
    (
        CredentialRequest,
        documents.credential_request(prepared_digest=DIGEST),
    ),
    (
        CredentialRequest,
        documents.credential_request(purpose="api", operation_id="op-1"),
    ),
    (CredentialRequest, documents.credential_request(repo="line\nbreak")),
]


@pytest.mark.parametrize(("model", "document"), REFUSED)
def test_vcs_models_refuse_invalid_fields(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(document))


def test_vcs_models_accept_their_bounds() -> None:
    bounded_plan = documents.checkout_plan(
        files=files(1, MAX_FILE_CONTENT_BYTES),
        steps=[documents.plan_step(timeout="PT1H", argv=["a" * 4096] * 16)],
    )
    requests = [
        documents.credential_request(
            purpose="publish",
            operation_id="op-1",
            prepared_digest=DIGEST,
            destination_digest=DIGEST,
        ),
        documents.credential_request(purpose="api", attempt_id=None),
    ]

    plan = CheckoutPlan.model_validate_json(json.dumps(bounded_plan))
    parsed = [CredentialRequest.model_validate(request) for request in requests]

    assert plan.steps[0].timeout == timedelta(hours=1)
    assert [request.purpose for request in parsed] == ["publish", "api"]


def test_plan_step_names_an_optional_failure_class() -> None:
    omitted = {
        name: value
        for name, value in documents.plan_step().items()
        if name != "failure_class"
    }

    step = PlanStep.model_validate_json(json.dumps(omitted))

    assert step.failure_class is None


def widest_prepared(padding: str) -> dict[str, object]:
    width = 255
    count = MAX_PATH_BYTES // width
    paths = [f"{index:08d}".ljust(width, padding) for index in range(count)]
    return documents.prepared_publication(
        repo="/" + "r" * 4095,
        branch="b" * 1024,
        commit="c" * 255,
        base_commit="d" * 255,
        changed_paths=paths,
        diff_stat="\\" * 4096,
    )


def test_prepared_publication_at_its_bounds_yields_a_valid_vcs_ref() -> None:
    document = widest_prepared("p")

    prepared = PreparedPublication.model_validate(document)
    reference = VcsRef.model_validate(
        {
            "repo": prepared.repo,
            "branch": prepared.branch,
            "commit": prepared.commit,
            "base_commit": prepared.base_commit,
            "changed_paths": list(prepared.changed_paths),
            "diff_stat": prepared.diff_stat,
            "pushed_by": {
                "provider_instance": "\U0001f600" * 255,
                "stable_id": "\U0001f600" * 255,
                "kind": "local_service",
                "display_name": "\U0001f600" * 255,
            },
        }
    )

    assert reference.changed_paths == prepared.changed_paths


def test_prepared_publication_refuses_what_a_vcs_ref_refuses() -> None:
    paths = [f"{index:08d}".ljust(255, "p") for index in range(2057)]

    over_sum = documents.prepared_publication(changed_paths=paths)
    escaped = widest_prepared('"')

    with pytest.raises(ValidationError):
        PreparedPublication.model_validate(over_sum)
    with pytest.raises(ValidationError):
        PreparedPublication.model_validate(escaped)


CHECKOUT: CheckoutRequest = CheckoutRequest(
    repo="/srv/git/repo.git",
    rev="main",
    root="/workspace/attempt",
    attempt_id=documents.ATTEMPT,
    branch="main",
)
PUBLISH: PublishRequest = PublishRequest(
    repo="/srv/git/repo.git",
    root="/workspace/attempt",
    attempt_id=documents.ATTEMPT,
    branch="main",
    commit_message="kora: fix #1",
)
REQUEST_CHANGES: list[tuple[CheckoutRequest | PublishRequest, str, object]] = [
    *(
        (request, field, value)
        for request in (CHECKOUT, PUBLISH)
        for field, value in (
            ("repo", ""),
            ("repo", "r" * 4097),
            ("root", "line\nbreak"),
            ("attempt_id", "has space"),
            ("branch", "b" * 1025),
        )
    ),
    (CHECKOUT, "rev", "r" * 1025),
    (CHECKOUT, "rev", 7),
    (PUBLISH, "commit_message", "nul\x00"),
    (PUBLISH, "commit_message", ""),
]


@pytest.mark.parametrize(("request_value", "field", "value"), REQUEST_CHANGES)
def test_requests_check_their_fields(
    request_value: CheckoutRequest | PublishRequest, field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        replace(request_value, **{field: value})


def test_requests_accept_a_vcs_ref_revision() -> None:
    reference = VcsRef.model_validate(documents.VCS_REF)

    checkout = replace(CHECKOUT, rev=reference)

    assert checkout.rev == reference


EXECUTOR_SETS: list[object] = [frozenset(), frozenset({"candidate"}), {"runner"}]


@pytest.mark.parametrize("executors", EXECUTOR_SETS)
def test_vcs_capabilities_need_an_executor(executors: object) -> None:
    flags = (False, False, False, False, False)

    with pytest.raises(ValueError, match="plan_executors"):
        VcsCapabilities(*flags, cast("frozenset[PlanExecutor]", executors))
