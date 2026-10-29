from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Discriminator
from pydantic_core import PydanticCustomError


class BoundaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def refusal_discriminator(
    tag: Callable[[object], object], refusal: Callable[[], PydanticCustomError]
) -> Discriminator:
    error = refusal()
    return Discriminator(
        tag,
        custom_error_type=error.type,
        custom_error_message=error.message_template,
    )
