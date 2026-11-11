import json
import os
import re
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

SERVER = "https://github.com"
REPORTER = "github-actions[bot]"
RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
REVISION = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[1-9][0-9]{0,19}")
PAGE_SIZE = 100
PAGES = 10
MAX_RESPONSE = 16777216


@dataclass(frozen=True)
class Lane:
    job: str
    variable: str
    title: str
    proof: bool


LANES = (
    Lane(
        "resource-measurements",
        "RESOURCE",
        "Logging, metadata and provider measurements",
        True,
    ),
    Lane(
        "provider-protocol-measurements",
        "PROVIDER_PROTOCOL",
        "Provider and protocol measurements",
        True,
    ),
    Lane("pricing-measurements", "PRICING", "Pricing database lifetime", True),
    Lane(
        "git-publication-measurements",
        "GIT_PUBLICATION",
        "Git publication measurements",
        True,
    ),
    Lane("ir-evidence", "IR_EVIDENCE", "IR model measurements", False),
    Lane("lifetime-tests", "LIFETIME", "Conformance lifetime tests", False),
    Lane(
        "storage-pressure-tests",
        "STORAGE_PRESSURE",
        "macOS storage pressure tests",
        False,
    ),
)


@dataclass(frozen=True)
class Run:
    repository: str
    run_id: str
    attempt: str
    revision: str


@dataclass(frozen=True)
class Failure:
    lane: Lane
    result: str


class Tracker(Protocol):
    def open_issues(self, title: str) -> tuple[int, ...]: ...

    def create(self, title: str, body: str) -> None: ...

    def comment(self, number: int, body: str) -> None: ...


class ReportFailure(ValueError):
    pass


def identity(
    environment: Mapping[str, str], name: str, pattern: re.Pattern[str]
) -> str:
    value = environment.get(name, "")
    if pattern.fullmatch(value) is None:
        raise ReportFailure(f"{name} is not a valid run identity")
    return value


def current_run(environment: Mapping[str, str]) -> Run:
    if environment.get("GITHUB_SERVER_URL") != SERVER:
        raise ReportFailure("GITHUB_SERVER_URL is not a valid run identity")
    return Run(
        identity(environment, "GITHUB_REPOSITORY", REPOSITORY),
        identity(environment, "GITHUB_RUN_ID", IDENTIFIER),
        identity(environment, "GITHUB_RUN_ATTEMPT", IDENTIFIER),
        identity(environment, "GITHUB_SHA", REVISION),
    )


def lane_result(environment: Mapping[str, str], lane: Lane) -> str:
    result = environment.get(f"{lane.variable}_RESULT", "")
    if result not in RESULTS:
        return "missing"
    proven = environment.get(f"{lane.variable}_VERIFIED") == "true"
    if result == "success" and lane.proof and not proven:
        return "unproven"
    return result


def failures(environment: Mapping[str, str]) -> tuple[Failure, ...]:
    results = ((lane, lane_result(environment, lane)) for lane in LANES)
    return tuple(
        Failure(lane, result) for lane, result in results if result != "success"
    )


def issue_title(lane: Lane) -> str:
    return f"Daily master lane failed: {lane.title}"


def occurrence(failure: Failure, run: Run) -> str:
    return (
        f"- Lane: `{failure.lane.job}`\n"
        f"- Result: `{failure.result}`\n"
        f"- Revision: `{run.revision}`\n"
        f"- Run: {SERVER}/{run.repository}/actions/runs/{run.run_id}"
        f"/attempts/{run.attempt}\n"
    )


def issue_body(failure: Failure, run: Run) -> str:
    return (
        "### Impact\n\n"
        "A lane of the scheduled `Daily master lanes` workflow did not pass on "
        "master. Its tests or measurements no longer run on pull requests, so this "
        "issue is the visible record of the failure until the lane passes again.\n\n"
        "### Failed lane\n\n"
        f"{occurrence(failure, run)}\n"
        "### Next step\n\n"
        "Inspect the run, find the cause and fix it through the normal issue and "
        "pull request process. Later failures of this lane add a comment here "
        "while this issue stays open.\n"
    )


def repeated_body(failure: Failure, run: Run) -> str:
    return f"The lane failed again.\n\n{occurrence(failure, run)}"


def report(environment: Mapping[str, str], tracker: Tracker) -> tuple[str, ...]:
    run = current_run(environment)
    actions: list[str] = []
    for failure in failures(environment):
        title = issue_title(failure.lane)
        existing = tracker.open_issues(title)
        if existing:
            tracker.comment(min(existing), repeated_body(failure, run))
            actions.append(f"commented {failure.lane.job}")
        else:
            tracker.create(title, issue_body(failure, run))
            actions.append(f"opened {failure.lane.job}")
    return tuple(actions)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: object,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ReportFailure("GitHub returned an unexpected issue listing")
    listed = cast(list[object], value)
    if not all(isinstance(entry, dict) for entry in listed):
        raise ReportFailure("GitHub returned an unexpected issue listing")
    return [cast(dict[str, object], entry) for entry in listed]


def reporter_issue(entry: dict[str, object], title: str) -> int | None:
    author = entry.get("user")
    number = entry.get("number")
    if (
        "pull_request" in entry
        or entry.get("title") != title
        or not isinstance(author, dict)
        or cast(dict[str, object], author).get("login") != REPORTER
        or not isinstance(number, int)
    ):
        return None
    return number


class GitHubTracker:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    def request(self, method: str, path: str, payload: object) -> object:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "kora-daily-lanes",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=30
        ) as reply:
            encoded: bytes = reply.read(MAX_RESPONSE + 1)
        if len(encoded) > MAX_RESPONSE:
            raise ReportFailure("GitHub returned an oversized response")
        result: object = json.loads(encoded)
        return result

    def open_issues(self, title: str) -> tuple[int, ...]:
        found: list[int] = []
        for page in range(1, PAGES + 1):
            listed = entries(
                self.request(
                    "GET",
                    f"issues?state=open&per_page={PAGE_SIZE}&page={page}",
                    None,
                )
            )
            found.extend(
                number
                for entry in listed
                if (number := reporter_issue(entry, title)) is not None
            )
            if len(listed) < PAGE_SIZE:
                return tuple(found)
        raise ReportFailure("open issues exceed the bounded listing")

    def create(self, title: str, body: str) -> None:
        self.request("POST", "issues", {"title": title, "body": body})

    def comment(self, number: int, body: str) -> None:
        self.request("POST", f"issues/{number}/comments", {"body": body})


def main() -> int:
    environment = dict(os.environ)
    run = current_run(environment)
    token = environment.get("GITHUB_TOKEN", "")
    if not token:
        raise ReportFailure("GITHUB_TOKEN is required to report failed lanes")
    for action in report(environment, GitHubTracker(run.repository, token)):
        sys.stdout.write(f"{action}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
