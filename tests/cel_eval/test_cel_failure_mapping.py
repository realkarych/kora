import asyncio

import pytest

from kora.cel_eval.binding import Binding, NativeProgram
from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError, Fault
from kora.cel_eval.evaluate import Machine, ResultType
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
from tests.cel_eval.thread_scenarios import Panic


class FailureProgram:
    def __init__(self, failure: type[BaseException]) -> None:
        self.failure = failure

    def execute(self, context: dict[str, object]) -> object:
        try:
            raise ValueError("private-native-cause")
        except ValueError as cause:
            raise self.failure("private-native-message") from cause


class FailureBinding:
    def __init__(self, failure: type[BaseException]) -> None:
        self.failure = failure

    def compile(self, expression: str) -> NativeProgram:
        return FailureProgram(self.failure)

    def binding(self) -> Binding:
        return Binding("fake", Panic, self.compile)


@pytest.mark.parametrize(
    ("failure", "kind", "fault"),
    [
        (KeyError, ErrorKind.MISSING_FIELD, None),
        (TypeError, ErrorKind.NO_OVERLOAD, None),
        (IndexError, ErrorKind.INDEX_OUT_OF_RANGE, None),
        (ZeroDivisionError, ErrorKind.DIVISION_BY_ZERO, None),
        (OverflowError, ErrorKind.OVERFLOW, None),
        (ValueError, ErrorKind.INTERNAL_FAULT, Fault.VALUE_ERROR),
        (RuntimeError, ErrorKind.INVALID_ARGUMENT, None),
        (Panic, ErrorKind.INTERNAL_FAULT, Fault.PANIC),
    ],
)
def test_cel_maps_only_documented_native_errors(
    failure: type[BaseException], kind: ErrorKind, fault: Fault | None
) -> None:
    machine = Machine(FailureBinding(failure).binding(), clock)

    result = machine.evaluate(
        expression("true"), {}, {}, ErrorContext("when"), ResultType.BOOL
    )

    assert isinstance(result, EvaluationError)
    assert result.kind is kind
    assert result.fault is fault
    assert "private-native-message" not in repr(result)
    assert "private-native-cause" not in repr(result)


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
def test_cel_cancellation_propagates_through_evaluation(
    failure: type[BaseException],
) -> None:
    machine = Machine(FailureBinding(failure).binding(), clock)
    checked = expression("true")

    with pytest.raises(failure):
        machine.evaluate(checked, {}, {}, ErrorContext("when"), ResultType.BOOL)


class CompileFailureBinding:
    def __init__(self, failure: type[BaseException]) -> None:
        self.failure = failure

    def compile(self, expression: str) -> NativeProgram:
        raise self.failure("private-native-message")

    def binding(self) -> Binding:
        return Binding("fake", Panic, self.compile)


@pytest.mark.parametrize(
    ("binding", "kind", "fault"),
    [
        (FailureBinding(RuntimeError).binding(), ErrorKind.INVALID_ARGUMENT, None),
        (
            CompileFailureBinding(RuntimeError).binding(),
            ErrorKind.INTERNAL_FAULT,
            Fault.RUNTIME_ERROR,
        ),
        (
            CompileFailureBinding(ValueError).binding(),
            ErrorKind.INTERNAL_FAULT,
            Fault.VALUE_ERROR,
        ),
        (
            FailureBinding(ValueError).binding(),
            ErrorKind.INTERNAL_FAULT,
            Fault.VALUE_ERROR,
        ),
    ],
    ids=["execution_runtime", "compile_runtime", "compile_value", "execution_value"],
)
def test_cel_invalid_argument_is_not_an_internal_fault(
    binding: Binding, kind: ErrorKind, fault: Fault | None
) -> None:
    machine = Machine(binding, clock)

    result = machine.evaluate(
        expression("true"), {}, {}, ErrorContext("when"), ResultType.BOOL
    )

    assert isinstance(result, EvaluationError)
    assert result.kind is kind
    assert result.fault is fault
    assert "private-native-message" not in repr(result)
