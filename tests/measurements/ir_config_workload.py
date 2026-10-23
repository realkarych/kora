from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from tests.ir.support import configuration_errors


def validate_config(
    model: type[BaseModel], document: dict[str, object], valid: bool = True
) -> object:
    try:
        parsed = model.model_validate(document)
    except ValidationError as error:
        assert not valid
        errors = configuration_errors(error)
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
