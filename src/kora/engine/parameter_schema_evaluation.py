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
from kora.ir.structured_schema_numbers import decimal_number


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
    CONTAINS = auto()
    UNAVAILABLE = auto()


_Head = _LogicalHead | ParameterArenaPredicate | ParameterNumericPredicate
_Continuation = Generator[tuple[ArenaCursor, ArenaCursor], bool, bool]
_HEADS: tuple[tuple[str, _Head], ...] = (
    ("$dynamicRef", _LogicalHead.UNAVAILABLE),
    ("$ref", _LogicalHead.UNAVAILABLE),
    ("additionalProperties", _LogicalHead.UNAVAILABLE),
    ("allOf", _LogicalHead.ALL),
    ("anyOf", _LogicalHead.ANY),
    ("const", ParameterArenaPredicate.CONST),
    ("contains", _LogicalHead.CONTAINS),
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


def _item(
    array: ArenaCursor, position: int, meter: BoundedEvaluationMeter
) -> ArenaCursor:
    return cast("ArenaCursor", array_item(array, position, meter))


class _Evaluation:
    def __init__(
        self,
        meter: BoundedEvaluationMeter,
        max_schema_frames: int,
        max_pair_frames: int,
    ) -> None:
        self.meter: BoundedEvaluationMeter = meter
        self.max_schema_frames: int = max_schema_frames
        self.max_pair_frames: int = max_pair_frames
        self.frames: list[_Continuation] = []

    def _push(self, schema: ArenaCursor, instance: ArenaCursor) -> None:
        self.meter.reserve(2)
        if len(self.frames) >= self.max_schema_frames:
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.CAPACITY
            )
        self.meter.reserve(4, entries=2)
        self.frames.append(self._visit(schema, instance))

    def run(self, schema: ArenaCursor, instance: ArenaCursor) -> bool:
        try:
            self._push(schema, instance)
            result: bool | None = None
            while True:
                self.meter.reserve(5, entries=1)
                frame = self.frames[-1]
                try:
                    child_schema, child_instance = (
                        next(frame) if result is None else frame.send(result)
                    )
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
                    self._push(child_schema, child_instance)
                    del child_schema, child_instance
                    result = None
        finally:
            while self.frames:
                self.frames.pop().close()

    def _sequence(
        self, head: _LogicalHead, argument: ArenaCursor, instance: ArenaCursor
    ) -> _Continuation:
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
            matched = yield _item(argument, index, meter), instance
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

    def _conditional(
        self, lookup: ArenaObjectLookup, position: int, instance: ArenaCursor
    ) -> _Continuation:
        meter = self.meter
        then = lookup.find("then", meter)
        otherwise = lookup.find("else", meter)
        meter.reserve(2)
        if then < 0 and otherwise < 0:
            return True
        matched = yield _member(lookup.cursor, position, meter), instance
        meter.reserve(2)
        branch = then if matched else otherwise
        if branch < 0:
            return True
        return (yield _member(lookup.cursor, branch, meter), instance)

    def _items(
        self,
        lookup: ArenaObjectLookup,
        instance: ArenaCursor,
        prefix_position: int,
        tail_position: int,
    ) -> _Continuation:
        meter = self.meter
        kind = cursor_kind(instance, meter)
        meter.reserve()
        if kind != "array":
            return True
        count = container_count(instance, meter)
        meter.reserve(2)
        if not count:
            return True
        if prefix_position < 0:
            prefix_position = lookup.find("prefixItems", meter)
        meter.reserve(2)
        index = 0
        if prefix_position >= 0:
            prefix = _member(lookup.cursor, prefix_position, meter)
            prefix_kind = cursor_kind(prefix, meter)
            meter.reserve()
            if prefix_kind != "array":
                raise ParameterSchemaEvaluationRejected(
                    ParameterSchemaEvaluationFailure.INVALID
                )
            prefix_count = container_count(prefix, meter)
            meter.reserve()
            stop = min(count, prefix_count)
            while True:
                meter.reserve()
                if index == stop:
                    break
                matched = yield (
                    _item(prefix, index, meter),
                    _item(instance, index, meter),
                )
                meter.reserve(2)
                if not matched:
                    return False
                index += 1
        meter.reserve(2)
        if tail_position < 0 or index == count:
            return True
        tail = _member(lookup.cursor, tail_position, meter)
        while True:
            meter.reserve()
            if index == count:
                return True
            matched = yield tail, _item(instance, index, meter)
            meter.reserve(2)
            if not matched:
                return False
            index += 1

    def _contains_bound(self, lookup: ArenaObjectLookup, name: str) -> int | None:
        meter = self.meter
        position = lookup.find(name, meter)
        meter.reserve()
        if position < 0:
            return None
        argument = _member(lookup.cursor, position, meter)
        kind = cursor_kind(argument, meter)
        meter.reserve()
        if kind != "number":
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        number = decimal_number(scalar_value(argument, meter), meter)
        if meter.integer_negative(number.coefficient):
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        meter.reserve(2)
        if number.exponent == 0:
            return number.coefficient
        if number.exponent > 0:
            return meter.integer_scale(number.coefficient, number.exponent)
        meter.reserve(2, entries=1)
        divisor = meter.integer_scale(1, -number.exponent)
        quotient, remainder = meter.integer_divmod(number.coefficient, divisor)
        if not meter.integer_zero(remainder):
            raise ParameterSchemaEvaluationRejected(
                ParameterSchemaEvaluationFailure.INVALID
            )
        return quotient

    def _contains(
        self, lookup: ArenaObjectLookup, position: int, instance: ArenaCursor
    ) -> _Continuation:
        meter = self.meter
        kind = cursor_kind(instance, meter)
        meter.reserve()
        if kind != "array":
            return True
        count = container_count(instance, meter)
        declared_minimum = self._contains_bound(lookup, "minContains")
        maximum = self._contains_bound(lookup, "maxContains")
        meter.reserve(2)
        minimum = 1 if declared_minimum is None else declared_minimum
        if maximum is not None and meter.integer_compare(minimum, maximum) > 0:
            return False
        meter.reserve(3)
        index = 0
        matches = 0
        child: ArenaCursor | None = None
        while True:
            meter.reserve(4)
            if maximum is not None and meter.integer_compare(matches, maximum) > 0:
                return False
            remaining = meter.integer_subtract(count, index)
            possible = meter.integer_add(matches, remaining)
            if meter.integer_compare(possible, minimum) < 0:
                return False
            if meter.integer_compare(matches, minimum) >= 0 and (
                maximum is None or meter.integer_compare(possible, maximum) <= 0
            ):
                return True
            meter.reserve()
            if child is None:
                child = _member(lookup.cursor, position, meter)
            matched = yield child, _item(instance, index, meter)
            meter.reserve(3)
            if matched:
                matches += 1
            index += 1

    def _numeric(
        self,
        head: ParameterNumericPredicate,
        argument: ArenaCursor,
        instance: ArenaCursor,
    ) -> bool:
        meter = self.meter
        kind = cursor_kind(instance, meter)
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
            scalar_value(instance, meter),
            scalar_value(argument, meter),
            meter=meter,
        )

    def _direct(
        self,
        head: ParameterArenaPredicate,
        argument: ArenaCursor,
        instance: ArenaCursor,
    ) -> bool:
        return check_parameter_arena_predicate(
            head,
            instance,
            argument,
            meter=self.meter,
            max_pair_frames=self.max_pair_frames,
        )

    def _visit(self, schema: ArenaCursor, instance: ArenaCursor) -> _Continuation:
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
            ParameterArenaPredicate.TYPE,
            _member(schema, type_position, meter),
            instance,
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
            if head is _LogicalHead.ITEMS:
                prefix_position = (
                    lookup.find("prefixItems", meter) if position < 0 else -1
                )
                if position < 0 and prefix_position < 0:
                    continue
                meter.reserve(2, entries=1)
                matched = yield from self._items(
                    lookup, instance, prefix_position, position
                )
                meter.reserve()
                if not matched:
                    return False
                continue
            if position < 0:
                continue
            meter.reserve(2)
            if head is _LogicalHead.UNAVAILABLE:
                raise ParameterSchemaEvaluationRejected(
                    ParameterSchemaEvaluationFailure.UNAVAILABLE
                )
            if head is _LogicalHead.IF:
                meter.reserve(entries=1)
                matched = yield from self._conditional(lookup, position, instance)
            elif head is _LogicalHead.CONTAINS:
                meter.reserve(entries=1)
                matched = yield from self._contains(lookup, position, instance)
            else:
                argument = _member(schema, position, meter)
                meter.reserve(3)
                if isinstance(head, ParameterArenaPredicate):
                    matched = self._direct(head, argument, instance)
                elif isinstance(head, ParameterNumericPredicate):
                    matched = self._numeric(head, argument, instance)
                elif head is _LogicalHead.NOT:
                    matched = not (yield argument, instance)
                else:
                    meter.reserve(entries=1)
                    matched = yield from self._sequence(head, argument, instance)
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
    return _Evaluation(meter, max_schema_frames, max_pair_frames).run(schema, instance)


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
