from tests.support.workflows import step_list, table, workflow_document


def test_mutation_audit_is_bounded_master_only_and_not_a_pr_gate() -> None:
    workflow = workflow_document("mutation.yml")
    job = table(table(workflow["jobs"])["audit"])
    steps = step_list(job)
    clock = steps[0]
    checkout = table(steps[1]["with"])
    installation = steps[3]
    execution = steps[4]
    publication = table(steps[5]["with"])

    assert table(workflow["on"]) == {
        "schedule": [{"cron": "17 3 * * *"}],
        "workflow_dispatch": None,
    }
    assert table(workflow["concurrency"])["cancel-in-progress"] is False
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 10
    assert 'test "$GITHUB_REF" = refs/heads/master' in str(clock["run"])
    assert checkout["ref"] == "${{ github.sha }}"
    assert checkout["persist-credentials"] is False
    assert installation["timeout-minutes"] == 1
    assert "--locked --group mutation --group build" in str(installation["run"])
    assert '--source-revision "$GITHUB_SHA"' in str(execution["run"])
    assert publication["retention-days"] == 14
    assert publication["if-no-files-found"] == "error"
    assert publication["path"] == "mutation-report/"
    assert all("continue-on-error" not in step for step in steps)
