import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from hashlib import sha256
from typing import Never, ParamSpec, TypeVar, cast

from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject
from kora.ir.structured_json import (
    MAX_JSON_BYTES,
    CanonicalBudget,
    StructuredOutputValidationError,
    encode_snapshot,
    snapshot,
    spend_string,
)
from kora.ir.structured_output import same_descriptor
from kora.ir.vcs_ref_json import refuse_constant, unique_object

_P = ParamSpec("_P")
_T = TypeVar("_T")
_ROOT_FIELDS = frozenset(("subgraph_attempt", "reason", "error_class", "artifacts"))
_REFERENCE_FIELDS = frozenset(("artifact_version_id",))
_MAX_FIELD_LENGTH = len("artifact_version_id")
_MAX_ORDINAL = 99
_MIN_KEY_LENGTH = 3
_ROOT_SYNTAX = len(b'{"artifacts":{},"error_class":"","reason":,"subgraph_attempt":}')
_MIN_ENTRY = len(b'"":{"artifact_version_id":""}') + 26 + _MIN_KEY_LENGTH
_MIN_ROOT = _ROOT_SYNTAX + len(b'timeout""1')
_MAX_ARTIFACTS = (MAX_JSON_BYTES - _MIN_ROOT + 1) // (_MIN_ENTRY + 1)
_MEMBER_LIMITS = (len(_ROOT_FIELDS), _MAX_ARTIFACTS, len(_REFERENCE_FIELDS))
_MAX_DEPTH = len(_MEMBER_LIMITS)
_SLOT = re.compile(r"[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}")
_IDENTIFIER = re.compile(r"[0-7][0-9ABCDEFGHJKMNPQRSTVWXYZ]{25}")
_ORDINAL_TOKEN = re.compile(r"[1-9][0-9]?")
_ERROR_CLASSES = frozenset(
    (
        "harness_error command_failed timeout events_exhausted budget_exhausted "
        "artifact_invalid attempt_failed approval_rejected approval_expired "
        "candidate_moved wait_expired stalled provider_limited protected_path_modified "
        "secret_leak integrity_error infrastructure cancelled config_error"
    ).split()
)
_MAX_CLASS_LENGTH = max(map(len, _ERROR_CLASSES))
_MESSAGES: dict[str, str] = {
    "PA001": "PreviousAttempt raw JSON exceeds its byte limit.",
    "PA002": "PreviousAttempt JSON encoding is invalid.",
    "PA003": "PreviousAttempt value is outside its revision domain.",
    "PA004": "PreviousAttempt canonical JSON exceeds its byte limit.",
    "PA005": "PreviousAttempt revision descriptor is unsupported.",
    "PA006": "PreviousAttempt retained bytes fail canonical or digest comparison.",
}


class PreviousAttemptValidationError(Exception):
    violation: ContractViolation

    def __init__(self, code: str) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.violation = ContractViolation(code=code, message=message, path=())


def _refuse(code: str = "PA003") -> Never:
    raise PreviousAttemptValidationError(code)


def _content_free_refusal(operation: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(operation)
    def checked(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return operation(*args, **kwargs)
        except PreviousAttemptValidationError as error:
            code = error.violation.code
        del args, kwargs
        try:
            _refuse(code)
        except PreviousAttemptValidationError as refusal:
            refusal.__context__ = None
            raise

    return checked


def _lexically_admitted(payload: bytes) -> bool:
    depth = 0
    members = [0] * _MAX_DEPTH
    separators = [0] * _MAX_DEPTH
    quoted = escaped = inventory_seen = False
    for byte in payload:
        if escaped:
            escaped = False
        elif byte == 34:
            quoted = not quoted
        elif quoted:
            escaped = byte == 92
        elif byte == 123:
            if depth == _MAX_DEPTH or (depth == 1 and inventory_seen):
                return False
            inventory_seen = inventory_seen or depth == 1
            members[depth] = separators[depth] = 0
            depth += 1
        elif byte == 125:
            if depth == 0:
                return False
            depth -= 1
        elif byte in (91, 93):
            return False
        elif byte in (58, 44):
            if depth == 0:
                return False
            level = depth - 1
            counts = members if byte == 58 else separators
            counts[level] += 1
            if counts[level] > _MEMBER_LIMITS[level] - int(byte == 44):
                return False
    return depth == 0 and not quoted


def _ordinal(token: str) -> int:
    if _ORDINAL_TOKEN.fullmatch(token) is None:
        raise ValueError("invalid PreviousAttempt integer token")
    return int(token)


def _admit_raw(payload: object) -> bytes:
    if type(payload) is not bytes:
        _refuse("PA002")
    if len(payload) > MAX_JSON_BYTES:
        _refuse("PA001")
    return payload


def _decode(payload: bytes) -> object:
    if not _lexically_admitted(payload):
        _refuse("PA002")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_int=_ordinal,
            parse_float=refuse_constant,
            parse_constant=refuse_constant,
        )
    except ValueError:
        _refuse("PA002")


def _closed(value: object, fields: frozenset[str]) -> dict[str, object]:
    entries = cast("dict[object, object]", value)
    if type(value) is not dict or len(entries) != len(fields):
        _refuse()
    for key in entries:
        if type(key) is not str or len(key) > _MAX_FIELD_LENGTH or key not in fields:
            _refuse()
    return cast("dict[str, object]", entries)


def _charge_artifacts(artifacts: dict[object, object], budget: CanonicalBudget) -> None:
    for slot, entry in artifacts.items():
        if type(slot) is not str or _SLOT.fullmatch(slot) is None:
            _refuse()
        identifier = _closed(entry, _REFERENCE_FIELDS)["artifact_version_id"]
        if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
            _refuse()
        budget.spend(len(slot) - _MIN_KEY_LENGTH)


def _charge(value: object) -> JsonObject:
    root = _closed(value, _ROOT_FIELDS)
    artifacts, ordinal = root["artifacts"], root["subgraph_attempt"]
    reason, error_class = root["reason"], root["error_class"]
    if (
        type(artifacts) is not dict
        or type(ordinal) is not int
        or not 1 <= ordinal <= _MAX_ORDINAL
        or type(reason) is not str
        or type(error_class) is not str
        or len(error_class) > _MAX_CLASS_LENGTH
        or error_class not in _ERROR_CLASSES
    ):
        _refuse()
    entries = cast("dict[object, object]", artifacts)
    count = len(entries)
    if count > _MAX_ARTIFACTS:
        _refuse("PA004")
    budget = CanonicalBudget()
    budget.spend(_ROOT_SYNTAX + len(error_class) + len(str(ordinal)))
    budget.spend(count * _MIN_ENTRY + max(0, count - 1))
    spend_string(reason, budget, "SO003")
    _charge_artifacts(entries, budget)
    return cast(JsonObject, root)


def _checked(value: object, *, detach: bool = False) -> JsonObject:
    try:
        checked = _charge(value)
        return _charge(snapshot(checked)) if detach else checked
    except StructuredOutputValidationError as error:
        _refuse("PA004" if error.violation.code == "SO004" else "PA003")


def _read_canonical(payload: bytes) -> JsonObject:
    value = _checked(_decode(_admit_raw(payload)))
    if encode_snapshot(value) != payload:
        _refuse("PA006")
    return value


@dataclass(frozen=True, slots=True, init=False)
class PreviousAttempt:
    _canonical: bytes = field(repr=False)

    @_content_free_refusal
    def __init__(self, value: object) -> None:
        canonical = encode_snapshot(_checked(value, detach=True))
        object.__setattr__(self, "_canonical", canonical)

    @property
    @_content_free_refusal
    def value(self) -> JsonObject:
        return _read_canonical(self._canonical)

    def canonical_bytes(self) -> bytes:
        return self._canonical

    def payload_digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()


@_content_free_refusal
def parse_previous_attempt_json(payload: bytes) -> PreviousAttempt:
    _read_canonical(payload)
    value = object.__new__(PreviousAttempt)
    object.__setattr__(value, "_canonical", payload)
    return value


@_content_free_refusal
def parse_retained_previous_attempt(
    payload: bytes, *, expected_payload_digest: str
) -> PreviousAttempt:
    raw = _admit_raw(payload)
    if (
        type(expected_payload_digest) is not str
        or expected_payload_digest != "sha256:" + sha256(raw).hexdigest()
    ):
        _refuse("PA006")
    return parse_previous_attempt_json(raw)


_DESCRIPTOR: JsonObject = {
    "name": "PreviousAttempt",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:PreviousAttempt",
    "parent": None,
    "behavior": {
        "validation": "kora.previous-attempt-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": MAX_JSON_BYTES,
        "max_canonical_json_bytes": MAX_JSON_BYTES,
        "max_json_depth": _MAX_DEPTH,
        "max_artifact_entries": _MAX_ARTIFACTS,
    },
    "canonicalization": "kora.structured-json.v1",
    "digest_selection": [""],
    "schema_sha256": "07d9e6e7e22334039584177116f56b8239c21a495633cf740caeda66097a7fd7",
}


@_content_free_refusal
def require_previous_attempt_revision(descriptor: object) -> None:
    if not same_descriptor(descriptor, _DESCRIPTOR):
        _refuse("PA005")


def previous_attempt_revision_descriptor() -> JsonObject:
    return cast(JsonObject, snapshot(_DESCRIPTOR))
