import json
import threading
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.ci.scheduled_report import (
    LANES,
    GitHubTracker,
    Lane,
    OpenIssue,
    ReportFailure,
    entries,
    report,
    reporter_issue,
)
from tests.support.workflows import job_of, repository_workflows, step_list, table

REVISION = "0123456789abcdef0123456789abcdef01234567"
ISSUE_WRITER = "fixture-issue-writer"
RUN = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "example/kora",
    "GITHUB_RUN_ID": "4321",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SHA": REVISION,
}
LIFETIME_TITLE = "Daily master lane failed: Conformance lifetime tests"
LIFETIME_BODY = (
    "### Impact\n\n"
    "A lane of the scheduled `Daily master lanes` workflow did not pass on master. "
    "Its tests or measurements run once a day on master instead of on every pull "
    "request. This open issue tracks the failure until the lane passes again.\n\n"
    "### Failed lane\n\n"
    "- Lane: `lifetime-tests`\n"
    "- Result: `failure`\n"
    f"- Revision: `{REVISION}`\n"
    "- Run: https://github.com/example/kora/actions/runs/4321/attempts/2\n\n"
    "### Next step\n\n"
    "Inspect the run, find the cause and fix it through the normal issue and pull "
    "request process. Later failures of this lane add a comment here while this "
    "issue stays open.\n"
)
LIFETIME_COMMENT = (
    "The lane failed again.\n\n"
    "- Lane: `lifetime-tests`\n"
    "- Result: `failure`\n"
    f"- Revision: `{REVISION}`\n"
    "- Run: https://github.com/example/kora/actions/runs/4321/attempts/2\n"
)
BOT = {"login": "github-actions[bot]"}
LISTING = (
    "/repos/example/kora/issues?state=open&creator=github-actions%5Bbot%5D"
    "&per_page=100&page={page}"
)


class RecordingTracker:
    def __init__(self, listed: tuple[OpenIssue, ...]) -> None:
        self.listed = listed
        self.listings = 0
        self.created: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []

    def open_issues(self) -> tuple[OpenIssue, ...]:
        self.listings += 1
        return self.listed

    def create(self, title: str, body: str) -> None:
        self.created.append((title, body))

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    location: str = ""


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class IssueApi:
    replies: list[Reply] = field(default_factory=list[Reply])
    received: list[Received] = field(default_factory=list[Received])
    base: str = ""

    def answer(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        self.received.append(
            Received(
                handler.command,
                handler.path,
                {name.lower(): value for name, value in handler.headers.items()},
                handler.rfile.read(length),
            )
        )
        reply = self.replies.pop(0)
        handler.send_response(reply.status)
        if reply.location:
            handler.send_header("Location", reply.location)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(reply.body)))
        handler.end_headers()
        handler.wfile.write(reply.body)


def handler_for(api: IssueApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            api.answer(self)

        def do_POST(self) -> None:
            api.answer(self)

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Handler


def serve() -> Iterator[IssueApi]:
    api = IssueApi()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(api))
    api.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def issue_api() -> Iterator[IssueApi]:
    yield from serve()


@pytest.fixture
def other_host() -> Iterator[IssueApi]:
    yield from serve()


def listing(*issues: dict[str, object]) -> Reply:
    return Reply(200, json.dumps(list(issues)).encode())


def bot_issue(number: int, title: str) -> dict[str, object]:
    return {"number": number, "title": title, "user": BOT}


def passing() -> dict[str, str]:
    values = dict(RUN)
    for lane in LANES:
        values[f"{lane.variable}_RESULT"] = "success"
        if lane.proof:
            values[f"{lane.variable}_VERIFIED"] = "true"
    return values


def lane_named(job: str) -> Lane:
    return next(lane for lane in LANES if lane.job == job)


def expected_headers(received: Received) -> dict[str, str]:
    return {
        name: received.headers[name]
        for name in ("accept", "authorization", "user-agent", "x-github-api-version")
    }


GITHUB_HEADERS = {
    "accept": "application/vnd.github+json",
    "authorization": f"Bearer {ISSUE_WRITER}",
    "user-agent": "kora-daily-lanes",
    "x-github-api-version": "2022-11-28",
}


def test_a_failing_lane_opens_one_issue_from_the_fixed_template() -> None:
    tracker = RecordingTracker((OpenIssue(4, "Another issue"),))
    environment = {**passing(), "LIFETIME_RESULT": "failure"}

    actions = report(environment, tracker)

    assert actions == ("opened lifetime-tests",)
    assert tracker.listings == 1
    assert tracker.created == [(LIFETIME_TITLE, LIFETIME_BODY)]
    assert tracker.comments == []


def test_a_repeated_failure_comments_on_the_oldest_matching_issue() -> None:
    listed = (
        OpenIssue(88, LIFETIME_TITLE),
        OpenIssue(12, "Daily master lane failed: IR model measurements"),
        OpenIssue(31, LIFETIME_TITLE),
    )
    tracker = RecordingTracker(listed)
    environment = {**passing(), "LIFETIME_RESULT": "failure"}

    actions = report(environment, tracker)

    assert actions == ("commented lifetime-tests",)
    assert tracker.created == []
    assert tracker.comments == [(31, LIFETIME_COMMENT)]


def test_passing_lanes_list_open_issues_without_writing() -> None:
    tracker = RecordingTracker((OpenIssue(31, LIFETIME_TITLE),))

    actions = report(passing(), tracker)

    assert actions == ()
    assert tracker.listings == 1
    assert tracker.created == []
    assert tracker.comments == []


@pytest.mark.parametrize("lane", LANES, ids=[lane.job for lane in LANES])
@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("failure", "failure"),
        ("cancelled", "cancelled"),
        ("skipped", "skipped"),
        ("", "missing"),
        ("failure\n@octocat <!-- hidden -->", "missing"),
    ],
)
def test_every_unsuccessful_lane_opens_its_own_issue(
    lane: Lane, value: str, rendered: str
) -> None:
    tracker = RecordingTracker(())
    environment = {**passing(), f"{lane.variable}_RESULT": value}

    actions = report(environment, tracker)

    assert actions == (f"opened {lane.job}",)
    [(title, body)] = tracker.created
    assert title == f"Daily master lane failed: {lane.title}"
    assert f"- Lane: `{lane.job}`\n- Result: `{rendered}`\n" in body
    assert "@octocat" not in body


@pytest.mark.parametrize(
    "job",
    [
        "resource-measurements",
        "provider-protocol-measurements",
        "pricing-measurements",
        "git-publication-measurements",
    ],
)
@pytest.mark.parametrize("proof", ["", "false"])
def test_a_producer_without_its_proof_fails(job: str, proof: str) -> None:
    lane = lane_named(job)
    tracker = RecordingTracker(())
    environment = {**passing(), f"{lane.variable}_VERIFIED": proof}

    actions = report(environment, tracker)

    assert actions == (f"opened {job}",)
    assert "- Result: `unproven`\n" in tracker.created[0][1]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_SERVER_URL", "https://example.invalid"),
        ("GITHUB_REPOSITORY", "example/kora\n@octocat"),
        ("GITHUB_REPOSITORY", ""),
        ("GITHUB_RUN_ID", "12a"),
        ("GITHUB_RUN_ATTEMPT", "0"),
        ("GITHUB_SHA", REVISION[:39]),
        ("GITHUB_SHA", REVISION.upper()),
    ],
)
def test_an_invalid_run_identity_refuses_before_any_request(
    name: str, value: str
) -> None:
    tracker = RecordingTracker(())
    environment = {**passing(), "LIFETIME_RESULT": "failure", name: value}

    with pytest.raises(ReportFailure, match=name):
        report(environment, tracker)

    assert tracker.listings == 0
    assert tracker.created == []


@pytest.mark.parametrize(
    "entry",
    [
        {"number": 5, "title": LIFETIME_TITLE, "user": {"login": "octocat"}},
        {**bot_issue(6, LIFETIME_TITLE), "pull_request": {"url": "pulls/6"}},
        {"number": "7", "title": LIFETIME_TITLE, "user": BOT},
        {"number": True, "title": LIFETIME_TITLE, "user": BOT},
        {"number": 0, "title": LIFETIME_TITLE, "user": BOT},
        {"number": 8, "title": None, "user": BOT},
        {"number": 9, "title": LIFETIME_TITLE, "user": "github-actions[bot]"},
        {"number": 10, "title": LIFETIME_TITLE},
    ],
    ids=[
        "other author",
        "pull request",
        "text number",
        "boolean number",
        "zero number",
        "missing title",
        "flat user",
        "no user",
    ],
)
def test_reporter_issue_ignores_entries_it_did_not_open(
    entry: dict[str, object],
) -> None:
    result = reporter_issue(entry)

    assert result is None


def test_reporter_issue_keeps_any_title_of_its_own_issues() -> None:
    entry = bot_issue(11, "Daily master lane failed: IR model measurements")

    result = reporter_issue(entry)

    assert result == OpenIssue(11, "Daily master lane failed: IR model measurements")


@pytest.mark.parametrize(
    "value",
    [{"items": []}, ["issue"], [bot_issue(1, LIFETIME_TITLE)] * 101, None],
    ids=["object", "text entry", "oversized page", "null"],
)
def test_entries_refuse_a_listing_of_another_shape(value: object) -> None:
    with pytest.raises(ReportFailure, match="unexpected issue listing"):
        entries(value)


def test_tracker_lists_bot_issues_through_a_creator_query(
    issue_api: IssueApi,
) -> None:
    issue_api.replies.append(
        listing(
            bot_issue(88, LIFETIME_TITLE),
            {"number": 5, "title": LIFETIME_TITLE, "user": {"login": "octocat"}},
            {**bot_issue(6, LIFETIME_TITLE), "pull_request": {}},
            bot_issue(31, LIFETIME_TITLE),
        )
    )
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    listed = tracker.open_issues()

    assert listed == (OpenIssue(88, LIFETIME_TITLE), OpenIssue(31, LIFETIME_TITLE))
    [received] = issue_api.received
    assert (received.method, received.path) == ("GET", LISTING.format(page=1))
    assert expected_headers(received) == GITHUB_HEADERS
    assert received.body == b""


def test_tracker_follows_pages_until_a_short_page(issue_api: IssueApi) -> None:
    full = [bot_issue(number, f"Issue {number}") for number in range(1, 101)]
    issue_api.replies.extend([listing(*full), listing(bot_issue(101, "Last"))])
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    listed = tracker.open_issues()

    assert len(listed) == 101
    assert [received.path for received in issue_api.received] == [
        LISTING.format(page=1),
        LISTING.format(page=2),
    ]


def test_tracker_refuses_listings_beyond_its_bound(issue_api: IssueApi) -> None:
    full = [bot_issue(number, f"Issue {number}") for number in range(1, 101)]
    issue_api.replies.extend([listing(*full)] * 10)
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    with pytest.raises(ReportFailure, match="bounded listing of 1000 issues"):
        tracker.open_issues()

    assert [received.path for received in issue_api.received] == [
        LISTING.format(page=page) for page in range(1, 11)
    ]


def test_tracker_creates_an_issue_with_a_json_body(issue_api: IssueApi) -> None:
    issue_api.replies.append(Reply(201, b'{"number": 9}'))
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    tracker.create(LIFETIME_TITLE, LIFETIME_BODY)

    [received] = issue_api.received
    assert (received.method, received.path) == ("POST", "/repos/example/kora/issues")
    assert expected_headers(received) == GITHUB_HEADERS
    assert received.headers["content-type"] == "application/json"
    assert json.loads(received.body) == {"title": LIFETIME_TITLE, "body": LIFETIME_BODY}


def test_report_comments_on_the_oldest_bot_issue_over_http(
    issue_api: IssueApi,
) -> None:
    issue_api.replies.extend(
        [
            listing(
                bot_issue(88, LIFETIME_TITLE),
                {"number": 5, "title": LIFETIME_TITLE, "user": {"login": "octocat"}},
                bot_issue(2, "Daily master lane failed: IR model measurements"),
                bot_issue(31, LIFETIME_TITLE),
            ),
            Reply(201, b'{"id": 1}'),
        ]
    )
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)
    environment = {**passing(), "LIFETIME_RESULT": "failure"}

    actions = report(environment, tracker)

    assert actions == ("commented lifetime-tests",)
    listed, commented = issue_api.received
    assert (listed.method, listed.path) == ("GET", LISTING.format(page=1))
    assert (commented.method, commented.path) == (
        "POST",
        "/repos/example/kora/issues/31/comments",
    )
    assert expected_headers(commented) == GITHUB_HEADERS
    assert json.loads(commented.body) == {"body": LIFETIME_COMMENT}


def test_passing_report_only_lists_over_http(issue_api: IssueApi) -> None:
    issue_api.replies.append(listing(bot_issue(31, LIFETIME_TITLE)))
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    actions = report(passing(), tracker)

    assert actions == ()
    assert [(item.method, item.path) for item in issue_api.received] == [
        ("GET", LISTING.format(page=1))
    ]


def test_tracker_refuses_redirects_without_forwarding_the_token(
    issue_api: IssueApi, other_host: IssueApi
) -> None:
    issue_api.replies.append(Reply(302, b"{}", f"{other_host.base}/issues"))
    tracker = GitHubTracker("example/kora", ISSUE_WRITER, issue_api.base)

    with pytest.raises(urllib.error.HTTPError) as refused:
        tracker.open_issues()

    assert refused.value.code == 302
    assert len(issue_api.received) == 1
    assert other_host.received == []


@pytest.mark.parametrize(
    "base",
    [
        "http://api.github.com",
        "https://api.github.com/",
        "https://example.invalid",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/repos",
        "file:///tmp/issues",
    ],
)
def test_tracker_accepts_only_github_or_a_loopback_server(base: str) -> None:
    with pytest.raises(ReportFailure, match="GitHub or a loopback test server"):
        GitHubTracker("example/kora", ISSUE_WRITER, base)


def test_report_job_passes_every_lane_result_and_proof() -> None:
    job = job_of(repository_workflows(), "daily-lanes.yml", "report")
    expected = {
        "GITHUB_TOKEN": "${{ github.token }}",
        **{
            f"{lane.variable}_RESULT": f"${{{{ needs.{lane.job}.result }}}}"
            for lane in LANES
        },
        **{
            f"{lane.variable}_VERIFIED": f"${{{{ needs.{lane.job}.outputs.verified }}}}"
            for lane in LANES
            if lane.proof
        },
    }

    step = step_list(job)[-1]

    assert job["needs"] == [lane.job for lane in LANES]
    assert table(step["env"]) == expected
