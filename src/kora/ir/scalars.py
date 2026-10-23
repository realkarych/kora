import re
from typing import Annotated

from pydantic import Field, StrictStr

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
