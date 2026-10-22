import json
import os
import resource
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast

from kora.cel import ValueBound
from kora.cel.types import STRING
from kora.cel_eval.binding import Program, load_binding
from kora.cel_eval.thread import EvaluationThread
from tests.measurements.cel_cost_catalogue import CostCase
from tests.measurements.cel_cost_workload import digest

COMPILED_LIMIT: int = 10_485_760
UPPER_REPETITIONS: int = 1_048_576
BOUNDARY_PRELUDE: str = (
    "import json,sys;sys.path[:]=json.loads(sys.argv[1]);"
    "from tests.measurements.cel_regex_workload import run_boundary;"
    "run_boundary(sys.argv[2])"
)
ESCAPE_NAMES: tuple[str, ...] = ("d", "D", "w", "W", "s", "S")
UNICODE_NAMES: tuple[str, ...] = (
    "L",
    "Lu",
    "Ll",
    "N",
    "Nd",
    "P",
    "S",
    "Z",
    "Greek",
    "Latin",
    "Han",
    "Cyrillic",
)


@dataclass(frozen=True)
class PatternForm:
    name: str
    atom: str
    terms: tuple[tuple[str, int], ...]
    flags: str = ""
    minimum_any: bool = False
    anchor: bool = False

    def pattern(self, count: int, *, repetition: bool = False) -> str:
        body = f"(?:{self.atom}){{{count}}}" if repetition else self.atom * count
        return self.flags + body

    def unit(self, table: Mapping[str, int]) -> int:
        value = sum(table[row] * count for row, count in self.terms)
        return max(value, table["any"]) if self.minimum_any else value

    def sizes(self) -> tuple[int, ...]:
        if any(row.startswith(("class:", "unicode:")) for row, _ in self.terms):
            return 1, 2, 4, 8
        if len(json.dumps(self.atom, ensure_ascii=False)) > 8:
            return 16, 32, 64, 128
        return 64, 128, 256, 512

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "atom": self.atom,
            "terms": [list(term) for term in self.terms],
            "flags": self.flags,
            "minimum_any": self.minimum_any,
            "anchor": self.anchor,
        }


def pattern_forms() -> tuple[PatternForm, ...]:
    ordinary: list[PatternForm] = [
        PatternForm("literal_ascii", "a", (("literal", 1),)),
        PatternForm("literal_wide", "\u03a9", (("literal", 1),)),
        PatternForm("literal_supplementary", "\U0001f642", (("literal", 1),)),
        PatternForm("escaped_control", r"\n", (("literal", 1),)),
        PatternForm("escaped_ascii", r"\x61", (("literal", 1),)),
        PatternForm("escaped_supplementary", r"\x{1f642}", (("literal", 1),)),
        PatternForm("escaped_punctuation", r"\+", (("literal", 1),)),
        PatternForm("literal_bracket", "[a]", (("literal", 1),)),
        PatternForm("range_ascii", "[a-z]", (("range", 1),)),
        PatternForm(
            "range_wide", r"[\x{80}-\x{10ffff}]", (("range", 1),), minimum_any=True
        ),
        PatternForm("dot", ".", (("any", 1),)),
        PatternForm("dot_all", ".", (("any", 1),), "(?s)"),
        PatternForm("negated_literal", "[^a]", (("literal", 1),), minimum_any=True),
        PatternForm(
            "composed_bracket", r"[\p{L}\d]", (("unicode:L", 1), ("class:d", 1))
        ),
        PatternForm(
            "negated_bracket",
            r"[^\p{L}\d]",
            (("unicode:L", 1), ("class:d", 1)),
            minimum_any=True,
        ),
        PatternForm("nested_bracket", "[[a-z]0-9]", (("range", 2),)),
    ]
    ordinary.extend(
        PatternForm("escape_" + name, "\\" + name, (("class:" + name, 1),))
        for name in ESCAPE_NAMES
    )
    ordinary.extend(
        PatternForm(
            f"unicode_{name}_{polarity}",
            "\\" + polarity + "{" + name + "}",
            (("unicode:" + name, 1),),
            minimum_any=polarity == "P",
        )
        for name in UNICODE_NAMES
        for polarity in ("p", "P")
    )
    result: list[PatternForm] = []
    for form in ordinary:
        result.append(form)
        terms = tuple(
            (
                "caseless_literal"
                if row == "literal"
                else "caseless_range"
                if row == "range"
                else row,
                count,
            )
            for row, count in form.terms
        )
        result.append(
            PatternForm(
                form.name + "_caseless",
                form.atom,
                terms,
                form.flags + "(?i)",
                form.minimum_any,
            )
        )
    result.extend(
        PatternForm("anchor_" + name, atom, (("anchor", 1),), anchor=True)
        for name, atom in (
            ("start", "^"),
            ("end", "$"),
            ("word", r"\b"),
            ("nonword", r"\B"),
            ("absolute_start", r"\A"),
            ("absolute_end", r"\z"),
        )
    )
    return tuple(result)


PATTERN_FORMS: tuple[PatternForm, ...] = pattern_forms()
STRUCTURES: tuple[str, ...] = (
    "captures",
    "noncaptures",
    "alternatives",
    "flags",
    "nested",
)
REPETITIONS: tuple[tuple[str, str, int], ...] = (
    ("optional", "?", 1),
    ("star", "*", 1),
    ("plus", "+", 2),
    ("bounded", "{1,3}", 3),
    ("unbounded", "{2,}", 3),
    ("lazy", "{1,3}?", 3),
)
MATCH_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("ascii", "^[a-z]+$", "a"),
    ("class", r"^\w+$", "a"),
    ("unicode", r"^\p{L}+$", "a"),
    ("wide", r"^[\x{80}-\x{10ffff}]+$", "\U0001f642"),
    ("hostile", "^(?:a|aa)+$", "a"),
    ("mixed", r"^[\p{L}\d]+$", "a"),
)
REGEX_IDENTITIES: tuple[tuple[str, int], ...] = (
    (("regex_base", 1),)
    + tuple(
        ("regex_atoms_" + form.name, size)
        for form in PATTERN_FORMS
        if not form.anchor
        for size in form.sizes()
    )
    + tuple(
        ("regex_anchor_" + form.name, size)
        for form in PATTERN_FORMS
        if form.anchor
        for size in (64, 128, 256, 512)
    )
    + tuple(
        ("regex_structure_" + name, size)
        for name in STRUCTURES
        for size in ((4, 8, 16, 32) if name == "nested" else (32, 64, 128, 256))
    )
    + tuple(
        ("regex_repetition_" + name, size)
        for name, _, _ in REPETITIONS
        for size in (32, 64, 128, 256)
    )
    + tuple(
        ("regex_match_" + name + "_" + outcome, size)
        for name, _, _ in MATCH_PATTERNS
        for outcome in ("late_failure", "success")
        for size in (256, 512, 1024, 2048)
    )
)
REGEX_MEMORY_IDENTITIES: tuple[tuple[str, int], ...] = tuple(
    (family, max(size for name, size in REGEX_IDENTITIES if name == family))
    for family in dict.fromkeys(name for name, _ in REGEX_IDENTITIES)
)


def form_by_name(name: str) -> PatternForm:
    for form in PATTERN_FORMS:
        if form.name == name:
            return form
    raise ValueError("undeclared pattern form")


def matches_source(subject: str, pattern: str) -> str:
    return f"{subject}.matches({json.dumps(pattern, ensure_ascii=False)})"


def repeated_matches(subject: str, pattern: str, expected: bool) -> str:
    source = matches_source(subject, pattern)
    values = ",".join(["true" if expected else "false"] * 8)
    return "[" + ",".join([source] * 8) + "]==[" + values + "]"


def regex_case(family: str, size: int) -> CostCase:
    if type(size) is not int or (family, size) not in REGEX_IDENTITIES:
        raise ValueError("undeclared regex case")
    if family == "regex_base":
        return CostCase(
            family,
            size,
            "pattern_base",
            1,
            matches_source("''", ""),
            "true",
            {},
            {},
            True,
            True,
            batch=32,
        )
    if family.startswith(("regex_atoms_", "regex_anchor_")):
        form = form_by_name(
            family.removeprefix("regex_atoms_").removeprefix("regex_anchor_")
        )
        subject = "'aa'" if form.anchor else "''"
        return CostCase(
            family,
            size,
            "pattern_compile",
            size,
            matches_source(subject, form.pattern(size)),
            matches_source(subject, form.flags),
            {},
            {},
            form.anchor,
            True,
            batch=1,
        )
    if family.startswith("regex_structure_"):
        name = family.removeprefix("regex_structure_")
        pattern = {
            "captures": "()" * size,
            "noncaptures": "(?:)" * size,
            "alternatives": "|" * size,
            "flags": "(?i)" * size,
            "nested": "(" * size + ")" * size,
        }[name]
        return CostCase(
            family,
            size,
            "pattern_syntax",
            len(pattern),
            matches_source("''", pattern),
            matches_source("''", ""),
            {},
            {},
            True,
            True,
            batch=1,
        )
    if family.startswith("regex_repetition_"):
        name = family.removeprefix("regex_repetition_")
        _, operator, factor = next(item for item in REPETITIONS if item[0] == name)
        pattern = ("a" + operator) * size + "b"
        control = "a" * (factor * size) + "b"
        return CostCase(
            family,
            size,
            "pattern_repetition",
            size,
            matches_source("''", pattern),
            matches_source("''", control),
            {},
            {},
            False,
            False,
            batch=1,
        )
    if family.startswith("regex_match_"):
        successful = family.endswith("_success")
        name = (
            family.removeprefix("regex_match_")
            .removesuffix("_success")
            .removesuffix("_late_failure")
        )
        _, pattern, character = next(item for item in MATCH_PATTERNS if item[0] == name)
        if successful:
            text = character * (size if character == "a" else size // 4)
        else:
            text = (
                "a" * (size - 1) + "!"
                if character == "a"
                else character * (size // 4 - 1) + "aaaa"
            )
        return CostCase(
            family,
            size,
            "pattern_match",
            8 * size,
            repeated_matches("text", pattern, successful),
            repeated_matches("''", pattern, False),
            {"text": text},
            {"text": STRING},
            True,
            True,
            batch=1,
            declared_bounds={"text": ValueBound(size + 2, 1, 0, size)},
        )
    raise ValueError("undeclared regex case")


def size_limit_error(pattern: str) -> str:
    return (
        f"Function 'matches' error: '{pattern}' not a valid regex:\n"
        f"Compiled regex exceeds size limit of {COMPILED_LIMIT} bytes.. "
        "Check function arguments and their types."
    )


class BoundaryAttempt(TypedDict):
    count: int
    status: str
    source_sha256: str
    result_sha256: str | None
    error: str | None


class BoundaryReport(TypedDict):
    schema: int
    form: dict[str, object]
    compiled_limit: int
    upper_repetitions: int
    attempts: list[BoundaryAttempt]
    resources: dict[str, object]
    completion: str


class BoundaryProbe(EvaluationThread):
    def __init__(self, form: PatternForm) -> None:
        self.form: PatternForm = form
        self.attempts: list[BoundaryAttempt] = []
        self.completed: bool = False
        super().__init__(load_binding())

    def attempt(self, count: int) -> bool:
        pattern = self.form.pattern(count, repetition=True)
        subject = "'aa'" if self.form.anchor else "''"
        source = matches_source(subject, pattern)
        binding = self.machine.binding
        program = Program(binding.native_compile(source), source)
        try:
            result = binding.execute(program, {})
        except RuntimeError as error:
            if str(error) != size_limit_error(pattern):
                raise
            self.attempts.append(
                BoundaryAttempt(
                    count=count,
                    status="refused",
                    source_sha256=digest(source),
                    result_sha256=None,
                    error=str(error),
                )
            )
            return False
        assert type(result) is bool
        assert result is self.form.anchor
        self.attempts.append(
            BoundaryAttempt(
                count=count,
                status="accepted",
                source_sha256=digest(source),
                result_sha256=digest(result),
                error=None,
            )
        )
        return True

    def _run(self) -> None:
        assert self.attempt(1)
        lower = 1
        upper = UPPER_REPETITIONS
        if not self.attempt(upper):
            while upper - lower > 1:
                assert len(self.attempts) < 22
                middle = (lower + upper) // 2
                if self.attempt(middle):
                    lower = middle
                else:
                    upper = middle
        self.completed = True


def run_boundary(name: str) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    form = form_by_name(name)
    started = time.perf_counter_ns()
    descriptors = len(os.listdir("/dev/fd"))
    previous = threading.stack_size()
    probe = BoundaryProbe(form)
    probe.worker.join(9)
    assert not probe.worker.is_alive()
    assert probe.completed
    assert previous == threading.stack_size()
    workers = sum(t.name == "kora-cel-evaluator" for t in threading.enumerate())
    assert workers == 0
    report = BoundaryReport(
        schema=1,
        form=form.metadata(),
        compiled_limit=COMPILED_LIMIT,
        upper_repetitions=UPPER_REPETITIONS,
        attempts=probe.attempts,
        resources={
            "pid": os.getpid(),
            "elapsed_ns": time.perf_counter_ns() - started,
            "descriptors_before": descriptors,
            "descriptors_after": len(os.listdir("/dev/fd")),
            "persistent_workers": workers,
            "stack_restored": True,
        },
        completion="cel-regex-boundary-complete",
    )
    sys.stdout.write(json.dumps(report))


def boundary_limits(attempts: Sequence[BoundaryAttempt]) -> tuple[int, int | None]:
    if not 2 <= len(attempts) <= 22:
        raise ValueError("incomplete regex boundary")
    lower = 1
    upper = UPPER_REPETITIONS
    expected = 1
    for index, attempt in enumerate(attempts):
        if index > 1 and upper - lower == 1:
            raise ValueError("regex boundary contains a probe after convergence")
        count = attempt["count"]
        status = attempt["status"]
        if (
            type(count) is not int
            or count != expected
            or status not in ("accepted", "refused")
        ):
            raise ValueError("regex boundary trace changed")
        if index == 0:
            if status != "accepted":
                raise ValueError("regex boundary refuses its first instance")
            expected = UPPER_REPETITIONS
            continue
        if index == 1 and status == "accepted":
            if len(attempts) != 2:
                raise ValueError("censored regex boundary has extra attempts")
            return UPPER_REPETITIONS, None
        if status == "accepted":
            lower = count
        else:
            upper = count
        expected = (lower + upper) // 2
    if upper != lower + 1:
        raise ValueError("regex boundary has no adjacent accepted/refused pair")
    return lower, upper


def validate_boundary(content: bytes, name: str) -> BoundaryReport:
    from tests.measurements.cel_cost_calibration import (
        mapping,
        reject_constant,
        sequence,
        unique_mapping,
    )

    raw: object = json.loads(
        content, object_pairs_hook=unique_mapping, parse_constant=reject_constant
    )
    value = mapping(raw)
    form = form_by_name(name)
    expected: dict[str, object] = {
        "schema": 1,
        "form": form.metadata(),
        "compiled_limit": COMPILED_LIMIT,
        "upper_repetitions": UPPER_REPETITIONS,
        "completion": "cel-regex-boundary-complete",
    }
    if set(value) != set(expected) | {"attempts", "resources"} or any(
        json.dumps(value[key], sort_keys=True) != json.dumps(item, sort_keys=True)
        for key, item in expected.items()
    ):
        raise ValueError("regex boundary identity changed")
    attempts: list[BoundaryAttempt] = []
    for entry in sequence(value["attempts"]):
        item = mapping(entry)
        if (
            set(item) != {"count", "status", "source_sha256", "result_sha256", "error"}
            or type(item["count"]) is not int
        ):
            raise ValueError("invalid regex boundary attempt")
        attempt = cast(BoundaryAttempt, item)
        count = attempt["count"]
        if not 1 <= count <= UPPER_REPETITIONS:
            raise ValueError("invalid regex boundary repetition count")
        pattern = form.pattern(count, repetition=True)
        source = matches_source("'aa'" if form.anchor else "''", pattern)
        if attempt["source_sha256"] != digest(source):
            raise ValueError("regex boundary source changed")
        if attempt["status"] == "accepted":
            if attempt["error"] is not None or attempt["result_sha256"] != digest(
                form.anchor
            ):
                raise ValueError(
                    "regex boundary survivor did not return its exact result"
                )
        elif attempt["status"] == "refused":
            if (
                attempt["error"] != size_limit_error(pattern)
                or attempt["result_sha256"] is not None
            ):
                raise ValueError("unexpected regex boundary error")
        else:
            raise ValueError("unknown regex boundary status")
        attempts.append(attempt)
    boundary_limits(attempts)
    resources = mapping(value["resources"])
    if set(resources) != {
        "pid",
        "elapsed_ns",
        "descriptors_before",
        "descriptors_after",
        "persistent_workers",
        "stack_restored",
    }:
        raise ValueError("incomplete regex boundary resource counters")
    if any(
        type(resources[field]) is not int or cast(int, resources[field]) <= 0
        for field in ("pid", "elapsed_ns", "descriptors_before", "descriptors_after")
    ):
        raise ValueError("invalid regex boundary process counter")
    if (
        resources["persistent_workers"] != 0
        or type(resources["persistent_workers"]) is not int
        or resources["stack_restored"] is not True
        or resources["descriptors_before"] != resources["descriptors_after"]
        or cast(int, resources["elapsed_ns"]) >= 10_000_000_000
    ):
        raise ValueError("regex boundary resources were not released")
    return cast(BoundaryReport, value)
