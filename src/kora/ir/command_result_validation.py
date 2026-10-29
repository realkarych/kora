import base64
import binascii
import re
import unicodedata
from hashlib import sha256
from typing import Never, cast

from kora.ir.diagnostics import ContractViolation
from kora.ir.globs import GlobInvalid, compile_globs
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import (
    StructuredOutputValidationError,
    decode_json,
    encode_snapshot,
    snapshot,
)

MAX_CAPTURE_BYTES: int = 67_108_864
MAX_COUNT: int = 2**63 - 1
ROOT_FIELDS: frozenset[str] = frozenset(
    (
        "exit_code",
        "signal",
        "termination",
        "stop_reason",
        "duration_ms",
        "repo",
        "commit",
        "base_commit",
        "protected_paths",
        "protection",
        "resources",
        "resource_set_digest",
        "resource_protection",
        "stdout",
        "stderr",
    )
)
STREAM_FIELDS: frozenset[str] = frozenset(
    (
        "storage_ref",
        "payload_digest",
        "size",
        "observed_bytes",
        "eof",
        "truncated",
        "tail_base64",
    )
)
RESOURCE_FIELDS: frozenset[str] = frozenset(("name", "sha256", "size", "executable"))
_INVALID_TEXT: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_DIGEST: re.Pattern[str] = re.compile(r"sha256:[0-9a-f]{64}")
_MESSAGES: dict[str, str] = {
    "CR001": "CommandResult raw JSON exceeds its byte limit.",
    "CR002": "CommandResult JSON encoding is invalid.",
    "CR003": "CommandResult value is outside its revision domain.",
    "CR004": "CommandResult canonical JSON exceeds its byte limit.",
    "CR005": "CommandResult revision descriptor is unsupported.",
    "CR006": "CommandResult retained capture bytes disagree with their descriptor.",
}


class CommandResultValidationError(Exception):
    violation: ContractViolation

    def __init__(self, code: str) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.violation = ContractViolation(code=code, message=message, path=())


def refuse(code: str = "CR003") -> Never:
    raise CommandResultValidationError(code)


def closed(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        refuse()
    entries = cast("dict[object, object]", value)
    if len(entries) != len(fields):
        refuse()
    for key in entries:
        if type(key) is not str or key not in fields:
            refuse()
    return cast("dict[str, object]", entries)


def integer(value: object, minimum: int = 0, maximum: int = MAX_COUNT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        refuse()
    return value


def boolean(value: object) -> bool:
    if type(value) is not bool:
        refuse()
    return value


def text(value: object, maximum: int, minimum: int = 1) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        refuse()
    if _INVALID_TEXT.search(value) is not None:
        refuse()
    return value


def choice(value: object, options: tuple[str, ...]) -> str:
    result = text(value, 32)
    if result not in options:
        refuse()
    return result


def digest(value: object) -> str:
    result = text(value, 71)
    if _DIGEST.fullmatch(result) is None:
        refuse()
    return result


def array(value: object, maximum: int) -> list[object]:
    if type(value) is not list:
        refuse()
    items = cast("list[object]", value)
    if len(items) > maximum:
        refuse()
    return items


def protected_paths(value: object) -> list[str]:
    items = array(value, 64)
    paths = [text(item, 4096) for item in items]
    valid = True
    try:
        compile_globs(tuple(paths))
    except GlobInvalid:
        valid = False
    if not valid:
        refuse()
    return paths


def resource_name(value: object) -> str:
    name = text(value, 4096)
    if len(name.encode("utf-8")) > 4096 or "\\" in name:
        refuse()
    parts = name.split("/")
    if any(
        part in ("", ".", "..") or len(part.encode("utf-8")) > 255 for part in parts
    ):
        refuse()
    if unicodedata.normalize("NFC", name) != name:
        refuse()
    return name


def resources(value: object, expected_digest: object) -> list[object]:
    items = array(value, 64)
    previous: bytes | None = None
    for item in items:
        entry = closed(item, RESOURCE_FIELDS)
        name = resource_name(entry["name"]).encode("utf-8")
        if previous is not None and name <= previous:
            refuse()
        previous = name
        digest(entry["sha256"])
        integer(entry["size"], maximum=262_144)
        boolean(entry["executable"])
    if not items:
        if expected_digest is not None:
            refuse()
    elif (
        digest(expected_digest)
        != "sha256:" + sha256(encode_snapshot(cast("JsonValue", items))).hexdigest()
    ):
        refuse()
    return items


def tail(value: object) -> bytes:
    encoded = text(value, 5464, 0)
    valid = True
    decoded = b""
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        valid = False
    if not valid or base64.b64encode(decoded).decode("ascii") != encoded:
        refuse()
    return decoded


def stream(value: object) -> dict[str, object]:
    entry = closed(value, STREAM_FIELDS)
    reference = text(entry["storage_ref"], 256)
    if any(not "!" <= char <= "~" for char in reference):
        refuse()
    identity = digest(entry["payload_digest"])
    size = integer(entry["size"], maximum=MAX_CAPTURE_BYTES)
    observed = integer(entry["observed_bytes"])
    eof = boolean(entry["eof"])
    truncated = boolean(entry["truncated"])
    if observed < size or truncated != (observed > size or not eof):
        refuse()
    suffix = tail(entry["tail_base64"])
    if len(suffix) != min(size, 4096):
        refuse()
    if size <= 4096 and identity != "sha256:" + sha256(suffix).hexdigest():
        refuse()
    return entry


def validate(value: object) -> JsonObject:
    entry = closed(value, ROOT_FIELDS)
    status = integer(entry["exit_code"], -255, 255)
    signal = entry["signal"]
    if status < 0:
        if integer(signal, 1, 255) != -status:
            refuse()
    elif signal is not None:
        refuse()
    termination = choice(
        entry["termination"], ("none", "interrupted", "killed", "killed_after_grace")
    )
    reason = choice(
        entry["stop_reason"], ("completed", "deadline", "cancel", "journal_unavailable")
    )
    if reason == "completed" and termination != "none":
        refuse()
    integer(entry["duration_ms"])
    repo = text(entry["repo"], 4096)
    if len(repo.encode("utf-8")) > 4096:
        refuse()
    text(entry["commit"], 255)
    text(entry["base_commit"], 255)
    paths = protected_paths(entry["protected_paths"])
    if choice(entry["protection"], ("verified", "unprotected")) != (
        "verified" if paths else "unprotected"
    ):
        refuse()
    manifest = resources(entry["resources"], entry["resource_set_digest"])
    protection = choice(entry["resource_protection"], ("protected", "none"))
    if not manifest and protection != "none":
        refuse()
    stream(entry["stdout"])
    stream(entry["stderr"])
    result: JsonValue = None
    failure: str | None = None
    try:
        result = snapshot(value)
    except StructuredOutputValidationError as error:
        failure = "CR004" if error.violation.code == "SO004" else "CR003"
    if failure is not None:
        refuse(failure)
    return cast("JsonObject", result)


def decode(payload: bytes) -> JsonValue:
    result: JsonValue = None
    failure: str | None = None
    try:
        result = decode_json(payload)
    except StructuredOutputValidationError as error:
        failure = "CR001" if error.violation.code == "SO001" else "CR002"
    if failure is not None:
        refuse(failure)
    return result


def verify_capture(entry: dict[str, object], payload: bytes) -> None:
    if (
        len(payload) != entry["size"]
        or "sha256:" + sha256(payload).hexdigest() != entry["payload_digest"]
        or base64.b64encode(payload[-4096:]).decode("ascii") != entry["tail_base64"]
    ):
        refuse("CR006")
