from dataclasses import dataclass, field
from hashlib import sha256
from typing import cast

from kora.ir.command_result_validation import (
    MAX_CAPTURE_BYTES,
    decode,
    refuse,
    validate,
    verify_capture,
)
from kora.ir.command_result_validation import (
    CommandResultValidationError as CommandResultValidationError,
)
from kora.ir.json import JsonObject
from kora.ir.structured_json import encode_snapshot
from kora.ir.structured_output import same_descriptor


@dataclass(frozen=True, slots=True, init=False)
class CommandResult:
    _canonical: bytes = field(repr=False)

    def __init__(self, value: object) -> None:
        canonical = encode_snapshot(validate(value))
        object.__setattr__(self, "_canonical", canonical)

    @property
    def value(self) -> JsonObject:
        return checked_value(self)

    def canonical_bytes(self) -> bytes:
        checked_value(self)
        return self._canonical


def checked_value(value: CommandResult) -> JsonObject:
    if type(value) is not CommandResult:
        refuse()
    raw: object = getattr(value, "_canonical", None)
    if type(raw) is not bytes:
        refuse()
    result = validate(decode(raw))
    if encode_snapshot(result) != raw:
        refuse()
    return result


def parse_command_result(value: object) -> CommandResult:
    if type(value) is CommandResult:
        return CommandResult(checked_value(value))
    return CommandResult(value)


def parse_command_result_json(payload: bytes) -> CommandResult:
    return CommandResult(decode(payload))


def canonical_command_result(value: CommandResult) -> bytes:
    return CommandResult.canonical_bytes(value)


def command_result_digest(value: CommandResult) -> str:
    return sha256(canonical_command_result(value)).hexdigest()


def summarize_command_result(value: CommandResult) -> JsonObject:
    return checked_value(value)


def verify_command_result_captures(
    value: CommandResult, *, stdout: bytes, stderr: bytes
) -> None:
    if (
        type(stdout) is not bytes
        or type(stderr) is not bytes
        or len(stdout) > MAX_CAPTURE_BYTES
        or len(stderr) > MAX_CAPTURE_BYTES
    ):
        refuse("CR006")
    result = checked_value(value)
    verify_capture(cast("dict[str, object]", result["stdout"]), stdout)
    verify_capture(cast("dict[str, object]", result["stderr"]), stderr)


_DESCRIPTOR: JsonObject = {
    "name": "CommandResult",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:CommandResult",
    "parent": None,
    "behavior": {
        "validation": "kora.command-result-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_json_depth": 16,
        "max_count": 9223372036854775807,
        "max_capture_bytes": 67108864,
        "max_tail_bytes": 4096,
        "max_resources": 64,
        "max_resource_bytes": 262144,
        "max_resource_name_utf8_bytes": 4096,
        "max_resource_component_utf8_bytes": 255,
        "max_protected_paths": 64,
        "max_protected_path_characters": 4096,
        "max_protected_path_segments": 32,
        "max_protected_path_pieces": 8192,
        "max_protected_path_utf8_bytes": 65536,
        "max_protected_path_non_ascii_characters": 16384,
        "max_repo_utf8_bytes": 4096,
        "max_revision_characters": 255,
    },
    "canonicalization": "kora.command-result-json.v1",
    "digest_selection": [""],
    "schema_sha256": "8444eeb4d4688b6d059dd7221689dd4c824084c88ff189ee3fbb848153c1f8d4",
}


def require_command_result_revision(descriptor: object) -> None:
    if not same_descriptor(descriptor, _DESCRIPTOR):
        refuse("CR005")
