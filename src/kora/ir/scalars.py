import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator, Field, StrictStr
from pydantic_core import PydanticCustomError

Duration = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[0-9]+(ms|s|m|h|d)(?![\s\S])"))
]
Digest = Annotated[
    StrictStr, Field(pattern=re.compile(r"^sha256:[0-9a-f]{64}(?![\s\S])"))
]
EnvName = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[A-Z][A-Z0-9_]{0,63}(?![\s\S])"))
]
NodeId = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[a-z][a-z0-9_]{0,63}(?![\s\S])"))
]
BranchTarget = Annotated[
    StrictStr,
    Field(pattern=re.compile(r"^(\$fail_attempt|[a-z][a-z0-9_]{0,63})(?![\s\S])")),
]
Expression = Annotated[StrictStr, Field(min_length=1, max_length=4096)]


TIMESTAMP_PATTERN: str = (
    r"^(?!0000)[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[Tt]"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
    r"([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])(?![\s\S])"
)


def calendar_timestamp(value: str) -> str:
    try:
        date.fromisoformat(value[:10])
    except ValueError as error:
        raise PydanticCustomError("ir_timestamp", "the timestamp is invalid") from error
    return value


Timestamp = Annotated[
    StrictStr,
    Field(
        pattern=re.compile(TIMESTAMP_PATTERN), json_schema_extra={"format": "date-time"}
    ),
    AfterValidator(calendar_timestamp),
]
HarnessName = Annotated[
    StrictStr,
    Field(pattern=re.compile(r"^[a-z][a-z0-9_]*(:[a-z0-9][a-z0-9_.-]*)?(?![\s\S])")),
]
SensorName = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[a-z][a-z0-9_]*(?![\s\S])"))
]
IrVersion = Annotated[StrictStr, Field(pattern=re.compile(r"^1\.[0-9]+(?![\s\S])"))]
TypeContractName = Annotated[
    StrictStr,
    Field(pattern=re.compile(r"^([a-z][a-z0-9_]*/)?[A-Z][A-Za-z0-9]*(?![\s\S])")),
]
RetentionPeriod = Annotated[
    StrictStr, Field(pattern=re.compile(r"^([0-9]+d|forever)(?![\s\S])"))
]
SecretRef = Annotated[
    StrictStr,
    Field(pattern=re.compile(r"^secret://[a-z][a-z0-9_-]*/[A-Za-z0-9_./-]+(?![\s\S])")),
]
ArtifactType = Annotated[
    StrictStr,
    Field(
        pattern=re.compile(
            r"^(([a-z][a-z0-9_]*/)?[A-Z][A-Za-z0-9]*|List\[(List\[)*([a-z][a-z0-9_]*/)?[A-Z][A-Za-z0-9]*\]+)(?![\s\S])"
        )
    ),
]
InputSource = Annotated[
    StrictStr,
    Field(
        pattern=re.compile(
            r"^(\$params|\$inputs|\$previous_attempt|[a-z][a-z0-9_]{0,63})(?![\s\S])"
        )
    ),
]
