import re
from hashlib import sha256
from typing import Never, cast

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from kora.ir.cost import UsageCoverage, UsageRow, identifier, money
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import (
    StructuredOutputValidationError,
    decode_json,
    snapshot,
)

MAX_LOGICAL_BYTES: int = 4_194_304
MAX_TRANSCRIPT_BYTES: int = 268_435_456
TRUNCATION_MARKER: bytes = b"\n[transcript truncated]\n"
BILLING_CLASSES: tuple[str, ...] = ("api_key", "subscription", "gateway", "managed")
TOKEN_FIELDS: frozenset[str] = frozenset(
    (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "reasoning_tokens",
    )
)
USAGE_FIELDS: frozenset[str] = frozenset(UsageRow.model_fields) - {"source"} | {
    "usd_estimate"
}
METADATA_FIELDS: frozenset[str] = frozenset(
    (
        "harness",
        "installed_version",
        "session_id",
        "billing_class",
        "usage",
        "usage_coverage",
        "price_table_id",
        "turns",
        "duration_ms",
        "stop_reason",
        "provenance",
    )
)
TRANSCRIPT_FIELDS: frozenset[str] = frozenset(
    (
        "capture",
        "source_size",
        "source_sha256",
        "source_verified",
        "retained_size",
        "retained_sha256",
        "truncated",
    )
)
_MESSAGES: dict[str, str] = {
    "AS001": "AgentSession raw logical JSON exceeds its byte limit.",
    "AS002": "AgentSession JSON encoding is invalid.",
    "AS003": "AgentSession value is outside its revision domain.",
    "AS004": "AgentSession canonical logical JSON exceeds its byte limit.",
    "AS005": "AgentSession revision descriptor is unsupported.",
    "AS006": "AgentSession retained transcript bytes disagree with their descriptor.",
}
_INVALID_TEXT: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_RULE: re.Pattern[str] = re.compile(r"(?:namespace|node)\.deny\.(?:0|[1-9][0-9]{0,3})")
_STOP: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DIGEST: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


class AgentSessionValidationError(Exception):
    violation: ContractViolation

    def __init__(self, code: str) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.violation = ContractViolation(code=code, message=message, path=())


def refuse(code: str = "AS003") -> Never:
    raise AgentSessionValidationError(code)


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


def integer(value: object, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        refuse()
    return value


def boolean(value: object) -> bool:
    if type(value) is not bool:
        refuse()
    return value


def text(value: object, maximum: int = 256) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        refuse()
    if _INVALID_TEXT.search(value) is not None:
        refuse()
    return value


def protocol_identifier(value: object) -> str:
    result = text(value)
    if any(not "!" <= char <= "~" for char in result):
        refuse()
    return result


def cost_identifier(value: object) -> str:
    result = text(value)
    valid = True
    try:
        identifier(result)
    except PydanticCustomError:
        valid = False
    if not valid:
        refuse()
    return result


def choice(value: object, options: tuple[str, ...]) -> str:
    result = text(value)
    if result not in options:
        refuse()
    return result


def array(value: object, maximum: int) -> list[object]:
    if type(value) is not list:
        refuse()
    items = cast("list[object]", value)
    if len(items) > maximum:
        refuse()
    return items


def scalar_fields(
    entry: dict[str, object], nested: frozenset[str] = frozenset()
) -> None:
    for name, value in entry.items():
        if name in nested or value is None or type(value) is bool:
            continue
        if type(value) is int:
            integer(value)
        elif type(value) is str:
            text(value)
        else:
            refuse()


def amount(value: object) -> None:
    if value is None:
        return
    rendered = text(value, 25)
    valid = True
    try:
        money(rendered)
    except PydanticCustomError:
        valid = False
    if not valid:
        refuse()


def usage(value: object) -> str:
    entry = closed(value, USAGE_FIELDS)
    scalar_fields(entry, frozenset(("known_token_fields", "cache_creation_by_ttl")))
    fields = array(entry["known_token_fields"], 5)
    for field in fields:
        if text(field) not in TOKEN_FIELDS:
            refuse()
    split = entry["cache_creation_by_ttl"]
    if split is not None:
        for count in closed(split, frozenset(("5m", "1h"))).values():
            integer(count, 2**31 - 1)
    amount(entry["usd_estimate"])
    supplied = {key: item for key, item in entry.items() if key != "usd_estimate"}
    supplied["source"] = {}
    parsed: UsageRow | None = None
    try:
        parsed = UsageRow.model_validate(supplied)
    except ValidationError:
        parsed = None
    if parsed is None or parsed.model_dump(mode="json") != supplied:
        refuse()
    estimate = entry["usd_estimate"]
    if parsed.billing_class == "subscription":
        if estimate is not None:
            refuse()
    elif parsed.basis in ("harness_list", "harness_managed"):
        if estimate != parsed.reported_usd:
            refuse()
    elif (
        not TOKEN_FIELDS - {"reasoning_tokens"} <= parsed.known_token_fields
        and estimate is not None
    ):
        refuse()
    return parsed.model


def coverage(value: object) -> None:
    entry = closed(value, frozenset(UsageCoverage.model_fields))
    scalar_fields(entry)
    if integer(entry["version"], 1) != 1:
        refuse()
    parsed: UsageCoverage | None = None
    try:
        parsed = UsageCoverage.model_validate(entry)
    except ValidationError:
        parsed = None
    if parsed is None or parsed.model_dump(mode="json") != entry:
        refuse()


def metadata(value: object) -> None:
    entry = closed(value, METADATA_FIELDS)
    protocol_identifier(entry["harness"])
    for key in ("installed_version", "session_id"):
        if entry[key] is not None:
            protocol_identifier(entry[key])
    if entry["billing_class"] is not None:
        choice(entry["billing_class"], BILLING_CLASSES)
    previous: str | None = None
    for item in array(entry["usage"], 64):
        name = usage(item)
        if previous is not None and name <= previous:
            refuse()
        previous = name
    coverage(entry["usage_coverage"])
    cost_identifier(entry["price_table_id"])
    integer(entry["turns"], 2**31 - 1)
    integer(entry["duration_ms"])
    if _STOP.fullmatch(text(entry["stop_reason"], 64)) is None:
        refuse()
    provenance = closed(entry["provenance"], frozenset(("worker_id", "image_digest")))
    protocol_identifier(provenance["worker_id"])
    if provenance["image_digest"] is not None:
        text(provenance["image_digest"], 255)


def denials(value: object) -> None:
    entry = closed(value, frozenset(("items", "total", "complete")))
    items = array(entry["items"], 1000)
    if integer(entry["total"]) < len(items):
        refuse()
    boolean(entry["complete"])
    harness_codes = ("harness_rule", "policy_error", "policy_timeout")
    policy_codes = (
        "denied",
        "protected_path",
        "not_allowed",
        "request_invalid",
        "argument_invalid",
    )
    for item in items:
        row = closed(
            item, frozenset(("tool", "tool_use_id", "source", "pattern", "code"))
        )
        protocol_identifier(row["tool"])
        protocol_identifier(row["tool_use_id"])
        source = choice(row["source"], ("harness", "policy"))
        code = choice(
            row["code"], harness_codes if source == "harness" else policy_codes
        )
        pattern = row["pattern"]
        if pattern is not None and (
            source != "policy"
            or code != "denied"
            or _RULE.fullmatch(text(pattern, 24)) is None
        ):
            refuse()


def transcript(value: object) -> None:
    if type(value) is not dict:
        refuse()
    candidate = cast("dict[object, object]", value)
    if len(candidate) == 1:
        entry = closed(candidate, frozenset(("capture",)))
        choice(entry["capture"], ("unavailable",))
        return
    entry = closed(candidate, TRANSCRIPT_FIELDS)
    choice(entry["capture"], ("captured",))
    source_size = integer(entry["source_size"])
    size = integer(entry["retained_size"], MAX_TRANSCRIPT_BYTES)
    for key in ("source_sha256", "retained_sha256"):
        if _DIGEST.fullmatch(text(entry[key], 64)) is None:
            refuse()
    verified = boolean(entry["source_verified"])
    truncated = boolean(entry["truncated"])
    if truncated:
        if (
            source_size <= MAX_TRANSCRIPT_BYTES
            or size != MAX_TRANSCRIPT_BYTES
            or verified
        ):
            refuse()
    elif (
        not verified
        or source_size != size
        or entry["source_sha256"] != entry["retained_sha256"]
    ):
        refuse()
    if size == 0 and entry["retained_sha256"] != sha256(b"").hexdigest():
        refuse()


def validate(value: object) -> JsonObject:
    entry = closed(value, frozenset(("metadata", "denials", "transcript")))
    metadata(entry["metadata"])
    denials(entry["denials"])
    transcript(entry["transcript"])
    result: JsonValue = None
    failure: str | None = None
    try:
        result = snapshot(value, max_bytes=MAX_LOGICAL_BYTES)
    except StructuredOutputValidationError as error:
        failure = "AS004" if error.violation.code == "SO004" else "AS003"
    if failure is not None:
        refuse(failure)
    return cast("JsonObject", result)


def decode(payload: bytes) -> JsonValue:
    result: JsonValue = None
    failure: str | None = None
    try:
        result = decode_json(payload, max_bytes=MAX_LOGICAL_BYTES)
    except StructuredOutputValidationError as error:
        failure = "AS001" if error.violation.code == "SO001" else "AS002"
    if failure is not None:
        refuse(failure)
    return result
