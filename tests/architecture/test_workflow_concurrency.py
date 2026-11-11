import pytest

from tests.support.workflows import table, workflow_document

PUSH_COMPLETE_WORKFLOWS = ("ci.yml", "codeql.yml", "gitleaks.yml", "osv-scanner.yml")
PER_REVISION_GROUP = (
    "${{ github.workflow }}-${{ github.event_name == 'pull_request' "
    "&& github.ref || github.sha }}"
)
PULL_REQUEST_CANCELLATION = "${{ github.event_name == 'pull_request' }}"


@pytest.mark.parametrize("filename", PUSH_COMPLETE_WORKFLOWS)
def test_only_superseded_pull_request_runs_are_cancelled(filename: str) -> None:
    workflow = workflow_document(filename)

    concurrency = table(workflow["concurrency"])

    assert concurrency == {
        "group": PER_REVISION_GROUP,
        "cancel-in-progress": PULL_REQUEST_CANCELLATION,
    }
