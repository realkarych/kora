import dataclasses
import datetime
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci.ci_recovery import (
    CEL_PRODUCER_STEPS,
    Client,
    Evidence,
    Job,
    RecoveryError,
    Run,
    Step,
    classify,
    mapping,
    parse_job,
    parse_run,
    sequence,
    text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ci_recovery"
BEARER = "bearer-value"
REPOSITORY = "realkarych/kora"
TRIGGER = 42261532071
STORAGE_LANE = "Scaffold tests (macos-15, Python 3.11.17, storage)"


def recorded(name: str) -> Evidence:
    data = mapping(json.loads((FIXTURES / f"{name}.json").read_text()))
    run = parse_run(data["run"])
    return Evidence(
        run=run,
        jobs=tuple(parse_job(job) for job in sequence(data["jobs"])),
        annotations={
            int(key): tuple(text(item) for item in sequence(value))
            for key, value in mapping(data["annotations"]).items()
        },
        logs={
            int(key): "\n".join(text(item) for item in sequence(value))
            for key, value in mapping(data["logs"]).items()
        },
        history=(run,),
        ledger_complete=True,
        gate_inputs_unchanged=True,
    )


def first_attempt(evidence: Evidence) -> Evidence:
    run = dataclasses.replace(evidence.run, run_attempt=1, conclusion="failure")
    return dataclasses.replace(evidence, run=run, history=(run,))


def other_run(evidence: Evidence, head_sha: str, tree_id: str, offset: int) -> Run:
    return dataclasses.replace(
        evidence.run,
        id=evidence.run.id + offset,
        head_sha=head_sha,
        tree_id=tree_id,
        created_at=evidence.run.created_at + datetime.timedelta(minutes=offset),
    )


def kinds(evidence: Evidence) -> dict[str, str]:
    return {item.job.name: item.kind for item in classify(evidence).jobs}


def gate_job(job_id: int, name: str, *failing: str) -> Job:
    return Job(
        job_id,
        name,
        "failure",
        "GitHub Actions 1",
        1,
        ("ubuntu-24.04",),
        (Step("Set up job", "success"), *(Step(step, "failure") for step in failing)),
    )


def outage() -> Evidence:
    base = recorded("unacquired_general_lanes")
    jobs = (
        gate_job(
            1, "Scaffold evidence", "Require all test lanes and collection evidence"
        ),
        gate_job(
            2,
            "Scaffold (ubuntu-24.04, Python 3.11.17)",
            "Require verified scaffold evidence",
        ),
        gate_job(
            3,
            "Scaffold (ubuntu-24.04, Python 3.15.0)",
            "Require verified scaffold evidence",
        ),
        gate_job(
            4,
            "Scaffold (macos-15, Python 3.11.17)",
            "Require verified scaffold evidence",
        ),
        gate_job(5, "CEL evaluator lifetime", "Require every measurement and artifact"),
        gate_job(
            6,
            "Console browser checks",
            "Require browser verification or approved editorial scope",
        ),
    )
    return dataclasses.replace(base, jobs=jobs, annotations={}, logs={})


def with_jobs(evidence: Evidence, *added: Job) -> Evidence:
    names = {job.name for job in added}
    kept = tuple(job for job in evidence.jobs if job.name not in names)
    return dataclasses.replace(evidence, jobs=(*kept, *added))


def unacquired_lane() -> Job:
    return next(
        job
        for job in recorded("unacquired_storage_lane").jobs
        if job.name == STORAGE_LANE
    )


def test_unacquired_storage_lane_reruns() -> None:
    evidence = recorded("unacquired_storage_lane")

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "rerun"
    assert classes[STORAGE_LANE] == "no_runner"
    assert classes["Scaffold evidence"] == "dependent"


@pytest.mark.parametrize(
    "name", ["unacquired_general_lanes", "unacquired_general_lanes_second"]
)
def test_unacquired_general_lanes_with_timing_refusal_rerun(name: str) -> None:
    evidence = recorded(name)

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "rerun"
    assert {item.kind for item in decision.jobs} == {"no_runner", "dependent"}
    assert classes["CEL evaluator lifetime"] == "dependent"
    assert classes["Scaffold (macos-15, Python 3.11.17)"] == "dependent"


def test_run_without_root_jobs_and_failing_joins_reruns() -> None:
    evidence = outage()

    decision = classify(evidence)

    assert decision.verdict == "rerun"
    assert {item.kind for item in decision.jobs} == {"dependent"}


def test_failing_joins_with_root_jobs_present_do_not_rerun() -> None:
    evidence = outage()
    root = Job(
        7, "Markdown lint", "success", "GitHub Actions 2", 2, ("ubuntu-latest",), ()
    )

    decision = classify(with_jobs(evidence, root))

    assert decision.verdict == "no_rerun"
    assert decision.reason == "no_infrastructure_cause"


def test_failed_test_lane_does_not_rerun() -> None:
    evidence = recorded("failed_test_lane")

    decision = classify(evidence)

    assert decision.verdict == "no_rerun"
    assert decision.reason == STORAGE_LANE


def test_deadline_cancellation_during_tests_does_not_rerun() -> None:
    evidence = first_attempt(recorded("deadline_during_tests"))

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "no_rerun"
    assert classes["Scaffold tests (macos-15, Python 3.11.17, general-2)"] == "kora"


def test_deadline_cancellation_before_tests_does_not_rerun() -> None:
    evidence = first_attempt(recorded("download_failure_and_deadline"))

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "no_rerun"
    assert (
        classes["Scaffold tests (ubuntu-24.04, Python 3.11.17, local-runtime)"]
        == "kora"
    )


def test_deadline_cancellation_after_tests_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    lane = Job(
        8,
        "Scaffold tests (ubuntu-24.04, Python 3.11.17, local-runtime)",
        "cancelled",
        "GitHub Actions 3",
        3,
        ("ubuntu-24.04",),
        (
            Step("Collect and execute the exact test lane", "success"),
            Step("Build isolated distributions offline", "cancelled"),
            Step("Require complete lane execution", "failure"),
        ),
    )

    combined = with_jobs(evidence, lane)

    decision = classify(combined)
    classes = kinds(combined)

    assert decision.verdict == "no_rerun"
    assert classes[lane.name] == "kora"


def test_download_failure_does_not_rerun() -> None:
    evidence = first_attempt(recorded("download_failure_and_deadline"))

    classes = kinds(evidence)

    assert classes["Mermaid diagrams"] == "kora"


def test_superseded_pull_request_run_does_not_rerun() -> None:
    evidence = first_attempt(recorded("unacquired_storage_lane"))
    pull_request = dataclasses.replace(
        evidence.run, event="pull_request", head_branch="topic"
    )
    evidence = dataclasses.replace(evidence, run=pull_request)
    newer = other_run(evidence, "1" * 40, "2" * 40, 1)

    decision = classify(dataclasses.replace(evidence, history=(pull_request, newer)))

    assert decision.verdict == "skip"
    assert decision.reason == "superseded"


def test_second_attempt_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    second = dataclasses.replace(evidence.run, run_attempt=2)

    decision = classify(dataclasses.replace(evidence, run=second, history=(second,)))

    assert decision.verdict == "skip"
    assert decision.reason == "attempt"


def test_manual_rerun_in_history_uses_the_limit() -> None:
    evidence = recorded("unacquired_storage_lane")
    rerun = dataclasses.replace(evidence.run, run_attempt=2)

    decision = classify(dataclasses.replace(evidence, history=(rerun,)))

    assert decision.verdict == "skip"
    assert decision.reason == "limit_used"


def test_empty_commit_with_same_tree_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    earlier = other_run(evidence, "3" * 40, evidence.run.tree_id, -1)

    decision = classify(dataclasses.replace(evidence, history=(earlier, evidence.run)))

    assert decision.verdict == "skip"
    assert decision.reason == "limit_used"


def test_master_run_with_pull_request_tree_is_not_counted() -> None:
    evidence = recorded("unacquired_storage_lane")
    pull_request = dataclasses.replace(
        other_run(evidence, "4" * 40, evidence.run.tree_id, -1),
        event="pull_request",
        head_branch="topic",
    )

    decision = classify(
        dataclasses.replace(evidence, history=(pull_request, evidence.run))
    )

    assert decision.verdict == "rerun"


def test_incomplete_ledger_listing_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")

    decision = classify(dataclasses.replace(evidence, ledger_complete=False))

    assert decision.verdict == "skip"
    assert decision.reason == "ledger_incomplete"


def test_unacquired_lane_plus_failed_test_does_not_rerun() -> None:
    evidence = recorded("unacquired_lanes_with_failed_local_runtime")

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "no_rerun"
    assert "local-runtime" in decision.reason
    assert "no_runner" in classes.values()
    assert classes["Scaffold evidence"] == "kora"


def test_timing_refusal_other_than_missing_lane_does_not_rerun() -> None:
    evidence = recorded("unacquired_general_lanes")
    evidence_job = next(job for job in evidence.jobs if job.name == "Scaffold evidence")
    logs = {evidence_job.id: "the lane duration table lacks an environment"}

    decision = classify(dataclasses.replace(evidence, logs=logs))

    assert decision.verdict == "no_rerun"
    assert decision.reason == "Scaffold evidence"


def test_failed_browser_gate_with_change_scope_present_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    browser = gate_job(
        9,
        "Console browser checks",
        "Require browser verification or approved editorial scope",
    )

    decision = classify(with_jobs(evidence, browser))

    assert decision.verdict == "no_rerun"
    assert decision.reason == "Console browser checks"


def test_failed_browser_gate_with_unacquired_change_scope_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    scope = Job(
        12, "Classify verification scope", "cancelled", "", 0, ("ubuntu-24.04",), ()
    )
    browser = gate_job(
        13,
        "Console browser checks",
        "Require browser verification or approved editorial scope",
    )
    annotations = {
        **evidence.annotations,
        scope.id: ("The job was not acquired by Runner",),
    }
    combined = dataclasses.replace(
        with_jobs(evidence, scope, browser), annotations=annotations
    )

    decision = classify(combined)
    classes = kinds(combined)

    assert decision.verdict == "no_rerun"
    assert classes["Classify verification scope"] == "no_runner"
    assert classes["Console browser checks"] == "kora"


def test_failed_cel_download_with_producer_present_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")
    cel = gate_job(
        10,
        "CEL evaluator lifetime",
        *sorted(CEL_PRODUCER_STEPS),
        "Require every measurement and artifact",
    )

    decision = classify(with_jobs(evidence, cel))

    assert decision.verdict == "no_rerun"
    assert decision.reason == "CEL evaluator lifetime"


def test_unannotated_cancellation_does_not_rerun() -> None:
    evidence = recorded("unacquired_storage_lane")

    decision = classify(dataclasses.replace(evidence, annotations={}))

    assert decision.verdict == "no_rerun"


def test_zero_job_cancelled_run_does_not_rerun() -> None:
    evidence = recorded("zero_job_cancelled")
    failed_run = first_attempt(evidence)

    recorded_decision = classify(evidence)
    failed_decision = classify(failed_run)

    assert recorded_decision.verdict == "skip"
    assert failed_decision.reason == "no_jobs"


def with_labels(job: Job) -> Job:
    return dataclasses.replace(job, labels=("macos-15", "self-hosted"))


def with_runner_id(job: Job) -> Job:
    return dataclasses.replace(job, runner_id=5)


def with_runner_name(job: Job) -> Job:
    return dataclasses.replace(job, runner_name="GitHub Actions 5")


@pytest.mark.parametrize("change", [with_labels, with_runner_id, with_runner_name])
def test_unassigned_job_with_runner_facts_does_not_rerun(
    change: Callable[[Job], Job],
) -> None:
    evidence = with_jobs(recorded("unacquired_storage_lane"), change(unacquired_lane()))

    decision = classify(evidence)
    classes = kinds(evidence)

    assert decision.verdict == "no_rerun"
    assert classes[STORAGE_LANE] == "kora"


def test_changed_gate_inputs_are_skipped() -> None:
    evidence = recorded("unacquired_storage_lane")

    decision = classify(dataclasses.replace(evidence, gate_inputs_unchanged=False))

    assert decision.verdict == "skip"
    assert decision.reason == "workflow_changed"


def test_workflow_named_ci_at_another_path_is_skipped() -> None:
    evidence = recorded("unacquired_storage_lane")
    impostor = dataclasses.replace(evidence.run, path=".github/workflows/other.yml")

    decision = classify(
        dataclasses.replace(evidence, run=impostor, history=(impostor,))
    )

    assert decision.verdict == "skip"
    assert decision.reason == "workflow"


def test_continue_on_error_diagnostic_does_not_decide() -> None:
    evidence = recorded("unacquired_storage_lane")
    diagnostic = gate_job(
        11, "Local runtime diagnostics (macos-15, Python 3.11.17)", "Run diagnostics"
    )
    combined = with_jobs(evidence, diagnostic)

    decision = classify(combined)
    classes = kinds(combined)

    assert decision.verdict == "rerun"
    assert diagnostic.name not in classes


def test_fork_run_is_skipped() -> None:
    evidence = recorded("unacquired_storage_lane")
    fork = dataclasses.replace(evidence.run, head_repository_id=1)

    decision = classify(dataclasses.replace(evidence, run=fork, history=(fork,)))

    assert decision.verdict == "skip"
    assert decision.reason == "fork"


def test_token_header_is_not_redirected() -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    request = client.request(client.url("actions/runs/1"))

    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == f"Bearer {BEARER}"
    assert request.host == "api.github.com"


def test_request_outside_the_api_host_is_refused() -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    with pytest.raises(RecoveryError, match="outside the API host"):
        client.request(f"https://example.com/repos/{REPOSITORY}/actions/runs/1")


@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/logs/1",
        "http://productionresultssa1.blob.core.windows.net/logs/1",
        "https://productionresultssa1.blob.core.windows.net.example.com/logs/1",
    ],
)
def test_redirect_to_unlisted_host_is_refused(location: str) -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    with pytest.raises(RecoveryError, match="log redirect"):
        client.log_request(location)


def test_log_redirect_carries_no_credentials() -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    request = client.log_request(
        "https://productionresultssa12.blob.core.windows.net/logs/1?sig=x"
    )

    assert request.header_items() == []
    assert request.get_method() == "GET"


def test_client_allows_only_the_rerun_of_the_triggering_run() -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    allowed = client.request(client.url(f"actions/runs/{TRIGGER}/rerun"), "POST")

    assert allowed.get_method() == "POST"
    with pytest.raises(RecoveryError, match="triggering rerun"):
        client.request(client.url(f"actions/runs/{TRIGGER + 1}/rerun"), "POST")
    with pytest.raises(RecoveryError, match="triggering rerun"):
        client.request(client.url(f"actions/runs/{TRIGGER}"), "DELETE")
    with pytest.raises(RecoveryError, match="triggering rerun"):
        client.request(client.url(f"actions/runs/{TRIGGER}/rerun-failed-jobs"), "POST")


def test_client_refuses_unlisted_paths_and_values() -> None:
    client = Client(REPOSITORY, BEARER, TRIGGER)

    with pytest.raises(RecoveryError, match="unlisted repository path"):
        client.blob("README.md", "5" * 40)
    with pytest.raises(RecoveryError, match="commit or tree SHA"):
        client.blob(".github/workflows/ci.yml", "master")
    with pytest.raises(RecoveryError, match="positive integer"):
        Client(REPOSITORY, BEARER, 0)
