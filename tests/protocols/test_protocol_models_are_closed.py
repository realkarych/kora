import pytest
from pydantic import ValidationError

from kora.protocols.common import BoundaryModel
from kora.protocols.harness import SessionFile, SessionResult, SessionSpec
from kora.protocols.identity import MAX_COUNT, MAX_PID, GrantBinding, ProcessIdentity
from kora.protocols.vcs import WorkspaceRef, WorkspaceTarget
from tests.protocols.harness_documents import (
    SESSION_FILE,
    SESSION_SPEC,
    WORKSPACE_REF,
    WORKSPACE_TARGET,
    session_result,
)

IDENTITY: dict[str, object] = {
    "host_id": "host-1",
    "boot_id": "boot-1",
    "pid_namespace": "pidns-1",
    "pid": 4242,
    "start_time": 98765,
    "leads_group": True,
}
BINDING: dict[str, object] = {
    "dispatcher_id": "dispatcher-1",
    "worker_id": "worker-1",
    "incarnation_id": "incarnation-1",
    "lease_epoch": 3,
    "execution_domain_id": "domain-1",
    "process": IDENTITY,
}
DOCUMENTS: dict[type[BoundaryModel], dict[str, object]] = {
    ProcessIdentity: IDENTITY,
    GrantBinding: BINDING,
    WorkspaceRef: WORKSPACE_REF,
    WorkspaceTarget: WORKSPACE_TARGET,
    SessionFile: SESSION_FILE,
    SessionSpec: SESSION_SPEC,
    SessionResult: session_result(),
}
REFUSED_IDENTITIES: list[dict[str, object]] = [
    {**IDENTITY, "pid": 0},
    {**IDENTITY, "pid": "4242"},
    {**IDENTITY, "pid": MAX_PID + 1},
    {**IDENTITY, "start_time": -1},
    {**IDENTITY, "start_time": MAX_COUNT + 1},
    {**IDENTITY, "host_id": "h" * 257},
    {**IDENTITY, "host_id": "has space"},
    {**IDENTITY, "leads_group": 1},
    {**IDENTITY, "unknown": 1},
]
REFUSED_BINDINGS: list[dict[str, object]] = [
    {**BINDING, "lease_epoch": -1},
    {**BINDING, "lease_epoch": MAX_COUNT + 1},
    {**BINDING, "worker_id": "has space"},
    {**BINDING, "process": {**IDENTITY, "unknown": 1}},
    {**BINDING, "unknown": 1},
]


def test_the_boundary_base_is_closed_and_hides_input() -> None:
    config = BoundaryModel.model_config

    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"
    assert config.get("hide_input_in_errors") is True


@pytest.mark.parametrize("model", list(DOCUMENTS), ids=lambda model: model.__name__)
def test_protocol_models_are_closed(model: type[BoundaryModel]) -> None:
    document = DOCUMENTS[model]

    parsed = model.model_validate(document)
    restored = model.model_validate_json(parsed.model_dump_json())

    assert restored == parsed
    assert parsed.model_dump(mode="json") == document


@pytest.mark.parametrize("model", list(DOCUMENTS), ids=lambda model: model.__name__)
def test_protocol_models_refuse_unknown_fields(model: type[BoundaryModel]) -> None:
    document = {**DOCUMENTS[model], "unknown": 1}

    with pytest.raises(ValidationError):
        model.model_validate(document)


@pytest.mark.parametrize("document", REFUSED_IDENTITIES)
def test_process_identity_refuses_invalid_values(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        ProcessIdentity.model_validate(document)

    assert "has space" not in str(raised.value)


@pytest.mark.parametrize("document", REFUSED_BINDINGS)
def test_grant_binding_refuses_invalid_values(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        GrantBinding.model_validate(document)

    assert "has space" not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "other"),
    [("host_id", "host-2"), ("boot_id", "boot-2"), ("pid", 4243), ("start_time", 1)],
)
def test_identities_differ_when_a_key_field_differs(field: str, other: object) -> None:
    changed = {**IDENTITY, field: other}

    first = ProcessIdentity.model_validate(IDENTITY)
    second = ProcessIdentity.model_validate(changed)

    assert first != second


def test_identity_equality_ignores_group_leadership() -> None:
    leader = ProcessIdentity.model_validate(IDENTITY)
    member = ProcessIdentity.model_validate({**IDENTITY, "leads_group": False})

    assert leader == member
    assert hash(leader) == hash(member)
    assert leader.identity_key() == member.identity_key()
