from collections.abc import Callable
from typing import LiteralString

from pydantic import BaseModel, ValidationError

from kora.ir.errors import safe_field_errors
from tests.ir.config_cases import CONFIG_FIELDS

SAFE_MESSAGE: LiteralString = "the execution configuration is invalid"


def validate_config(
    model: type[BaseModel], document: dict[str, object], valid: bool = True
) -> object:
    try:
        parsed = model.model_validate(document)
    except ValidationError as error:
        assert not valid
        errors = safe_field_errors(error, CONFIG_FIELDS, SAFE_MESSAGE)
        assert errors
        return errors
    assert valid
    return parsed


def validation_action(
    model: type[BaseModel], document: dict[str, object], valid: bool = True
) -> Callable[[], object]:
    return lambda: validate_config(model, document, valid)


def export_action(
    model: type[BaseModel], document: dict[str, object]
) -> Callable[[], object]:
    parsed = model.model_validate(document)
    return lambda: parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)


def round_trip_action(
    model: type[BaseModel], document: dict[str, object]
) -> Callable[[], object]:
    def round_trip() -> object:
        parsed = model.model_validate(document)
        return parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)

    return round_trip
