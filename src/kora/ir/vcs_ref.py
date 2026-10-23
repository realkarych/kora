import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Annotated, ClassVar, Literal, Never, Self, cast

from pydantic import (
    MISSING,
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    StrictStr,
    model_validator,
)
from pydantic.config import ExtraValues

from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import (
    INVALID_SCALAR,
    MAX_JSON_BYTES,
    JsonRefusal,
    canonical_bytes,
    canonical_size,
    decode_vcs_ref_json,
)
from kora.ir.vcs_ref_url import valid_vcs_ref_url

TEXT_PATTERN: str = r"^[^\u0000-\u001f\u007f]*(?![\s\S])"
PATH_PATTERN: str = (
    r"^(?!\.{1,2}(?:/|$))(?![\s\S]*?/\.{1,2}(?:/|$))"
    r"[^/\u0000-\u001f\u007f\\]+(?:/[^/\u0000-\u001f\u007f\\]+)*(?![\s\S])"
)
URL_PATTERN: str = (
    r"^https?://[^/?#@\u0000-\u0020\u007f-\u009f\u00a0\u1680\u2000-\u200a"
    r"\u2028-\u2029\u202f\u205f\u3000\ufeff\\]+"
    r"(?:[/?#][^\u0000-\u0020\u007f-\u009f\u00a0\u1680\u2000-\u200a"
    r"\u2028-\u2029\u202f\u205f\u3000\ufeff\\]*)?(?![\s\S])"
)
_CONTROLS: re.Pattern[str] = re.compile(r"[\u0000-\u001f\u007f]")
_BAD_PATH: re.Pattern[str] = re.compile(r"(^|/)\.{1,2}(/|$)|//|^/|/$|\\")
_URL_SHAPE: re.Pattern[str] = re.compile(URL_PATTERN)
_KINDS: frozenset[str] = frozenset(
    {"app", "bot_user", "service_user", "user", "local_service"}
)
_PRINCIPAL_FIELDS: tuple[str, ...] = (
    "provider_instance",
    "stable_id",
    "kind",
    "display_name",
)
_REQUIRED: tuple[str, ...] = (
    "repo",
    "branch",
    "commit",
    "base_commit",
    "changed_paths",
    "diff_stat",
    "pushed_by",
)
_OPTIONAL: tuple[str, ...] = ("url", "commit_url", "compare_url", "opened_by")
_FIELDS: tuple[str, ...] = (*_REQUIRED, *_OPTIONAL)
_URL_FIELDS: tuple[str, ...] = ("url", "commit_url", "compare_url")
MAX_PATHS: int = 10_000
MAX_PATH_BYTES: int = 524_288
_MESSAGES: dict[str, str] = {
    "VCS001": "VcsRef raw JSON exceeds its byte limit.",
    "VCS002": "VcsRef JSON encoding is invalid.",
    "VCS003": "VcsRef payload shape is invalid.",
    "VCS004": "VcsRef changed paths exceed their byte limit.",
    "VCS005": "VcsRef URL syntax is invalid.",
    "VCS006": "VcsRef canonical JSON exceeds its byte limit.",
    "VCS007": "VcsRef revision descriptor is unsupported.",
}
Text = Annotated[StrictStr, Field(min_length=1, pattern=re.compile(TEXT_PATTERN))]
Identity = Annotated[Text, Field(max_length=255)]
ChangedPath = Annotated[
    StrictStr, Field(min_length=1, pattern=re.compile(PATH_PATTERN))
]
Url = Annotated[StrictStr, Field(pattern=re.compile(URL_PATTERN))]
PrincipalKind = Literal["app", "bot_user", "service_user", "user", "local_service"]


class VcsRefValidationError(Exception):
    violation: ContractViolation

    def __init__(self, code: str, path: tuple[str | int, ...] = ()) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.violation = ContractViolation(code=code, message=message, path=path)


def refuse(code: str, path: tuple[str | int, ...] = ()) -> Never:
    raise VcsRefValidationError(code, path)


class PrincipalRef(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
    )

    provider_instance: Identity
    stable_id: Identity
    kind: PrincipalKind
    display_name: Identity

    @model_validator(mode="wrap")
    @classmethod
    def validate_snapshot(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        document = principal_shape(value, ())
        return handler(document)

    def identity(self) -> tuple[str, str]:
        value = PrincipalRef.model_validate(self)
        return value.provider_instance, value.stable_id


class VcsRef(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:kora:artifact:VcsRef",
            "dependentRequired": {"url": ["opened_by"], "opened_by": ["url"]},
        },
    )

    repo: Text
    branch: Text
    commit: Identity
    base_commit: Identity
    changed_paths: Annotated[
        tuple[ChangedPath, ...],
        Field(max_length=MAX_PATHS, json_schema_extra={"uniqueItems": True}),
    ]
    diff_stat: Annotated[StrictStr, Field(pattern=re.compile(r"^[^\u0000]*(?![\s\S])"))]
    pushed_by: PrincipalRef
    url: Url | MISSING = MISSING
    commit_url: Url | MISSING = MISSING
    compare_url: Url | MISSING = MISSING
    opened_by: PrincipalRef | MISSING = MISSING
    revision: ClassVar[str] = "1"

    @model_validator(mode="wrap")
    @classmethod
    def validate_payload(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        document = vcs_ref_shape(value)
        paths = cast("list[str] | tuple[str, ...]", document["changed_paths"])
        total = 0
        for path in paths:
            remaining = MAX_PATH_BYTES - total
            if len(path) > remaining:
                refuse("VCS004", ("changed_paths",))
            total += len(path.encode("utf-8"))
            if total > MAX_PATH_BYTES:
                refuse("VCS004", ("changed_paths",))
        for name in _URL_FIELDS:
            if name in document and not valid_vcs_ref_url(cast("str", document[name])):
                refuse("VCS005", (name,))
        projection = payload_projection(document)
        if canonical_size(projection) > MAX_JSON_BYTES:
            refuse("VCS006")
        prepared = dict(document)
        prepared["changed_paths"] = tuple(paths)
        return handler(prepared)

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        if type(json_data) is str:
            if len(json_data) > MAX_JSON_BYTES:
                refuse("VCS001")
            encoded = encode_json_input(json_data)
        elif type(json_data) is bytes or type(json_data) is bytearray:
            if len(json_data) > MAX_JSON_BYTES:
                refuse("VCS001")
            encoded = bytes(json_data)
        else:
            refuse("VCS002")
        return cast("Self", parse_vcs_ref_json(encoded))


def closed_document(
    value: object,
    model: type[BaseModel],
    fields: tuple[str, ...],
    path: tuple[str | int, ...],
) -> tuple[Mapping[str, object], bool]:
    instance = type(value) is model
    if instance:
        entries = cast("Mapping[str, object]", value.__dict__)
    elif type(value) is dict:
        entries = cast("dict[str, object]", value)
    else:
        refuse("VCS003", path)
    if len(entries) > len(fields):
        refuse("VCS003", path)
    for key in entries:
        if type(key) is not str or key not in fields:
            refuse("VCS003", path)
    return entries, instance


def text_shape(
    value: object,
    path: tuple[str | int, ...],
    *,
    minimum: int = 1,
    maximum: int | None = None,
    controls: bool = True,
) -> str:
    if type(value) is not str:
        refuse("VCS003", path)
    text = value
    if len(text) < minimum or (maximum is not None and len(text) > maximum):
        refuse("VCS003", path)
    if INVALID_SCALAR.search(text) is not None:
        refuse("VCS003", path)
    if controls and _CONTROLS.search(text) is not None:
        refuse("VCS003", path)
    return text


def principal_shape(value: object, path: tuple[str | int, ...]) -> dict[str, object]:
    document, _ = closed_document(value, PrincipalRef, _PRINCIPAL_FIELDS, path)
    for name in _PRINCIPAL_FIELDS:
        if name not in document:
            refuse("VCS003", (*path, name))
        text = text_shape(document[name], (*path, name), maximum=255)
        if name == "kind" and text not in _KINDS:
            refuse("VCS003", (*path, name))
    return dict(document)


def vcs_ref_shape(value: object) -> dict[str, object]:
    document, instance = closed_document(value, VcsRef, _FIELDS, ())
    for name in _FIELDS:
        if name not in document or (instance and document[name] is MISSING):
            if name in _REQUIRED:
                refuse("VCS003", (name,))
            continue
        item = document[name]
        if name == "changed_paths":
            path_shape(item, instance)
        elif name in ("pushed_by", "opened_by"):
            principal_shape(item, (name,))
        elif name in _URL_FIELDS:
            text = text_shape(item, (name,), minimum=0, controls=False)
            if _URL_SHAPE.fullmatch(text) is None:
                refuse("VCS003", (name,))
        else:
            text_shape(
                item,
                (name,),
                minimum=0 if name == "diff_stat" else 1,
                maximum=255 if name in ("commit", "base_commit") else None,
                controls=name != "diff_stat",
            )
    present = {name: item for name, item in document.items() if item is not MISSING}
    if ("url" in present) != ("opened_by" in present):
        refuse("VCS003", ("opened_by" if "url" in present else "url",))
    return present


def path_shape(value: object, instance: bool) -> None:
    if type(value) is not list and not (instance and type(value) is tuple):
        refuse("VCS003", ("changed_paths",))
    paths = cast("list[object] | tuple[object, ...]", value)
    if len(paths) > MAX_PATHS:
        refuse("VCS003", ("changed_paths",))
    seen: set[str] = set()
    for index, item in enumerate(paths):
        text = text_shape(item, ("changed_paths", index))
        if _BAD_PATH.search(text) is not None or text in seen:
            refuse("VCS003", ("changed_paths", index))
        seen.add(text)


def principal_projection(value: object) -> JsonObject:
    document = principal_shape(value, ())
    return {name: cast("str", document[name]) for name in _PRINCIPAL_FIELDS}


def payload_projection(document: Mapping[str, object]) -> JsonObject:
    projection: JsonObject = {}
    for name in _FIELDS:
        if name not in document or document[name] is MISSING:
            continue
        value = document[name]
        if name in ("pushed_by", "opened_by"):
            projection[name] = principal_projection(value)
        elif name == "changed_paths":
            projection[name] = [
                item for item in cast("list[str] | tuple[str, ...]", value)
            ]
        else:
            projection[name] = cast("str", value)
    return projection


def encode_json_input(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        refuse("VCS002")
    return encoded


def parse_vcs_ref(value: object) -> VcsRef:
    return VcsRef.model_validate(value)


def parse_vcs_ref_json(payload: bytes) -> VcsRef:
    if type(payload) is not bytes:
        refuse("VCS002")
    decoded = decode_vcs_ref_json(payload)
    if decoded is JsonRefusal.SIZE:
        refuse("VCS001")
    if decoded is JsonRefusal.DECODE:
        refuse("VCS002")
    return parse_vcs_ref(decoded)


def checked_projection(ref: VcsRef) -> JsonObject:
    if type(ref) is not VcsRef:
        refuse("VCS003")
    value = parse_vcs_ref(ref)
    return payload_projection(cast("Mapping[str, object]", value.__dict__))


def canonical_vcs_ref(ref: VcsRef) -> bytes:
    return canonical_bytes(checked_projection(ref))


def vcs_ref_digest(ref: VcsRef) -> str:
    return sha256(canonical_vcs_ref(ref)).hexdigest()


def summarize_vcs_ref(ref: VcsRef) -> JsonObject:
    return checked_projection(ref)


_DESCRIPTOR: JsonObject = {
    "name": "VcsRef",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:VcsRef",
    "parent": None,
    "behavior": {
        "validation": "kora.vcs-ref-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_changed_paths": 10000,
        "max_changed_path_utf8_bytes": 524288,
        "max_revision_characters": 255,
        "max_principal_characters": 255,
        "max_json_depth": 16,
    },
    "digest_selection": [""],
    "schema_sha256": "6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8",
}


def same_revision_value(value: object, expected: JsonValue) -> bool:
    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        entries = cast("dict[str, object]", value)
        shape = cast("JsonObject", expected)
        if any(type(name) is not str for name in entries):
            return False
        return len(entries) == len(shape) and all(
            name in entries and same_revision_value(entries[name], item)
            for name, item in shape.items()
        )
    if type(expected) is list:
        entries = cast("list[object]", value)
        items = cast("list[JsonValue]", expected)
        return len(entries) == len(items) and all(
            same_revision_value(left, right)
            for left, right in zip(entries, items, strict=True)
        )
    return value == expected


def require_vcs_ref_revision(descriptor: object) -> None:
    if not same_revision_value(descriptor, _DESCRIPTOR):
        refuse("VCS007")
