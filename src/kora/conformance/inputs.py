import enum
import json
import re
from dataclasses import dataclass, field
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, UnknownType, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

from kora.ir.json import MAX_CANONICAL_BYTES, JsonObject, parse_json_object
from kora.protocols.metadata import parse_config_schema


class InputReason(enum.StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    DIALECT_UNSUPPORTED = "dialect_unsupported"
    CONFIGURATION_INVALID = "configuration_invalid"
    FIXTURE_DATA_INVALID = "fixture_data_invalid"
    REFERENCE_UNRESOLVED = "reference_unresolved"
    SCHEMA_EVALUATION_UNAVAILABLE = "schema_evaluation_unavailable"
    INSTANCE_SCHEMA_INVALID = "instance_schema_invalid"
    INSTANCE_SCHEMA_MISMATCH = "instance_schema_mismatch"


class ConformanceInputError(ValueError):
    def __init__(self, reason: InputReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ValidatorSelector(Protocol):
    def __call__(
        self, schema: JsonObject, *, default: None
    ) -> type[Validator] | None: ...


@dataclass(frozen=True, init=False)
class ConformanceInputs:
    _schema: bytes = field(repr=False)
    _configuration: bytes = field(repr=False)
    _fixture_data: bytes = field(repr=False)

    def __init__(
        self,
        schema: object,
        configuration: bytes | None = None,
        fixture_data: bytes | None = None,
    ) -> None:
        schema_bytes = snapshot_schema(schema)
        if schema_bytes is None:
            raise ConformanceInputError(InputReason.SCHEMA_INVALID)
        object.__setattr__(self, "_schema", schema_bytes)
        configuration_bytes = snapshot_data(configuration)
        if configuration_bytes is None:
            raise ConformanceInputError(InputReason.CONFIGURATION_INVALID)
        fixture_bytes = snapshot_data(fixture_data)
        if fixture_bytes is None:
            raise ConformanceInputError(InputReason.FIXTURE_DATA_INVALID)
        failure = validate_configuration(schema_bytes, configuration_bytes)
        if failure is not None:
            raise ConformanceInputError(failure)
        object.__setattr__(self, "_configuration", configuration_bytes)
        object.__setattr__(self, "_fixture_data", fixture_bytes)

    @property
    def schema(self) -> JsonObject:
        return decode_snapshot(self._schema)

    @property
    def configuration(self) -> JsonObject:
        return decode_snapshot(self._configuration)

    @property
    def fixture_data(self) -> JsonObject:
        return decode_snapshot(self._fixture_data)

    def require_matching_schema(self, schema: object) -> None:
        candidate = snapshot_schema(schema)
        if candidate is None:
            raise ConformanceInputError(InputReason.INSTANCE_SCHEMA_INVALID)
        if candidate != self._schema:
            raise ConformanceInputError(InputReason.INSTANCE_SCHEMA_MISMATCH)


def canonical_object(value: JsonObject) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def snapshot_schema(value: object) -> bytes | None:
    try:
        return canonical_object(parse_config_schema(value))
    except (ValueError, RecursionError):
        return None


def snapshot_data(value: bytes | None) -> bytes | None:
    if value is None:
        return b"{}"
    if len(value) > MAX_CANONICAL_BYTES:
        return None
    try:
        parsed: object = json.loads(value)
        return canonical_object(parse_json_object(parsed))
    except (ValueError, RecursionError):
        return None


def decode_snapshot(value: bytes) -> JsonObject:
    parsed: object = json.loads(value)
    return parse_json_object(parsed)


def select_validator(schema: JsonObject) -> type[Validator] | InputReason:
    if "$schema" not in schema:
        return Draft202012Validator
    if not isinstance(schema["$schema"], str):
        return InputReason.DIALECT_UNSUPPORTED
    try:
        selected = cast("ValidatorSelector", validator_for)(schema, default=None)
    except ValueError:
        return InputReason.DIALECT_UNSUPPORTED
    if selected is None:
        return InputReason.DIALECT_UNSUPPORTED
    return selected


def validate_configuration(
    schema_bytes: bytes, configuration_bytes: bytes
) -> InputReason | None:
    schema = decode_snapshot(schema_bytes)
    selected = select_validator(schema)
    if isinstance(selected, InputReason):
        return selected
    try:
        selected.check_schema(schema)
    except (SchemaError, ValueError):
        return InputReason.SCHEMA_INVALID
    except RecursionError:
        return InputReason.SCHEMA_EVALUATION_UNAVAILABLE
    try:
        validator = selected(schema, registry=Registry())
        validator.validate(decode_snapshot(configuration_bytes))
    except ValidationError:
        return InputReason.CONFIGURATION_INVALID
    except (UnknownType, TypeError, re.error):
        return InputReason.SCHEMA_INVALID
    except (Unresolvable, ValueError):
        return InputReason.REFERENCE_UNRESOLVED
    except (OverflowError, RecursionError):
        return InputReason.SCHEMA_EVALUATION_UNAVAILABLE
    return None
