import re
from typing import Annotated, Final, Self

from pydantic import (
    MISSING,
    BeforeValidator,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir.config import environment_schema, require_integer
from kora.protocols.boundary import BoundaryModel

MAX_RELEASE_BATCH: Final[int] = 1000
MAX_RELEASE_INTERVAL: Final[int] = 86400
QuotaIdentifier = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[\x21-\x7e]{1,256}(?![\s\S])"))
]
ModelPatterns = Annotated[tuple[QuotaIdentifier, ...], Field(min_length=1)]


class HarnessQuota(BoundaryModel):
    resource: QuotaIdentifier | MISSING = MISSING
    groups: Annotated[
        dict[QuotaIdentifier, ModelPatterns],
        Field(json_schema_extra=environment_schema),
    ] = Field(default_factory=lambda: {"account": ("*",)})
    vendor_groups: Annotated[
        dict[QuotaIdentifier, QuotaIdentifier],
        Field(json_schema_extra=environment_schema),
    ] = Field(default_factory=dict)
    release_batch: Annotated[
        StrictInt, Field(ge=1, le=MAX_RELEASE_BATCH), BeforeValidator(require_integer)
    ] = 1
    release_interval: Annotated[
        StrictFloat, Field(gt=0, le=MAX_RELEASE_INTERVAL, allow_inf_nan=False)
    ] = 1.0

    @model_validator(mode="after")
    def declared_vendor_targets(self) -> Self:
        if any(group not in self.groups for group in self.vendor_groups.values()):
            raise PydanticCustomError(
                "quota_group_unknown",
                "a vendor limit group targets an undeclared group",
            )
        return self
