import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlencode, urlsplit

SERVER = "https://github.com"
GITHUB_API = "https://api.github.com"
LOOPBACK = "127.0.0.1"
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


@dataclass(frozen=True)
class OpenIssue:
    number: int
    title: str


class Tracker(Protocol):
    def open_issues(self) -> tuple[OpenIssue, ...]: ...

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
        "master. Its tests or measurements run once a day on master instead of on "
        "every pull request. This open issue tracks the failure until the lane "
        "passes again.\n\n"
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
    listed = tracker.open_issues()
    actions: list[str] = []
    for failure in failures(environment):
        title = issue_title(failure.lane)
        existing = [issue.number for issue in listed if issue.title == title]
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
    if len(listed) > PAGE_SIZE or not all(isinstance(entry, dict) for entry in listed):
        raise ReportFailure("GitHub returned an unexpected issue listing")
    return [cast(dict[str, object], entry) for entry in listed]


def reporter_issue(entry: dict[str, object]) -> OpenIssue | None:
    author = entry.get("user")
    number = entry.get("number")
    title = entry.get("title")
    if (
        "pull_request" in entry
        or not isinstance(author, dict)
        or cast(dict[str, object], author).get("login") != REPORTER
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(title, str)
    ):
        return None
    return OpenIssue(number, title)


def issue_api(base: str) -> tuple[bool, str]:
    location = urlsplit(base)
    if base == GITHUB_API:
        return True, location.netloc
    if (
        location.scheme == "http"
        and location.hostname == LOOPBACK
        and location.port is not None
        and base == f"http://{LOOPBACK}:{location.port}"
    ):
        return False, location.netloc
    raise ReportFailure("the issue API must be GitHub or a loopback test server")


class GitHubTracker:
    def __init__(self, repository: str, token: str, api: str = GITHUB_API) -> None:
        self.secure, self.host = issue_api(api)
        self.repository = repository
        self.token = token

    def request(self, method: str, target: str, payload: object) -> object:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "kora-daily-lanes",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        path = f"repos/{self.repository}/{target}"
        if self.secure:
            request = urllib.request.Request(
                f"https://{self.host}/{path}", data=data, method=method, headers=headers
            )
        else:
            request = urllib.request.Request(
                f"http://{self.host}/{path}", data=data, method=method, headers=headers
            )
        try:
            reply = urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as refusal:
            refusal.close()
            raise
        with reply:
            encoded: bytes = reply.read(MAX_RESPONSE + 1)
        if len(encoded) > MAX_RESPONSE:
            raise ReportFailure("GitHub returned an oversized response")
        result: object = json.loads(encoded)
        return result

    def open_issues(self) -> tuple[OpenIssue, ...]:
        found: list[OpenIssue] = []
        for page in range(1, PAGES + 1):
            query = urlencode(
                {
                    "state": "open",
                    "creator": REPORTER,
                    "per_page": PAGE_SIZE,
                    "page": page,
                }
            )
            listed = entries(self.request("GET", f"issues?{query}", None))
            found.extend(
                issue
                for entry in listed
                if (issue := reporter_issue(entry)) is not None
            )
            if len(listed) < PAGE_SIZE:
                return tuple(found)
        raise ReportFailure(
            f"open issues created by {REPORTER} exceed the bounded listing of "
            f"{PAGES * PAGE_SIZE} issues"
        )

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
