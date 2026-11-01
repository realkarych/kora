from collections.abc import Callable, Generator
from contextlib import closing
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from pydantic import MISSING
from referencing import Registry

from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_locations import (
    LISTED_SCHEMAS,
    NAMED_SCHEMAS,
    SINGLE_SCHEMAS,
    Location,
)

STRING_KEYWORDS: frozenset[str] = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$dynamicRef",
        "$anchor",
        "$dynamicAnchor",
        "$recursiveRef",
        "$recursiveAnchor",
        "$comment",
        "title",
        "description",
        "format",
        "pattern",
        "contentEncoding",
        "contentMediaType",
    }
)
NUMBER_KEYWORDS: frozenset[str] = frozenset(
    {
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "maxItems",
        "minItems",
        "maxContains",
        "minContains",
        "maxProperties",
        "minProperties",
    }
)
BOOLEAN_KEYWORDS: frozenset[str] = frozenset(
    {"uniqueItems", "readOnly", "writeOnly", "deprecated"}
)
BATCHED_MAP_KEYWORDS: frozenset[str] = frozenset(
    {
        "$defs",
        "definitions",
        "properties",
        "dependentSchemas",
        "dependencies",
        "dependentRequired",
        "$vocabulary",
    }
)
SIMPLE_TYPES: frozenset[str] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
Fault = Callable[[Location], None]


class SafeText(str):
    def __repr__(self) -> str:
        return "'<schema text>'"


class SafeInteger(int):
    def __repr__(self) -> str:
        return "<schema integer>"

    def __str__(self) -> str:
        return "<schema integer>"


class SafeArray(list[JsonValue]):
    def __repr__(self) -> str:
        return "<schema array>"


class SafeObject(dict[str, JsonValue]):
    def __repr__(self) -> str:
        return "<schema object>"


def string_array(
    value: JsonValue, location: Location, fault: Fault
) -> SafeArray | MISSING:
    if not isinstance(value, list):
        fault(location)
        return MISSING
    result = SafeArray()
    seen: set[str] = set()
    valid = True
    for index, item in enumerate(value):
        if isinstance(item, str) and item not in seen:
            result.append(SafeText(item))
            seen.add(item)
        else:
            fault(location.child(index))
            valid = False
    return result if valid else MISSING


def type_value(
    value: JsonValue, location: Location, fault: Fault
) -> JsonValue | MISSING:
    if isinstance(value, str) and value in SIMPLE_TYPES:
        return SafeText(value)
    if not isinstance(value, list) or not value:
        fault(location)
        return MISSING
    seen: set[str] = set()
    valid = True
    for index, item in enumerate(value):
        if not isinstance(item, str) or item not in SIMPLE_TYPES or item in seen:
            fault(location.child(index))
            valid = False
        else:
            seen.add(item)
    return SafeArray(sorted(seen)) if valid else MISSING


def schema_value(value: JsonValue, location: Location, fault: Fault) -> bool:
    if not isinstance(value, dict | bool):
        fault(location)
    return True


def map_value(
    keyword: str, value: JsonValue, location: Location, fault: Fault
) -> SafeObject | MISSING:
    if not isinstance(value, dict):
        fault(location)
        return MISSING
    result = SafeObject()
    for name, item in value.items():
        field = location.child("<field>")
        if keyword == "$vocabulary":
            if not isinstance(item, bool):
                fault(field)
            prepared: JsonValue = True
        elif keyword == "dependentRequired" or (
            keyword == "dependencies" and isinstance(item, list)
        ):
            strings = string_array(item, field, fault)
            prepared = SafeArray() if strings is MISSING else strings
        else:
            prepared = schema_value(item, field, fault)
        result[SafeText(name)] = prepared
    return result


def keyword_value(
    keyword: str, value: JsonValue, location: Location, fault: Fault
) -> JsonValue | MISSING:
    if keyword == "type":
        return type_value(value, location, fault)
    if keyword == "required":
        return string_array(value, location, fault)
    if keyword in NAMED_SCHEMAS | {"dependencies", "dependentRequired", "$vocabulary"}:
        return map_value(keyword, value, location, fault)
    if keyword in SINGLE_SCHEMAS:
        return schema_value(value, location, fault)
    if keyword in LISTED_SCHEMAS:
        if isinstance(value, list):
            return SafeArray(
                schema_value(item, location.child(index), fault)
                for index, item in enumerate(value)
            )
    elif keyword in STRING_KEYWORDS:
        if isinstance(value, str):
            return SafeText(value)
    elif keyword in NUMBER_KEYWORDS:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return SafeInteger(value) if isinstance(value, int) else value
    elif keyword in BOOLEAN_KEYWORDS:
        if isinstance(value, bool):
            return value
    elif keyword in {"enum", "examples"}:
        if isinstance(value, list):
            return SafeArray()
    else:
        return MISSING
    fault(location)
    return MISSING


class KeywordValidation:
    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.validator: Validator = Draft202012Validator(
            Draft202012Validator.META_SCHEMA,
            registry=Registry(),
            format_checker=FormatChecker(formats=("regex",)),
        )

    def check(self, schema: JsonObject, location: Location) -> None:
        for keyword, value in schema.items():
            field = location.child(keyword)
            prepared = keyword_value(keyword, value, field, self.fault)
            if prepared is MISSING:
                continue
            if isinstance(prepared, SafeObject) and keyword not in BATCHED_MAP_KEYWORDS:
                for name, item in prepared.items():
                    self.check_value(keyword, SafeObject({name: item}), field)
            else:
                self.check_value(keyword, prepared, field)

    def check_value(self, keyword: str, value: JsonValue, field: Location) -> None:
        errors = cast(
            "Generator[ValidationError, None, None]",
            self.validator.iter_errors(SafeObject({keyword: value})),
        )
        try:
            with closing(errors):
                for _ in errors:
                    self.fault(field)
        except (RecursionError, OverflowError, ValueError):
            if keyword not in {"pattern", "patternProperties"}:
                raise
            self.fault(field)
