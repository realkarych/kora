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
        (RuntimeError, ErrorKind.INTERNAL_FAULT, Fault.RUNTIME_ERROR),
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
def test_cel_panic_maps_and_preserves_cancellation(
    failure: type[BaseException],
) -> None:
    machine = Machine(FailureBinding(failure).binding(), clock)
    panic_machine = Machine(FailureBinding(Panic).binding(), clock)

    panic = panic_machine.evaluate(
        expression("true"), {}, {}, ErrorContext("when"), ResultType.BOOL
    )
    with pytest.raises(failure):
        machine.evaluate(
            expression("true"), {}, {}, ErrorContext("when"), ResultType.BOOL
        )
    assert isinstance(panic, EvaluationError)
    assert panic.kind is ErrorKind.INTERNAL_FAULT
    assert panic.fault is Fault.PANIC
