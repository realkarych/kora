import sys
from collections.abc import Generator
from enum import Enum, StrEnum, auto
from typing import cast

from kora.cel_eval.errors import raise_safe
from kora.engine.parameter_arena_predicates import (
    ParameterArenaFailure,
    ParameterArenaPredicate,
    ParameterArenaRejected,
    check_parameter_arena_predicate,
)
from kora.engine.parameter_numeric_predicates import (
    ParameterNumericFailure,
    ParameterNumericPredicate,
    ParameterNumericRejected,
    check_parameter_numeric_predicate,
)
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    ArenaObjectLookup,
    array_item,
    container_count,
    cursor_kind,
    object_value,
    scalar_value,
    valid_arena_cursor,
)
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import BoundedEvaluationMeter


class ParameterSchemaEvaluationFailure(StrEnum):
    INVALID = "invalid parameter schema evaluation operand"
    CAPACITY = "parameter schema evaluation capacity exceeded"
    UNAVAILABLE = "parameter schema evaluation head is unavailable"


class ParameterSchemaEvaluationRejected(ValueError):
    def __init__(self, reason: ParameterSchemaEvaluationFailure) -> None:
        self.reason: ParameterSchemaEvaluationFailure = reason
        super().__init__(reason.value)


class _LogicalHead(Enum):
    ALL = auto()
    ANY = auto()
    ONE = auto()
    NOT = auto()
    IF = auto()
    ITEMS = auto()
    UNAVAILABLE = auto()


_Head = _LogicalHead | ParameterArenaPredicate | ParameterNumericPredicate
_Continuation = Generator[ArenaCursor, bool, bool]
_HEADS: tuple[tuple[str, _Head], ...] = (
    ("$dynamicRef", _LogicalHead.UNAVAILABLE),
    ("$ref", _LogicalHead.UNAVAILABLE),
    ("additionalProperties", _LogicalHead.UNAVAILABLE),
    ("allOf", _LogicalHead.ALL),
    ("anyOf", _LogicalHead.ANY),
    ("const", ParameterArenaPredicate.CONST),
    ("contains", _LogicalHead.UNAVAILABLE),
    ("dependentRequired", ParameterArenaPredicate.DEPENDENT_REQUIRED),
    ("dependentSchemas", _LogicalHead.UNAVAILABLE),
    ("enum", ParameterArenaPredicate.ENUM),
    ("exclusiveMaximum", ParameterNumericPredicate.EXCLUSIVE_MAXIMUM),
    ("exclusiveMinimum", ParameterNumericPredicate.EXCLUSIVE_MINIMUM),
    ("if", _LogicalHead.IF),
    ("items", _LogicalHead.ITEMS),
    ("maxItems", ParameterArenaPredicate.MAX_ITEMS),
    ("maxLength", ParameterArenaPredicate.MAX_LENGTH),
    ("maxProperties", ParameterArenaPredicate.MAX_PROPERTIES),
    ("maximum", ParameterNumericPredicate.MAXIMUM),
    ("minItems", ParameterArenaPredicate.MIN_ITEMS),
    ("minLength", ParameterArenaPredicate.MIN_LENGTH),
    ("minProperties", ParameterArenaPredicate.MIN_PROPERTIES),
    ("minimum", ParameterNumericPredicate.MINIMUM),
    ("multipleOf", ParameterNumericPredicate.MULTIPLE_OF),
    ("not", _LogicalHead.NOT),
    ("oneOf", _LogicalHead.ONE),
    ("pattern", _LogicalHead.UNAVAILABLE),
    ("patternProperties", _LogicalHead.UNAVAILABLE),
    ("properties", _LogicalHead.UNAVAILABLE),
    ("propertyNames", _LogicalHead.UNAVAILABLE),
    ("required", ParameterArenaPredicate.REQUIRED),
    ("unevaluatedItems", _LogicalHead.UNAVAILABLE),
    ("unevaluatedProperties", _LogicalHead.UNAVAILABLE),
    ("uniqueItems", ParameterArenaPredicate.UNIQUE_ITEMS),
)


def _member(
    schema: ArenaCursor, position: int, meter: BoundedEvaluationMeter
) -> ArenaCursor:
    return cast("ArenaCursor", object_value(schema, None, position, meter))


class _Evaluation:
    def __init__(
        self,
        instance: ArenaCursor,
        meter: BoundedEvaluationMeter,
        max_schema_frames: int,
        max_pair_frames: int,
    ) -> None:
        self.instance: ArenaCursor = instance
        self.meter: BoundedEvaluationMeter = meter
        self.max_schema_frames: int = max_schema_frames
        self.max_pair_frames: int = max_pair_frames
        self.frames: list[_Continuation] = []

    def _push(self, schema: ArenaCursor) -> None:
        self.meter.reserve(2)
        if len(self.frames) >= self.max_schema_frames:
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.CAPACITY
            )
        self.meter.reserve(4, entries=2)
        self.frames.append(self._visit(schema))

    def run(self, schema: ArenaCursor) -> bool:
        try:
            self._push(schema)
            result: bool | None = None
            while True:
                self.meter.reserve(5, entries=1)
                frame = self.frames[-1]
                try:
                    child = next(frame) if result is None else frame.send(result)
                except StopIteration as completed:
                    verdict: object = completed.value
                    if type(verdict) is not bool:
                        raise ParameterSchemaEvaluationRejected(
                            ParameterSchemaEvaluationFailure.INVALID
                        ) from None
                    self.frames.pop()
                    if not self.frames:
                        return verdict
                    result = verdict
                else:
                    self._push(child)
                    result = None
        finally:
            while self.frames:
                self.frames.pop().close()

    def _sequence(self, head: _LogicalHead, argument: ArenaCursor) -> _Continuation:
        meter = self.meter
        kind = cursor_kind(argument, meter)
        meter.reserve()
        if kind != "array":
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        count = container_count(argument, meter)
        meter.reserve(2)
        index, found = 0, False
        while True:
            meter.reserve(2)
            if index == count:
                return head is _LogicalHead.ALL or found
            child = cast("ArenaCursor", array_item(argument, index, meter))
            matched = yield child
            meter.reserve(4)
            if head is _LogicalHead.ALL and not matched:
                return False
            if head is _LogicalHead.ANY and matched:
                return True
            if head is _LogicalHead.ONE and matched:
                if found:
                    return False
                found = True
            index += 1

    def _conditional(self, lookup: ArenaObjectLookup, position: int) -> _Continuation:
        meter = self.meter
        then = lookup.find("then", meter)
        otherwise = lookup.find("else", meter)
        meter.reserve(2)
        if then < 0 and otherwise < 0:
            return True
        matched = yield _member(lookup.cursor, position, meter)
        meter.reserve(2)
        branch = then if matched else otherwise
        if branch < 0:
            return True
        return (yield _member(lookup.cursor, branch, meter))

    def _numeric(self, head: ParameterNumericPredicate, argument: ArenaCursor) -> bool:
        meter = self.meter
        kind = cursor_kind(self.instance, meter)
        meter.reserve()
        if kind != "number":
            return True
        bound_kind = cursor_kind(argument, meter)
        meter.reserve()
        if bound_kind != "number":
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        return check_parameter_numeric_predicate(
            head,
            scalar_value(self.instance, meter),
            scalar_value(argument, meter),
            meter=meter,
        )

    def _direct(self, head: ParameterArenaPredicate, argument: ArenaCursor) -> bool:
        return check_parameter_arena_predicate(
            head,
            self.instance,
            argument,
            meter=self.meter,
            max_pair_frames=self.max_pair_frames,
        )

    def _visit(self, schema: ArenaCursor) -> _Continuation:
        meter = self.meter
        kind = cursor_kind(schema, meter)
        meter.reserve()
        if kind == "boolean":
            return scalar_value(schema, meter) is True
        meter.reserve()
        if kind != "object":
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        meter.reserve(2, entries=1)
        lookup = ArenaObjectLookup(schema)
        type_position = lookup.find("type", meter)
        meter.reserve(2)
        if type_position >= 0 and not self._direct(
            ParameterArenaPredicate.TYPE, _member(schema, type_position, meter)
        ):
            return False
        index = 0
        while True:
            meter.reserve(2)
            if index == len(_HEADS):
                return True
            keyword, head = _HEADS[index]
            index += 1
            position = lookup.find(keyword, meter)
            meter.reserve(2)
            if position < 0 and head is _LogicalHead.ITEMS:
                position = lookup.find("prefixItems", meter)
            if position < 0:
                continue
            meter.reserve(2)
            if head is _LogicalHead.UNAVAILABLE or head is _LogicalHead.ITEMS:
                raise ParameterSchemaEvaluationRejected(
                    ParameterSchemaEvaluationFailure.UNAVAILABLE
                )
            if head is _LogicalHead.IF:
                meter.reserve(entries=1)
                matched = yield from self._conditional(lookup, position)
            else:
                argument = _member(schema, position, meter)
                meter.reserve(3)
                if isinstance(head, ParameterArenaPredicate):
                    matched = self._direct(head, argument)
                elif isinstance(head, ParameterNumericPredicate):
                    matched = self._numeric(head, argument)
                elif head is _LogicalHead.NOT:
                    matched = not (yield argument)
                else:
                    meter.reserve(entries=1)
                    matched = yield from self._sequence(head, argument)
            meter.reserve()
            if not matched:
                return False


def _evaluate(
    schema: ArenaCursor,
    instance: ArenaCursor,
    meter: BoundedEvaluationMeter,
    max_schema_frames: int,
    max_pair_frames: int,
) -> bool:
    if type(meter) is not BoundedEvaluationMeter:
        raise ParameterSchemaEvaluationRejected(
            ParameterSchemaEvaluationFailure.INVALID
        )
    meter.reserve()
    if (
        type(max_schema_frames) is not int
        or not 0 <= max_schema_frames <= sys.maxsize
        or type(max_pair_frames) is not int
        or not 0 <= max_pair_frames <= sys.maxsize
        or not valid_arena_cursor(schema)
        or not valid_arena_cursor(instance)
    ):
        raise ParameterSchemaEvaluationRejected(
            ParameterSchemaEvaluationFailure.INVALID
        )
    meter.reserve(2, entries=2)
    return _Evaluation(instance, meter, max_schema_frames, max_pair_frames).run(schema)


def evaluate_parameter_schema(
    schema: ArenaCursor,
    instance: ArenaCursor,
    *,
    meter: BoundedEvaluationMeter,
    max_schema_frames: int,
    max_pair_frames: int,
) -> bool:
    try:
        return _evaluate(schema, instance, meter, max_schema_frames, max_pair_frames)
    except ParameterSchemaEvaluationRejected as rejected:
        reason = rejected.reason
    except (
        ParameterArenaRejected,
        ParameterNumericRejected,
        EvaluationRefused,
    ) as rejected:
        reason = (
            ParameterSchemaEvaluationFailure.CAPACITY
            if rejected.reason
            in (
                ParameterArenaFailure.CAPACITY,
                ParameterNumericFailure.CAPACITY,
                "evaluation_exhausted",
            )
            else ParameterSchemaEvaluationFailure.INVALID
        )
    except MemoryError:
        reason = ParameterSchemaEvaluationFailure.CAPACITY
    del schema, instance, meter, max_schema_frames, max_pair_frames
    raise_safe(ParameterSchemaEvaluationRejected(reason))
