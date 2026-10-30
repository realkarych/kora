import hashlib
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FILE = 8 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_WORK = 128 * 1024 * 1024
MAX_MUTANTS = 10_000
TOOL = "cosmic-ray==8.7.0"
Outcome = Literal[
    "killed",
    "survived",
    "invalid",
    "test_error",
    "timeout",
    "interrupted",
    "output_limit",
    "workspace_limit",
    "infrastructure_error",
    "pending",
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(Record):
    source: str
    tests: str


SCOPES: tuple[Scope, ...] = (
    Scope(source="src/kora/ir/pricing.py", tests="tests/ir/test_usage_pricing.py"),
    Scope(source="src/kora/cel/paths.py", tests="tests/cel/test_cel_paths.py"),
)


class Mutation(Record):
    module_path: str
    operator_name: str = Field(pattern=r"^core/[A-Za-z0-9_]+$", max_length=128)
    occurrence: int = Field(ge=0, le=MAX_MUTANTS)
    start_pos: tuple[int, int]
    end_pos: tuple[int, int]
    operator_args: dict[str, str] = Field(default_factory=dict, max_length=0)
    definition_name: str | None = Field(default=None, max_length=256)
    fingerprint: str = ""

    def identity(self) -> str:
        value = f"{self.module_path}:{self.operator_name}:{self.occurrence}"
        return self.fingerprint or hashlib.sha256(value.encode()).hexdigest()


class Work(Record):
    job_id: str = Field(max_length=128)
    mutations: tuple[Mutation] = Field(min_length=1, max_length=1)


class Result(Record):
    mutation: Mutation
    outcome: Outcome
    input_digest: str
    tested_revision: str


class Report(Record):
    version: Literal[1] = 1
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    tooling_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    tooling_dirty: bool = False
    retained_workspace: bool = False
    run_id: int = 0
    attempt: int = 0
    event: str = "local"
    status: Literal["partial", "complete", "failed"] = "partial"
    reason: str = "budget"
    cursor: int = Field(default=0, ge=0)
    next_identity: str = ""
    generation: int = Field(default=0, ge=0)
    results: dict[str, Result] = Field(default_factory=dict, max_length=MAX_MUTANTS)
    seconds: float = 0
    tool: Literal["cosmic-ray==8.7.0"] = TOOL


def regular_bytes(path: Path, limit: int = MAX_FILE) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("invalid bounded audit file")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("oversized audit file")
    return value


def digest(root: Path, scope: Scope) -> str:
    paths = sorted(
        path
        for base in ("src", "tests", "scripts/ci")
        for path in (root / base).rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix != ".pyc"
    )
    result = hashlib.sha256(
        f"{TOOL}:{sys.version}:{sys.platform}:{scope.model_dump_json()}".encode()
    )
    config = (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "hatch_build.py",
        ".github/workflows/mutation.yml",
    )
    for path in [*paths, *(root / name for name in config if (root / name).is_file())]:
        result.update(str(path.relative_to(root)).encode())
        result.update(regular_bytes(path))
    return result.hexdigest()


def ordered(work: list[Mutation], previous: Report | None) -> list[Mutation]:
    if not work or previous is None:
        return work
    identities = [item.identity() for item in work]
    cursor = (
        identities.index(previous.next_identity)
        if previous.next_identity in identities
        else previous.cursor % len(work)
    )
    rotated = work[cursor:] + work[:cursor]
    pending = [item for item in work if item.identity() not in previous.results]
    result: dict[str, Mutation] = {}
    for index in range(max(len(pending), len(rotated))):
        for queue in (pending, rotated):
            if index < len(queue):
                item = queue[index]
                result.setdefault(item.identity(), item)
    return list(result.values())
