import pytest

from scripts.ci.scheduled_report import LANES, Lane, ReportFailure, report
from tests.support.workflows import job_of, repository_workflows, step_list, table

REVISION = "0123456789abcdef0123456789abcdef01234567"
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
    "Its tests or measurements no longer run on pull requests, so this issue is the "
    "visible record of the failure until the lane passes again.\n\n"
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


class RecordingTracker:
    def __init__(self, existing: dict[str, tuple[int, ...]]) -> None:
        self.existing = existing
        self.searched: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []

    def open_issues(self, title: str) -> tuple[int, ...]:
        self.searched.append(title)
        return self.existing.get(title, ())

    def create(self, title: str, body: str) -> None:
        self.created.append((title, body))

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))


def passing() -> dict[str, str]:
    values = dict(RUN)
    for lane in LANES:
        values[f"{lane.variable}_RESULT"] = "success"
        if lane.proof:
            values[f"{lane.variable}_VERIFIED"] = "true"
    return values


def lane_named(job: str) -> Lane:
    return next(lane for lane in LANES if lane.job == job)


def test_a_failing_lane_opens_one_issue_from_the_fixed_template() -> None:
    tracker = RecordingTracker({})
    environment = {**passing(), "LIFETIME_RESULT": "failure"}

    actions = report(environment, tracker)

    assert actions == ("opened lifetime-tests",)
    assert tracker.searched == [LIFETIME_TITLE]
    assert tracker.created == [(LIFETIME_TITLE, LIFETIME_BODY)]
    assert tracker.comments == []


def test_a_repeated_failure_comments_on_the_oldest_open_issue() -> None:
    tracker = RecordingTracker({LIFETIME_TITLE: (88, 31)})
    environment = {**passing(), "LIFETIME_RESULT": "failure"}

    actions = report(environment, tracker)

    assert actions == ("commented lifetime-tests",)
    assert tracker.created == []
    assert tracker.comments == [(31, LIFETIME_COMMENT)]


def test_passing_lanes_report_nothing() -> None:
    tracker = RecordingTracker({})

    actions = report(passing(), tracker)

    assert actions == ()
    assert tracker.searched == []


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
    tracker = RecordingTracker({})
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
    tracker = RecordingTracker({})
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
    tracker = RecordingTracker({})
    environment = {**passing(), "LIFETIME_RESULT": "failure", name: value}

    with pytest.raises(ReportFailure, match=name):
        report(environment, tracker)

    assert tracker.searched == []
    assert tracker.created == []


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
