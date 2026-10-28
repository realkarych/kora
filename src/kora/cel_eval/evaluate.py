from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from kora.cel import CheckedExpression
from kora.cel.diagnostics import DiagnosticError
from kora.cel.parser import RENDERING_LIMITS, parse
from kora.cel.types import CelType
from kora.cel_eval.binding import Binding, Program
from kora.cel_eval.cache import (
    DEFAULT_CACHE_CHARACTERS,
    DEFAULT_CACHE_ENTRIES,
    ProgramCache,
)
from kora.cel_eval.convert import BoundValues, bind
from kora.cel_eval.errors import Clock, ErrorContext, ErrorKind, EvaluationError, Fault


class ResultType(StrEnum):
    BOOL = "bool"
    STRING = "string"


@dataclass(frozen=True)
class Compilation:
    valid: bool
    fault: Fault | None = None


@dataclass(frozen=True)
class Prepared:
    checked: CheckedExpression
    values: BoundValues
    context: ErrorContext
    result_type: ResultType


Result = bool | str | EvaluationError


def canonical(checked: CheckedExpression) -> bool:
    try:
        tree = parse(checked.rendering, limits=RENDERING_LIMITS)
    except DiagnosticError:
        return False
    return tree == checked.tree


def compile_program(binding: Binding, checked: CheckedExpression) -> Program | Fault:
    if not canonical(checked):
        return Fault.UNMAPPED
    try:
        return binding.compile(checked)
    except ValueError:
        return Fault.VALUE_ERROR
    except RuntimeError:
        return Fault.RUNTIME_ERROR
    except binding.panic:
        return Fault.PANIC


@dataclass(frozen=True)
class NativeFailure:
    kind: ErrorKind
    fault: Fault | None = None


def execute(binding: Binding, program: Program, values: BoundValues) -> object:
    try:
        return binding.execute(program, values.values)
    except KeyError:
        return NativeFailure(ErrorKind.MISSING_FIELD)
    except TypeError:
        return NativeFailure(ErrorKind.NO_OVERLOAD)
    except IndexError:
        return NativeFailure(ErrorKind.INDEX_OUT_OF_RANGE)
    except ZeroDivisionError:
        return NativeFailure(ErrorKind.DIVISION_BY_ZERO)
    except OverflowError:
        return NativeFailure(ErrorKind.OVERFLOW)
    except ValueError:
        return NativeFailure(ErrorKind.INTERNAL_FAULT, Fault.VALUE_ERROR)
    except RuntimeError:
        return NativeFailure(ErrorKind.INTERNAL_FAULT, Fault.RUNTIME_ERROR)
    except binding.panic:
        return NativeFailure(ErrorKind.INTERNAL_FAULT, Fault.PANIC)


class Machine:
    def __init__(
        self,
        binding: Binding,
        clock: Clock,
        cache_entries: int = DEFAULT_CACHE_ENTRIES,
        cache_characters: int = DEFAULT_CACHE_CHARACTERS,
    ) -> None:
        self.binding = binding
        self.clock = clock
        self.cache = ProgramCache(cache_entries, cache_characters)

    def validate(self, checked: CheckedExpression) -> Compilation:
        compiled = compile_program(self.binding, checked)
        return (
            Compilation(False, compiled)
            if isinstance(compiled, Fault)
            else Compilation(True)
        )

    def evaluate(
        self,
        checked: CheckedExpression,
        values: Mapping[str, object],
        types: Mapping[str, CelType],
        context: ErrorContext,
        result_type: ResultType,
    ) -> Result:
        bound = bind(checked, values, types)
        if isinstance(bound, ErrorKind):
            return context.record(bound, self.clock)
        return self.run(Prepared(checked, bound, context, result_type))

    def run(self, request: Prepared, clock: Clock | None = None) -> Result:
        used_clock = self.clock if clock is None else clock
        checked, context = request.checked, request.context
        program = self.cache.find(checked, context.field)
        if program is None:
            program = compile_program(self.binding, checked)
            if isinstance(program, Program):
                self.cache.put(checked, context.field, program)
        if isinstance(program, Fault):
            return context.record(ErrorKind.INTERNAL_FAULT, used_clock, program)
        result = execute(self.binding, program, request.values)
        if isinstance(result, NativeFailure):
            return context.record(result.kind, used_clock, result.fault)
        if request.result_type is ResultType.BOOL and type(result) is bool:
            return result
        if request.result_type is ResultType.STRING and type(result) is str:
            return result
        return context.record(ErrorKind.WRONG_RESULT_TYPE, used_clock)
