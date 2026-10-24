from typing import Annotated

from pydantic import AfterValidator, StringConstraints
from pydantic_core import PydanticCustomError

from kora.protocols.common import BoundaryModel
from kora.protocols.identity import Identifier


def printable_path(value: str) -> str:
    if not value.isprintable():
        raise PydanticCustomError("path_invalid", "the path is invalid")
    return value


PathText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=4096),
    AfterValidator(printable_path),
]


class WorkspaceTarget(BoundaryModel):
    root: PathText
    host_path: PathText | None


class WorkspaceRef(BoundaryModel):
    attempt_id: Identifier
    root: PathText
    repo: PathText
    commit: Identifier
    base_commit: Identifier
    branch: Identifier | None
