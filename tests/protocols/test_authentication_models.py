from dataclasses import FrozenInstanceError, fields
from typing import cast

import pytest
from pydantic import ValidationError

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.protocols.authentication import (
    AuthenticationCapabilities,
    AuthenticationContext,
    NamespaceGrant,
    Principal,
    RequestInfo,
)
from kora.protocols.time import SystemClock

__all__ = ["kora_capture"]


def document(kind: str = "user") -> dict[str, object]:
    service = kind == "service"
    return {
        "token_id": "token-example",
        "kind": kind,
        "owner": "worker-example" if service else "person-example",
        "namespaces": [
            {"namespace_id": "namespace-example", "transcripts": not service}
        ],
        "approve": not service,
        "platform": not service,
        "runtime": "local" if service else None,
    }


@pytest.mark.parametrize("kind", ["user", "service"])
def test_principal_preserves_exact_authority_and_round_trips(kind: str) -> None:
    source = document(kind)

    principal = Principal.model_validate(source)

    assert principal.model_dump(mode="json") == source
    assert Principal.model_validate_json(principal.model_dump_json()) == principal
    assert principal.token_id == source["token_id"]
    assert principal.kind == kind
    assert principal.owner == source["owner"]
    assert principal.runtime == source["runtime"]


@pytest.mark.parametrize(
    ("approve", "platform"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_user_flags_are_independent(approve: bool, platform: bool) -> None:
    source = document()
    source.update(approve=approve, platform=platform)

    principal = Principal.model_validate(source)

    assert principal.approve is approve
    assert principal.platform is platform
    assert principal.namespaces == (
        NamespaceGrant(namespace_id="namespace-example", transcripts=True),
    )


def test_transcript_grants_remain_namespace_local() -> None:
    source = document()
    source["namespaces"] = [
        {"namespace_id": "first", "transcripts": True},
        {"namespace_id": "second", "transcripts": False},
    ]

    principal = Principal.model_validate(source)

    assert principal.namespaces == (
        NamespaceGrant(namespace_id="first", transcripts=True),
        NamespaceGrant(namespace_id="second", transcripts=False),
    )


@pytest.mark.parametrize("kind", ["user", "service"])
def test_empty_namespace_grants_add_no_authority(kind: str) -> None:
    source = document(kind)
    source["namespaces"] = []

    principal = Principal.model_validate(source)

    assert principal.namespaces == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_id", ""),
        ("owner", ""),
        ("owner", "person\nexample"),
        ("kind", "agent"),
        ("kind", "admin"),
        ("approve", 1),
        ("approve", "true"),
        ("platform", 0),
        ("runtime", "local"),
        ("namespaces", [{"namespace_id": "first", "transcripts": 1}]),
        ("namespaces", [{"namespace_id": "first", "transcripts": "false"}]),
        ("namespaces", [{"namespace_id": "first"}]),
        ("namespaces", [{"namespace_id": "", "transcripts": False}]),
        (
            "namespaces",
            [{"namespace_id": "first", "transcripts": False, "roles": list[str]()}],
        ),
        ("roles", ["admin"]),
        ("groups", list[str]()),
    ],
)
def test_principal_rejects_malformed_or_extra_authority(
    field: str, value: object
) -> None:
    source = document()
    source[field] = value

    with pytest.raises(ValidationError):
        Principal.model_validate(source)


@pytest.mark.parametrize("transcripts", [False, True])
def test_duplicate_namespace_entries_are_refused(transcripts: bool) -> None:
    source = document()
    source["namespaces"] = [
        {"namespace_id": "same", "transcripts": True},
        {"namespace_id": "same", "transcripts": transcripts},
    ]

    with pytest.raises(ValidationError, match="namespace grants must be unique"):
        Principal.model_validate(source)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approve", True),
        ("platform", True),
        ("runtime", None),
        ("namespaces", [{"namespace_id": "first", "transcripts": True}]),
    ],
)
def test_service_cannot_gain_personal_authority(field: str, value: object) -> None:
    source = document("service")
    source[field] = value

    with pytest.raises(ValidationError, match="only worker authority"):
        Principal.model_validate(source)


def test_principal_detaches_and_freezes_namespace_grants() -> None:
    grant: dict[str, object] = {"namespace_id": "first", "transcripts": False}
    grants = [grant]
    source = document()
    source["namespaces"] = grants

    principal = Principal.model_validate(source)
    grant["transcripts"] = True
    grants.clear()

    assert principal.namespaces == (
        NamespaceGrant(namespace_id="first", transcripts=False),
    )
    with pytest.raises(ValidationError, match="frozen"):
        principal.approve = False
    with pytest.raises(ValidationError, match="frozen"):
        principal.namespaces[0].transcripts = True


def test_principal_revalidates_constructed_and_copied_instances() -> None:
    valid = Principal.model_validate(document())
    copied = valid.model_copy(update={"platform": "true"})
    constructed = Principal.model_construct(
        token_id=valid.token_id,
        kind=valid.kind,
        owner=valid.owner,
        namespaces=valid.namespaces,
        approve=1,
        platform=False,
        runtime=None,
    )
    invalid_grant = NamespaceGrant.model_construct(
        namespace_id="first", transcripts="true"
    )
    nested = valid.model_copy(update={"namespaces": (invalid_grant,)})

    for invalid in (copied, constructed, nested):
        with pytest.raises(ValidationError):
            Principal.model_validate(invalid)


def test_authentication_validation_errors_hide_invalid_content() -> None:
    source = document()
    source["owner"] = "private-authentication-owner-marker\n"

    with pytest.raises(ValidationError) as raised:
        Principal.model_validate(source)

    assert "private-authentication-owner-marker" not in str(raised.value)


@pytest.mark.parametrize("authorization", [None, "Bearer private-credential-marker"])
def test_request_info_hides_credentials_and_defaults_to_ordinary_authority(
    authorization: str | None,
) -> None:
    request = RequestInfo(authorization)

    rendered = (str(request), repr(request))

    assert all("private-credential-marker" not in text for text in rendered)
    assert request.authorization == authorization
    assert request.requires_current_authority is False


def test_request_info_preserves_trusted_freshness_requirement() -> None:
    request = RequestInfo(None, requires_current_authority=True)

    assert request.requires_current_authority is True


@pytest.mark.parametrize(
    ("authorization", "freshness"),
    [(1, False), (b"private-credential-marker", False), (None, 1), (None, "true")],
)
def test_request_info_refuses_nonexact_types(
    authorization: object, freshness: object
) -> None:
    with pytest.raises(ValueError, match="must be") as raised:
        RequestInfo(cast("str | None", authorization), cast("bool", freshness))

    assert "private-credential-marker" not in str(raised.value)


def test_authentication_capabilities_are_frozen_and_consistent() -> None:
    first = AuthenticationCapabilities()

    second = AuthenticationCapabilities()

    assert first == second


@pytest.mark.parametrize(
    ("value", "attribute"),
    [
        (RequestInfo(None), "requires_current_authority"),
        (AuthenticationCapabilities(), "unexpected"),
    ],
)
def test_authentication_dataclasses_are_frozen(value: object, attribute: str) -> None:
    require_frozen_attribute(value, attribute)


def require_frozen_attribute(value: object, attribute: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, attribute, True)


def test_authentication_context_is_frozen_and_has_only_borrowed_logger_and_clock(
    kora_capture: Capture,
) -> None:
    logger = kora_capture.diagnostics.logger(CONFORMANCE_COMPONENT)
    clock = SystemClock()

    context = AuthenticationContext(logger=logger, clock=clock)

    assert tuple(field.name for field in fields(context)) == ("logger", "clock")
    assert context.logger is logger
    assert context.clock is clock
    assert not hasattr(context, "namespace_id")
    require_frozen_attribute(context, "clock")
