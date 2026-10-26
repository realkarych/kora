import json
import re
from dataclasses import dataclass, replace
from typing import Final, cast

from pydantic import TypeAdapter, ValidationError

from kora.executor.failures import Phase, invalid
from kora.ir.vcs_ref import MAX_PATH_BYTES, MAX_PATHS
from kora.protocols.vcs import (
    MAX_DIFF_STAT_BYTES,
    Capture,
    InspectionManifest,
    RelativePath,
)

MAX_IDENTIFIER_OUTPUT: Final[int] = 512
MAX_CHANGED_PATHS_OUTPUT: Final[int] = MAX_PATH_BYTES * 2 + MAX_PATHS * 4 + 2
MAX_DIFF_OUTPUT: Final[int] = 1_048_576
MAX_INSPECTION_OUTPUT: Final[int] = 65_536
UNCAPTURED_OUTPUT: Final[int] = 0
CAPTURE_LIMITS: Final[dict[Capture, int]] = {
    "commit": MAX_IDENTIFIER_OUTPUT,
    "branch": MAX_IDENTIFIER_OUTPUT,
    "changed_paths": MAX_CHANGED_PATHS_OUTPUT,
    "diff": MAX_DIFF_OUTPUT,
    "diff_stat": MAX_DIFF_STAT_BYTES + 1,
    "inspection": MAX_INSPECTION_OUTPUT,
}
SINGLE_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,255}")
PATHS: Final[TypeAdapter[tuple[RelativePath, ...]]] = TypeAdapter(
    tuple[RelativePath, ...]
)


@dataclass(frozen=True)
class Captures:
    commit: str | None = None
    branch: str | None = None
    changed_paths: tuple[str, ...] | None = None
    diff: str | None = None
    diff_stat: str | None = None
    inspection: InspectionManifest | None = None


def capture_limit(capture: Capture | None) -> int:
    return UNCAPTURED_OUTPUT if capture is None else CAPTURE_LIMITS[capture]


def text_of(output: bytes, phase: Phase) -> str:
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        invalid("capture_malformed", phase)


def without_newline(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def single_identifier(output: bytes, phase: Phase) -> str:
    text = without_newline(text_of(output, phase))
    if SINGLE_IDENTIFIER.fullmatch(text) is None:
        invalid("capture_malformed", phase)
    return text


def changed_paths(output: bytes, phase: Phase) -> tuple[str, ...]:
    try:
        document: object = json.loads(text_of(output, phase))
    except json.JSONDecodeError:
        invalid("capture_malformed", phase)
    if not isinstance(document, list):
        invalid("capture_malformed", phase)
    try:
        paths = PATHS.validate_python(
            tuple(cast("list[object]", document)), strict=True
        )
    except ValidationError:
        invalid("capture_malformed", phase)
    if (
        len(paths) > MAX_PATHS
        or len(set(paths)) != len(paths)
        or sum(len(path.encode("utf-8")) for path in paths) > MAX_PATH_BYTES
    ):
        invalid("capture_oversized", phase)
    return paths


def diff_stat(output: bytes, phase: Phase) -> str:
    text = without_newline(text_of(output, phase))
    if "\x00" in text or len(text.encode("utf-8")) > MAX_DIFF_STAT_BYTES:
        invalid("capture_malformed", phase)
    return text


def inspection(output: bytes, phase: Phase) -> InspectionManifest:
    try:
        return InspectionManifest.model_validate_json(output, strict=True)
    except ValidationError:
        invalid("capture_malformed", phase)


def parsed(
    captures: Captures,
    capture: Capture,
    output: bytes,
    overflow: bool,
    phase: Phase,
) -> Captures:
    if overflow:
        invalid("capture_oversized", phase)
    if capture == "commit":
        return replace(captures, commit=single_identifier(output, phase))
    if capture == "branch":
        return replace(captures, branch=single_identifier(output, phase))
    if capture == "changed_paths":
        return replace(captures, changed_paths=changed_paths(output, phase))
    if capture == "diff":
        return replace(captures, diff=text_of(output, phase))
    if capture == "diff_stat":
        return replace(captures, diff_stat=diff_stat(output, phase))
    return replace(captures, inspection=inspection(output, phase))
