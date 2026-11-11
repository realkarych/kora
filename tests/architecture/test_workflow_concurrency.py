import pytest

from tests.support.workflows import table, workflow_document, workflow_names

PER_REVISION_GROUP = (
    "${{ github.workflow }}-${{ github.event_name == 'pull_request' "
    "&& github.ref || github.sha }}"
)
PULL_REQUEST_CANCELLATION = "${{ github.event_name == 'pull_request' }}"
PUSH_TRIGGERED_WORKFLOWS = tuple(
    filename
    for filename in workflow_names()
    if "push" in table(workflow_document(filename)["on"])
)


def test_push_triggered_workflows_include_ci_and_security_scans() -> None:
    expected = {"ci.yml", "codeql.yml", "gitleaks.yml", "osv-scanner.yml"}

    actual = set(PUSH_TRIGGERED_WORKFLOWS)

    assert expected <= actual


@pytest.mark.parametrize("filename", PUSH_TRIGGERED_WORKFLOWS)
def test_only_superseded_pull_request_runs_are_cancelled(filename: str) -> None:
    workflow = workflow_document(filename)

    concurrency = table(workflow["concurrency"])

    assert concurrency == {
        "group": PER_REVISION_GROUP,
        "cancel-in-progress": PULL_REQUEST_CANCELLATION,
    }
