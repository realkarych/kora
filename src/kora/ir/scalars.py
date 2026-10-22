import re
from typing import Annotated

from pydantic import Field, StrictStr

Duration = Annotated[StrictStr, Field(pattern=re.compile(r"^[0-9]+(ms|s|m|h|d)$"))]
Digest = Annotated[StrictStr, Field(pattern=re.compile(r"^sha256:[0-9a-f]{64}$"))]
EnvName = Annotated[StrictStr, Field(pattern=re.compile(r"^[A-Z][A-Z0-9_]{0,63}$"))]
