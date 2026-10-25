import ast
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

import kora.conformance.secrets
import kora.protocols.common
import kora.protocols.evidence
import kora.protocols.history
import kora.protocols.identity
import kora.protocols.report
import kora.protocols.runtime
import kora.protocols.vcs
from kora.conformance.secrets import check_never_rendered
from kora.observability import LoggingSettings
from kora.protocols.common import BoundaryModel
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.secret_value import TRANSFER_SCOPE_KEY, SecretScope
from kora.protocols.vcs import IssuedCredential
from tests.protocols import runtime_documents as documents
from tests.protocols.conftest import StartCapture
from tests.protocols.secret_support import ESCAPED, PLAIN, Holder, Wrapper

SOURCE: Path = Path(__file__).parents[2] / "src"
TRANSFER_INVALID: str = "secret_transfer_invalid"
TRANSFER_FAILURES: tuple[tuple[str, str], ...] = (
    ('{"credential": 7}', TRANSFER_INVALID),
    ('{"credential": "short"}', TRANSFER_INVALID),
    ('{"credential": "' + "x" * 20000 + '"}', TRANSFER_INVALID),
    ('{"credential": "' + PLAIN + '"', "json_invalid"),
)
SAFE_ERROR_KEYWORDS: dict[str, frozenset[str]] = {
    "errors": frozenset({"include_input", "include_context"}),
    "json": frozenset({"include_input", "include_context"}),
}


def failure_forms(error: ValidationError) -> str:
    return " ".join(
        (
            str(error),
            repr(error),
            error.json(include_input=False),
            json.dumps(error.errors(include_input=False, include_context=False)),
        )
    )


@pytest.mark.parametrize("value", [PLAIN, ESCAPED])
def test_secret_value_never_rendered(start_capture: StartCapture, value: str) -> None:
    capture = start_capture(LoggingSettings())

    secret = capture.scope.admit(value)

    check_never_rendered(secret, value, capture.scope)
    assert (
        Holder.model_validate_json(
            json.dumps({"credential": value}), context={"secret_scope": capture.scope}
        ).credential.reveal()
        == value
    )


def error_shapes(error: ValidationError) -> list[tuple[str, bool]]:
    return [(item["type"], "ctx" in item) for item in error.errors(include_input=False)]


@pytest.mark.parametrize(("document", "error_type"), TRANSFER_FAILURES)
@pytest.mark.parametrize("model", [Holder, Wrapper])
def test_transfer_failures_carry_no_input(
    start_capture: StartCapture,
    document: str,
    error_type: str,
    model: type[BaseModel],
) -> None:
    scope: SecretScope = start_capture(LoggingSettings()).scope
    wrapped = document if model is Holder else '{"inner": ' + document + "}"

    with pytest.raises(ValidationError) as raised:
        model.model_validate_json(wrapped, context={"secret_scope": scope})

    rendered = failure_forms(raised.value)
    assert PLAIN not in rendered
    assert "short" not in rendered
    assert "xxxxxxxxxx" not in rendered
    if error_type == TRANSFER_INVALID:
        assert error_shapes(raised.value) == [(TRANSFER_INVALID, False)]


def test_a_transfer_without_a_scope_is_invalid() -> None:
    document = json.dumps({"credential": PLAIN})

    with pytest.raises(ValidationError) as raised:
        Holder.model_validate_json(document)

    assert error_shapes(raised.value) == [(TRANSFER_INVALID, False)]
    assert PLAIN not in failure_forms(raised.value)


def test_a_transfer_that_is_not_valid_text_is_invalid(
    start_capture: StartCapture,
) -> None:
    scope = start_capture(LoggingSettings()).scope
    value = "surrogate-\ud800-" + PLAIN

    with pytest.raises(ValidationError) as raised:
        Holder.model_validate({"credential": value}, context={"secret_scope": scope})

    assert error_shapes(raised.value) == [(TRANSFER_INVALID, False)]
    assert PLAIN not in failure_forms(raised.value)
    assert scope.registered_count() == 0


def model_classes() -> list[type[BaseModel]]:
    modules = (
        kora.protocols.common,
        kora.protocols.identity,
        kora.protocols.evidence,
        kora.protocols.report,
        kora.protocols.history,
        kora.protocols.vcs,
        kora.protocols.runtime,
        kora.conformance.secrets,
    )
    return [
        item
        for module in modules
        for item in vars(module).values()
        if isinstance(item, type)
        and issubclass(item, BaseModel)
        and item.__module__ == module.__name__
        and item is not BoundaryModel
    ]


def test_every_boundary_model_derives_from_the_base() -> None:
    classes = model_classes()

    assert classes
    assert all(issubclass(item, BoundaryModel) for item in classes)


def disabled_keywords(node: ast.Call) -> frozenset[str]:
    return frozenset(
        keyword.arg
        for keyword in node.keywords
        if keyword.arg is not None
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
    )


def is_unsafe_error_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    required = SAFE_ERROR_KEYWORDS.get(node.func.attr)
    return required is not None and not required <= disabled_keywords(node)


def unsafe_error_calls(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and is_unsafe_error_call(node)
    ]


def test_no_source_reads_validation_errors_with_input() -> None:
    offenders = {
        str(path.relative_to(SOURCE)): unsafe_error_calls(ast.parse(path.read_text()))
        for path in SOURCE.rglob("*.py")
    }

    assert {path: lines for path, lines in offenders.items() if lines} == {}


def required_credential(value: object) -> dict[str, object]:
    return documents.issued_credential(
        credential_mode="credential_required",
        value=value,
        principal=documents.APP_PRINCIPAL,
        rights=documents.rights(),
        expires_at=documents.AT,
        replacement="reissue",
        revocation="native",
    )


def required_authorization(credential: object) -> dict[str, object]:
    return documents.authorization(
        credential_mode="credential_required",
        principal=documents.APP_PRINCIPAL,
        credential=credential,
    )


def test_an_issued_credential_passes_into_an_authorization(
    start_capture: StartCapture,
) -> None:
    scope = start_capture(LoggingSettings()).scope
    context = {TRANSFER_SCOPE_KEY: scope}
    issued = IssuedCredential.model_validate_json(
        json.dumps(required_credential(PLAIN)), context=context
    )

    authorization = PublicationAuthorization.model_validate(
        required_authorization(issued.value), context=context
    )

    assert authorization.credential is issued.value
    assert PLAIN not in repr(authorization)
    with pytest.raises(PydanticSerializationError):
        authorization.model_dump_json()
    assert PLAIN in authorization.model_dump_json(context={"secret_transfer": True})


def test_a_passed_value_needs_its_own_unreleased_scope(
    start_capture: StartCapture,
) -> None:
    scope = start_capture(LoggingSettings()).scope
    other = start_capture(LoggingSettings()).scope
    value = scope.admit(PLAIN)
    released = other.admit(ESCAPED)
    other.release()
    attempts: list[dict[str, object]] = [
        {},
        {TRANSFER_SCOPE_KEY: other},
        {TRANSFER_SCOPE_KEY: scope},
    ]

    failures = [
        refusal_shape(required_authorization(value), context) for context in attempts
    ]
    released_failure = refusal_shape(
        required_authorization(released), {TRANSFER_SCOPE_KEY: other}
    )

    assert failures == [([(TRANSFER_INVALID, False)], False)] * 2 + [None]
    assert released_failure == ([(TRANSFER_INVALID, False)], False)


def refusal_shape(
    document: dict[str, object], context: dict[str, object]
) -> tuple[list[tuple[str, bool]], bool] | None:
    try:
        PublicationAuthorization.model_validate(document, context=context)
    except ValidationError as error:
        return error_shapes(error), PLAIN in failure_forms(error)
    return None


@pytest.mark.parametrize(
    "document",
    [document for document, error in TRANSFER_FAILURES if error == TRANSFER_INVALID],
)
def test_credential_decode_failures_carry_no_input(
    start_capture: StartCapture, document: str
) -> None:
    scope = start_capture(LoggingSettings()).scope
    credential = document.removeprefix('{"credential": ').removesuffix("}")
    issued = json.dumps(required_credential(None))[:-1]
    wrapped = issued.replace('"value": null', '"value": ' + credential) + "}"

    with pytest.raises(ValidationError) as raised:
        IssuedCredential.model_validate_json(
            wrapped, context={TRANSFER_SCOPE_KEY: scope}
        )

    assert PLAIN not in failure_forms(raised.value)
    assert "xxxxxxxxxx" not in failure_forms(raised.value)
    assert error_shapes(raised.value) == [(TRANSFER_INVALID, False)]


def test_a_coherence_refusal_hides_an_admitted_value(
    start_capture: StartCapture,
) -> None:
    scope = start_capture(LoggingSettings()).scope
    document = {**required_credential(PLAIN), "principal": documents.LOCAL_PRINCIPAL}

    with pytest.raises(ValidationError) as raised:
        IssuedCredential.model_validate_json(
            json.dumps(document), context={TRANSFER_SCOPE_KEY: scope}
        )

    assert PLAIN not in failure_forms(raised.value)


def test_a_provider_builds_a_credential_through_its_admission(
    start_capture: StartCapture,
) -> None:
    scope = start_capture(LoggingSettings()).scope
    other = start_capture(LoggingSettings()).scope
    admission = scope.admission()
    value = admission.admit(PLAIN)
    foreign = other.admission()

    issued = IssuedCredential.model_validate(
        required_credential(value), context={TRANSFER_SCOPE_KEY: admission}
    )
    refusals = [
        refused_credential(value, foreign),
        refused_credential(other.admit(ESCAPED), admission),
    ]
    scope.release()
    released = refused_credential(value, admission)

    assert issued.value is value
    assert refusals == [True, True]
    assert released is True


def refused_credential(value: object, owner: object) -> bool:
    try:
        IssuedCredential.model_validate(
            required_credential(value), context={TRANSFER_SCOPE_KEY: owner}
        )
    except ValidationError as error:
        return error_shapes(error) == [(TRANSFER_INVALID, False)]
    return False
