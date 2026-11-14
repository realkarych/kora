import dataclasses
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Literal, cast

API_HOST = "api.github.com"
LOG_HOST = re.compile(r"productionresultssa[0-9]{1,3}\.blob\.core\.windows\.net")
BODY_LIMIT = 4 * 1024 * 1024
PAGE_SIZE = 100
LISTING_LIMIT = 1000
LEDGER_WINDOW = datetime.timedelta(days=14)
CONFIRM_SECONDS = 60
SHA = re.compile(r"[0-9a-f]{40}")
WORKFLOW_NAME = "CI"
WORKFLOW_PATH = ".github/workflows/ci.yml"
DEFAULT_BRANCH = "master"
NOT_ACQUIRED = "The job was not acquired by Runner"
HOSTED_LABELS = frozenset({"ubuntu-24.04", "ubuntu-latest", "macos-15"})
GATE_INPUTS = (
    ".github/workflows/ci.yml",
    "scripts/ci/require_outcomes.sh",
    "tests/scaffold_evidence.py",
    "scripts/ci/lane_plan.py",
)
ROOT_JOBS = frozenset(
    {
        "Classify verification scope",
        "Changed test repetition",
        "Markdown lint",
        "Links and anchors",
        "Mermaid diagrams",
        "Example graph documents",
        "GitHub templates",
    }
)
CONTINUE_ON_ERROR = frozenset(
    {
        "Local runtime diagnostics (ubuntu-24.04, Python 3.11.17)",
        "Local runtime diagnostics (ubuntu-24.04, Python 3.15.0)",
        "Local runtime diagnostics (macos-15, Python 3.11.17)",
    }
)
GROUPS: Mapping[str, Callable[[str], bool]] = {
    "change-scope": lambda name: name == "Classify verification scope",
    "changed-test-repetition": lambda name: name == "Changed test repetition",
    "scaffold-plan": lambda name: name == "Plan scaffold lanes",
    "scaffold-lanes": lambda name: name.startswith("Scaffold tests ("),
    "scaffold-evidence": lambda name: name == "Scaffold evidence",
    "cel-producer": lambda name: name == "CEL lifetime and representative cost",
}
TIMINGS_STEP = "Report planned and recorded lane times"
VERIFY_STEP = "Verify the planned lanes and complete collection coverage"
EVIDENCE_LINES = {
    TIMINGS_STEP: "scaffold collection is incomplete",
    VERIFY_STEP: "scaffold evidence does not cover exactly the planned lanes",
}
CEL_PRODUCER_STEPS = frozenset(
    {
        "Run actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0",
        "Verify exact CEL evidence and both source receipts",
    }
)

Verdict = Literal["rerun", "no_rerun", "skip"]
JobClass = Literal["no_runner", "dependent", "kora"]


@dataclass(frozen=True)
class Join:
    names: frozenset[str]
    needs: tuple[str, ...]
    checks: frozenset[str]


JOINS = (
    Join(
        frozenset({"Scaffold evidence"}),
        ("change-scope", "scaffold-plan", "scaffold-lanes"),
        frozenset({"Require all test lanes and collection evidence"}),
    ),
    Join(
        frozenset(
            {
                "Scaffold (ubuntu-24.04, Python 3.11.17)",
                "Scaffold (ubuntu-24.04, Python 3.15.0)",
                "Scaffold (macos-15, Python 3.11.17)",
            }
        ),
        ("scaffold-evidence", "changed-test-repetition"),
        frozenset({"Require verified scaffold evidence"}),
    ),
    Join(
        frozenset({"CEL evaluator lifetime"}),
        ("change-scope", "scaffold-evidence", "cel-producer"),
        frozenset({"Require every measurement and artifact"}),
    ),
    Join(
        frozenset({"Console browser checks"}),
        ("change-scope",),
        frozenset({"Require browser verification or approved editorial scope"}),
    ),
)


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    name: str
    conclusion: str | None


@dataclass(frozen=True)
class Job:
    id: int
    name: str
    conclusion: str | None
    runner_name: str
    runner_id: int
    labels: tuple[str, ...]
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Run:
    id: int
    run_attempt: int
    conclusion: str | None
    event: str
    head_branch: str
    head_sha: str
    tree_id: str
    head_repository_id: int
    repository_id: int
    created_at: datetime.datetime
    name: str
    path: str
    pull_requests: tuple[int, ...]


@dataclass(frozen=True)
class Evidence:
    run: Run
    jobs: tuple[Job, ...]
    annotations: Mapping[int, tuple[str, ...]]
    logs: Mapping[int, str]
    history: tuple[Run, ...]
    ledger_complete: bool
    gate_inputs_unchanged: bool


@dataclass(frozen=True)
class Classified:
    job: Job
    kind: JobClass


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    jobs: tuple[Classified, ...] = ()


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RecoveryError("expected an object")
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise RecoveryError("expected string keys")
        result[key] = item
    return result


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise RecoveryError("expected a list")
    return list(cast(list[object], value))


def text(value: object) -> str:
    if not isinstance(value, str):
        raise RecoveryError("expected a string")
    return value


def optional_text(value: object) -> str | None:
    return None if value is None else text(value)


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecoveryError("expected an integer")
    return value


def positive(value: object) -> int:
    number = integer(value)
    if number <= 0:
        raise RecoveryError("expected a positive integer")
    return number


def sha(value: object) -> str:
    candidate = text(value)
    if SHA.fullmatch(candidate) is None:
        raise RecoveryError("expected a commit or tree SHA")
    return candidate


def timestamp(value: object) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RecoveryError("expected a timezone-aware timestamp")
    return parsed


def parse_run(value: object) -> Run:
    data = mapping(value)
    head_commit = mapping(data["head_commit"])
    return Run(
        id=positive(data["id"]),
        run_attempt=positive(data["run_attempt"]),
        conclusion=optional_text(data["conclusion"]),
        event=text(data["event"]),
        head_branch=text(data["head_branch"]),
        head_sha=sha(data["head_sha"]),
        tree_id=sha(head_commit["tree_id"]),
        head_repository_id=positive(mapping(data["head_repository"])["id"]),
        repository_id=positive(mapping(data["repository"])["id"]),
        created_at=timestamp(data["created_at"]),
        name=text(data["name"]),
        path=text(data["path"]),
        pull_requests=tuple(
            positive(mapping(item)["number"])
            for item in sequence(data["pull_requests"])
        ),
    )


def parse_job(value: object) -> Job:
    data = mapping(value)
    runner_id = data.get("runner_id")
    return Job(
        id=positive(data["id"]),
        name=text(data["name"]),
        conclusion=optional_text(data["conclusion"]),
        runner_name=optional_text(data.get("runner_name")) or "",
        runner_id=0 if runner_id is None else integer(runner_id),
        labels=tuple(text(label) for label in sequence(data["labels"])),
        steps=tuple(
            Step(
                name=text(mapping(step)["name"]),
                conclusion=optional_text(mapping(step)["conclusion"]),
            )
            for step in sequence(data["steps"])
        ),
    )


def failed(conclusion: str | None) -> bool:
    return conclusion not in {"success", "skipped", "neutral"}


def step_failed(conclusion: str | None) -> bool:
    return conclusion in {"failure", "cancelled", "timed_out"}


def no_runner(job: Job, annotations: Mapping[int, tuple[str, ...]]) -> bool:
    return (
        job.steps == ()
        and job.runner_name == ""
        and job.runner_id == 0
        and job.conclusion in {"cancelled", "failure"}
        and len(job.labels) == 1
        and job.labels[0] in HOSTED_LABELS
        and any(
            message.startswith(NOT_ACQUIRED) for message in annotations.get(job.id, ())
        )
    )


def members(jobs: Iterable[Job], group: str) -> list[Job]:
    return [job for job in jobs if GROUPS[group](job.name)]


def group_state(
    jobs: tuple[Job, ...], group: str, kinds: Mapping[int, JobClass]
) -> str:
    present = members(jobs, group)
    if not present:
        return "missing"
    pending = [job for job in present if failed(job.conclusion)]
    if not pending:
        return "succeeded"
    if all(kinds.get(job.id) in {"no_runner", "dependent"} for job in pending):
        return "infrastructure"
    return "blocking"


def allowed_steps(
    join: Join, jobs: tuple[Job, ...], kinds: Mapping[int, JobClass], log: str
) -> frozenset[str]:
    allowed = set(join.checks)
    if "Scaffold evidence" in join.names:
        lanes = group_state(jobs, "scaffold-lanes", kinds)
        if lanes in {"missing", "infrastructure"}:
            allowed.update(step for step, line in EVIDENCE_LINES.items() if line in log)
    if (
        "CEL evaluator lifetime" in join.names
        and group_state(jobs, "cel-producer", kinds) == "missing"
    ):
        allowed.update(CEL_PRODUCER_STEPS)
    if (
        "Console browser checks" in join.names
        and group_state(jobs, "change-scope", kinds) != "missing"
    ):
        allowed.clear()
    return frozenset(allowed)


def dependent(
    job: Job,
    join: Join,
    jobs: tuple[Job, ...],
    kinds: Mapping[int, JobClass],
    logs: Mapping[int, str],
) -> bool:
    failures = [step.name for step in job.steps if step_failed(step.conclusion)]
    if not failures:
        return False
    if not set(failures) <= allowed_steps(join, jobs, kinds, logs.get(job.id, "")):
        return False
    states = [group_state(jobs, group, kinds) for group in join.needs]
    return "blocking" not in states and any(
        state in {"missing", "infrastructure"} for state in states
    )


def classify_jobs(evidence: Evidence) -> tuple[Classified, ...]:
    jobs = evidence.jobs
    kinds: dict[int, JobClass] = {}
    candidates = [
        job
        for job in jobs
        if failed(job.conclusion) and job.name not in CONTINUE_ON_ERROR
    ]
    for job in candidates:
        if no_runner(job, evidence.annotations):
            kinds[job.id] = "no_runner"
    for join in JOINS:
        for job in candidates:
            if job.id not in kinds and job.name in join.names:
                if dependent(job, join, jobs, kinds, evidence.logs):
                    kinds[job.id] = "dependent"
    return tuple(Classified(job, kinds.get(job.id, "kora")) for job in candidates)


def run_skip_reason(run: Run) -> str | None:
    if run.name != WORKFLOW_NAME or run.path != WORKFLOW_PATH:
        return "workflow"
    if run.run_attempt != 1:
        return "attempt"
    if run.conclusion != "failure":
        return "conclusion"
    if run.event not in {"pull_request", "push"}:
        return "event"
    if run.event == "push" and run.head_branch != DEFAULT_BRANCH:
        return "branch"
    if run.head_repository_id != run.repository_id:
        return "fork"
    return None


def ledger_skip_reason(evidence: Evidence) -> str | None:
    run = evidence.run
    if not evidence.ledger_complete:
        return "ledger_incomplete"
    others = [
        other
        for other in evidence.history
        if other.event == run.event
        and other.head_branch == run.head_branch
        and other.head_repository_id == run.repository_id
    ]
    if any(other.id != run.id and other.tree_id == run.tree_id for other in others):
        return "limit_used"
    if any(
        other.head_sha == run.head_sha and other.run_attempt > 1 for other in others
    ):
        return "limit_used"
    if run.event == "pull_request" and any(
        other.created_at > run.created_at and other.head_sha != run.head_sha
        for other in others
    ):
        return "superseded"
    return None


def classify(evidence: Evidence) -> Decision:
    reason = run_skip_reason(evidence.run)
    if reason is not None:
        return Decision("skip", reason)
    if not evidence.gate_inputs_unchanged:
        return Decision("skip", "workflow_changed")
    reason = ledger_skip_reason(evidence)
    if reason is not None:
        return Decision("skip", reason)
    if not evidence.jobs:
        return Decision("skip", "no_jobs")
    classified = classify_jobs(evidence)
    roots_absent = not any(job.name in ROOT_JOBS for job in evidence.jobs)
    kora = [item for item in classified if item.kind == "kora"]
    if kora:
        return Decision("no_rerun", kora[0].job.name, classified)
    unacquired = any(item.kind == "no_runner" for item in classified)
    joins_only = bool(classified) and all(
        item.kind == "dependent" for item in classified
    )
    if unacquired or (roots_absent and joins_only):
        return Decision("rerun", "infrastructure", classified)
    return Decision("no_rerun", "no_infrastructure_cause", classified)


class Client:
    def __init__(self, repository: str, token: str, trigger: int) -> None:
        if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository) is None:
            raise RecoveryError("unexpected repository name")
        self.repository = repository
        self.token = token
        self.trigger = positive(trigger)
        self.opener = urllib.request.OpenerDirector()
        self.opener.add_handler(urllib.request.HTTPSHandler())

    def url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        encoded = "" if not query else "?" + urllib.parse.urlencode(query)
        return f"https://{API_HOST}/repos/{self.repository}/{path}{encoded}"

    def request(self, url: str, method: str = "GET") -> urllib.request.Request:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != API_HOST:
            raise RecoveryError("refused a request outside the API host")
        rerun = self.url(f"actions/runs/{self.trigger}/rerun")
        if method != "GET" and (method, url) != ("POST", rerun):
            raise RecoveryError("refused a write other than the triggering rerun")
        request = urllib.request.Request(f"https://{url[8:]}", method=method)
        request.add_unredirected_header("Authorization", "Bearer " + self.token)
        request.add_unredirected_header("Accept", "application/vnd.github+json")
        request.add_unredirected_header("X-GitHub-Api-Version", "2022-11-28")
        return request

    def send(self, request: urllib.request.Request) -> tuple[int, bytes, str]:
        response = self.opener.open(request, timeout=30)
        if not isinstance(response, HTTPResponse):
            raise RecoveryError("unexpected response type")
        with response:
            body = response.read(BODY_LIMIT + 1)
            location = response.headers.get("Location") or ""
            status = response.status
        if len(body) > BODY_LIMIT:
            raise RecoveryError("response exceeds its size limit")
        return status, body, location

    def get_json(self, path: str, query: Mapping[str, str] | None = None) -> object:
        status, body, _ = self.send(self.request(self.url(path, query)))
        if status != 200:
            raise RecoveryError(f"GET {path} returned {status}")
        return json.loads(body)

    def run(self, run_id: int) -> Run:
        return parse_run(self.get_json(f"actions/runs/{positive(run_id)}"))

    def jobs(self, run_id: int) -> tuple[Job, ...]:
        found: list[Job] = []
        page = 1
        while True:
            data = mapping(
                self.get_json(
                    f"actions/runs/{positive(run_id)}/jobs",
                    {"per_page": str(PAGE_SIZE), "page": str(page), "filter": "latest"},
                )
            )
            batch = [parse_job(item) for item in sequence(data["jobs"])]
            found.extend(batch)
            if len(found) >= LISTING_LIMIT:
                raise RecoveryError("the job listing exceeds its limit")
            if len(batch) < PAGE_SIZE:
                return tuple(found)
            page += 1

    def annotations(self, job_id: int) -> tuple[str, ...]:
        items = sequence(
            self.get_json(
                f"check-runs/{positive(job_id)}/annotations",
                {"per_page": str(PAGE_SIZE)},
            )
        )
        return tuple(text(mapping(item)["message"]) for item in items)

    def log(self, job_id: int) -> str:
        status, body, location = self.send(
            self.request(self.url(f"actions/jobs/{positive(job_id)}/logs"))
        )
        if status in {301, 302, 307, 308}:
            status, body, _ = self.send(self.log_request(location))
        if status != 200:
            raise RecoveryError(f"log download returned {status}")
        return body.decode("utf-8", "replace")

    def log_request(self, location: str) -> urllib.request.Request:
        parts = urllib.parse.urlsplit(location)
        if parts.scheme != "https" or LOG_HOST.fullmatch(parts.hostname or "") is None:
            raise RecoveryError("refused a log redirect outside the allowed hosts")
        return urllib.request.Request(f"https://{location[8:]}")

    def history(self, run: Run) -> tuple[tuple[Run, ...], bool]:
        since = (run.created_at - LEDGER_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        found: list[Run] = []
        page = 1
        while True:
            data = mapping(
                self.get_json(
                    "actions/workflows/ci.yml/runs",
                    {
                        "event": run.event,
                        "branch": run.head_branch,
                        "created": f">={since}",
                        "per_page": str(PAGE_SIZE),
                        "page": str(page),
                    },
                )
            )
            batch = [parse_run(item) for item in sequence(data["workflow_runs"])]
            found.extend(batch)
            if len(found) >= LISTING_LIMIT:
                return tuple(found), False
            if len(batch) < PAGE_SIZE:
                return tuple(found), True
            page += 1

    def blob(self, path: str, ref: str) -> str:
        if path not in GATE_INPUTS:
            raise RecoveryError("refused an unlisted repository path")
        data = mapping(self.get_json(f"contents/{path}", {"ref": sha(ref)}))
        return sha(data["sha"])

    def rerun(self) -> bool:
        status, _, _ = self.send(
            self.request(self.url(f"actions/runs/{self.trigger}/rerun"), "POST")
        )
        if status == 201:
            return True
        if status in {403, 409} and self.run(self.trigger).run_attempt > 1:
            return False
        raise RecoveryError(f"rerun request returned {status}")


def collect(client: Client, run: Run, checkout: str) -> Evidence:
    jobs = client.jobs(run.id)
    annotations = {
        job.id: client.annotations(job.id)
        for job in jobs
        if failed(job.conclusion) and job.steps == () and job.runner_id == 0
    }
    logs = {
        job.id: client.log(job.id)
        for job in jobs
        if job.name == "Scaffold evidence"
        and any(
            step.name in EVIDENCE_LINES and step_failed(step.conclusion)
            for step in job.steps
        )
    }
    history, complete = client.history(run)
    unchanged = all(
        client.blob(path, run.head_sha) == client.blob(path, checkout)
        for path in GATE_INPUTS
    )
    return Evidence(run, jobs, annotations, logs, history, complete, unchanged)


def emit(record: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def cell(value: object) -> str:
    raw = str(value)[:200]
    return (
        "`"
        + raw.replace("`", "'").replace("|", "/").replace("\n", " ").replace("\r", " ")
        + "`"
    )


def table_lines(rows: Sequence[Mapping[str, object]]) -> list[str]:
    if not rows:
        return []
    headers = list(rows[0])
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *("| " + " | ".join(cell(row[key]) for key in headers) + " |" for row in rows),
        "",
    ]


def job_evidence(job: Job, evidence: Evidence | None) -> str:
    if evidence is None:
        return ""
    log = evidence.logs.get(job.id, "")
    matched = [
        *(
            message
            for message in evidence.annotations.get(job.id, ())
            if message.startswith(NOT_ACQUIRED)
        ),
        *(line for line in EVIDENCE_LINES.values() if line in log),
    ]
    return "; ".join(matched)


def job_record(item: Classified, evidence: Evidence | None) -> dict[str, object]:
    return {
        "job_id": item.job.id,
        "job": item.job.name,
        "conclusion": item.job.conclusion,
        "steps": len(item.job.steps),
        "runner_name": item.job.runner_name,
        "labels": list(item.job.labels),
        "class": item.kind,
        "evidence": job_evidence(item.job, evidence),
    }


def run_record(
    run: Run, decision: Decision, evidence: Evidence | None, attempt: int | None
) -> dict[str, object]:
    return {
        "run_id": run.id,
        "run_attempt": run.run_attempt,
        "event": run.event,
        "pull_requests": list(run.pull_requests),
        "head_sha": run.head_sha,
        "tree_id": run.tree_id,
        "gate_inputs_unchanged": None
        if evidence is None
        else evidence.gate_inputs_unchanged,
        "ledger": None
        if evidence is None
        else ledger_skip_reason(evidence) or "available",
        "verdict": decision.verdict,
        "reason": decision.reason,
        "rerun_attempt": attempt,
    }


def confirm(client: Client, run_id: int, clock: Callable[[], float]) -> int:
    deadline = clock() + CONFIRM_SECONDS
    while True:
        attempt = client.run(run_id).run_attempt
        if attempt > 1:
            return attempt
        if clock() >= deadline:
            raise RecoveryError("the rerun attempt did not appear in time")
        time.sleep(5)


def main() -> int:
    with open(os.environ["GITHUB_EVENT_PATH"], "rb") as stream:
        event = mapping(json.loads(stream.read(BODY_LIMIT)))
    run_id = positive(mapping(event["workflow_run"])["id"])
    checkout = sha(os.environ["GITHUB_SHA"])
    client = Client(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], run_id)
    run = client.run(run_id)
    early = run_skip_reason(run)
    evidence: Evidence | None = None
    if early is None:
        evidence = collect(client, run, checkout)
        decision = classify(evidence)
    else:
        decision = Decision("skip", early)
    jobs = [job_record(item, evidence) for item in decision.jobs]
    for record in jobs:
        emit(record)
    attempt: int | None = None
    if decision.verdict == "rerun":
        if client.rerun():
            attempt = confirm(client, run.id, time.monotonic)
        else:
            decision = dataclasses.replace(
                decision, verdict="skip", reason="limit_used"
            )
    summary = run_record(run, decision, evidence, attempt)
    emit(summary)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write("\n".join([*table_lines([summary]), *table_lines(jobs)]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
