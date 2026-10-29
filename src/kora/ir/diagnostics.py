import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir.json import utf8_length

if TYPE_CHECKING:
    from kora.cel.diagnostics import Diagnostic as CelDiagnostic

MAX_INTEGER: int = 9_007_199_254_740_991
CODE: re.Pattern[str] = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
URI_SCHEME: re.Pattern[str] = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def bounded_text(value: str, limit: int) -> str:
    if len(value) > limit:
        raise PydanticCustomError(
            "diagnostic_text_limit", "text exceeds its byte limit"
        )
    length = utf8_length(value)
    if length is None:
        raise PydanticCustomError("diagnostic_unicode", "text contains invalid Unicode")
    if length > limit:
        raise PydanticCustomError(
            "diagnostic_text_limit", "text exceeds its byte limit"
        )
    return value


def text_256(value: str) -> str:
    return bounded_text(value, 256)


def text_512(value: str) -> str:
    return bounded_text(value, 512)


def text_1024(value: str) -> str:
    return bounded_text(value, 1024)


def diagnostic_code(value: str) -> str:
    if CODE.fullmatch(value) is None:
        raise PydanticCustomError("diagnostic_code", "invalid diagnostic code")
    return value


def source_label(value: str) -> str:
    if value.startswith("/") or URI_SCHEME.match(value) is not None:
        raise PydanticCustomError("diagnostic_source", "source label is not relative")
    return value


def path_sequence(value: object) -> object:
    if not isinstance(value, list | tuple):
        raise PydanticCustomError("diagnostic_path", "path is not a list")
    sequence = cast("list[object] | tuple[object, ...]", value)
    if len(sequence) > 64:
        raise PydanticCustomError("diagnostic_path_limit", "path has too many segments")
    return tuple(sequence)


def closed_fields(value: object, fields: Mapping[str, object]) -> object:
    if isinstance(value, Mapping):
        for key in cast("Mapping[object, object]", value):
            if type(key) is not str or key not in fields:
                raise PydanticCustomError(
                    "diagnostic_field_unknown", "diagnostic has an unknown field"
                )
    return cast("object", value)


Index = Annotated[int, Field(strict=True, ge=0, le=MAX_INTEGER)]
Line = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
Text256 = Annotated[StrictStr, AfterValidator(text_256)]
Text512 = Annotated[StrictStr, AfterValidator(text_512)]
Text1024 = Annotated[StrictStr, AfterValidator(text_1024)]
DiagnosticCode = Annotated[StrictStr, AfterValidator(diagnostic_code)]
SourceLabel = Annotated[Text1024, AfterValidator(source_label)]
DiagnosticPath = Annotated[tuple[Text256 | Index, ...], BeforeValidator(path_sequence)]


class SourceLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    file: SourceLabel
    line: Line

    @model_validator(mode="before")
    @classmethod
    def require_known_fields(cls, value: object) -> object:
        return closed_fields(value, cls.model_fields)


class Diagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    code: DiagnosticCode
    message: Text1024
    path: DiagnosticPath | None = None
    location: SourceLocation | None = None
    offset: Index | None = None
    node: Text256 | None = None
    hint: Text512 | None = None

    @model_validator(mode="before")
    @classmethod
    def require_known_fields(cls, value: object) -> object:
        return closed_fields(value, cls.model_fields)

    @model_validator(mode="after")
    def require_offset_path(self) -> Self:
        if self.offset is not None and self.path is None:
            raise PydanticCustomError(
                "diagnostic_offset_path", "an expression offset requires a path"
            )
        return self


ConfigWarning = Diagnostic
ContractViolation = Diagnostic


def from_cel(
    diagnostic: "CelDiagnostic",
    *,
    path: tuple[str | int, ...] | None,
    exact_expression_field: bool,
    location: SourceLocation | None = None,
    node: str | None = None,
    hint: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        code=diagnostic.code.value,
        message=diagnostic.message,
        path=path,
        location=location,
        offset=diagnostic.offset
        if path is not None and exact_expression_field
        else None,
        node=node,
        hint=hint,
    )
