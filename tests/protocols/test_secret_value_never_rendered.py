import ast
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import kora.conformance.secrets
import kora.protocols.common
import kora.protocols.identity
from kora.conformance.secrets import check_never_rendered
from kora.observability import LoggingSettings
from kora.protocols.common import BoundaryModel
from kora.protocols.secret_value import SecretScope
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

    check_never_rendered(secret, value)
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
    modules = (kora.protocols.common, kora.protocols.identity, kora.conformance.secrets)
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
