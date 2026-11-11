import re
from collections.abc import Callable
from typing import cast

import pytest

from tests.support.workflows import (
    action_name,
    step_list,
    table,
    workflow_document,
    workflow_names,
)

Workflows = dict[str, dict[str, object]]
Mutation = Callable[[Workflows], None]
JobEntry = tuple[str, dict[str, object]]

FULL_COMMIT_PIN = re.compile(
    r"[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}"
)
REVIEWED_ACTION_COMMITS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact": "634f93cb2916e3fdff6788551b99b062d0335ce0",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    "astral-sh/setup-uv": "c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "github/codeql-action": "6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee",
    "google/osv-scanner-action": "b1d267aea5211fa648d9d442fa64b90dbdcbdd36",
    "pnpm/action-setup": "ea17c68df8912ef543352723c149a84f56e3d413",
}
READ_ONLY_WORKFLOW_PERMISSIONS = {"contents": "read"}
READ_ACCESS = frozenset({"read", "none"})
ALLOWED_WRITE_SCOPES = frozenset(
    {
        "codeql.yml:analyze:security-events",
        "osv-scanner.yml:pull-request:security-events",
        "osv-scanner.yml:full-scan:security-events",
    }
)
PRIVILEGED_TRIGGERS = frozenset({"pull_request_target", "workflow_run"})
PROTECTED_STATUS_NAMES = frozenset(
    {
        "Markdown lint",
        "Links and anchors",
        "Mermaid diagrams",
        "Example graph documents",
        "GitHub templates",
        "Scaffold (ubuntu-24.04, Python 3.11.17)",
        "Scaffold (ubuntu-24.04, Python 3.15.0)",
        "Scaffold (macos-15, Python 3.11.17)",
        "CodeQL (python)",
        "CodeQL (javascript-typescript)",
        "CodeQL (actions)",
        "Secret scan",
        "Dependencies introduced by the pull request / osv-scan",
        "CEL evaluator lifetime",
        "Console browser checks",
    }
)
RELEASE_STATUS_NAMES = frozenset({"Required storage evidence"})
REQUIRED_STATUS_NAMES = PROTECTED_STATUS_NAMES | RELEASE_STATUS_NAMES
REUSABLE_WORKFLOW_JOBS = {
    "google/osv-scanner-action/.github/workflows/osv-scanner-reusable-pr.yml": (
        "osv-scan"
    ),
}
GATING_JOB_CONDITIONS = {
    "ci.yml:browser": "${{ always() }}",
    "ci.yml:scaffold": "${{ always() }}",
    "ci.yml:cel-measurements": "${{ always() }}",
    "osv-scanner.yml:pull-request": "github.event_name == 'pull_request'",
    "storage.yml:storage-evidence": "${{ always() }}",
}
UNREVIEWED_COMMIT = "0" * 40


def repository_workflows() -> Workflows:
    return {name: workflow_document(name) for name in workflow_names()}


def jobs_of(workflows: Workflows) -> list[JobEntry]:
    return [
        (f"{name}:{identity}", table(job))
        for name, document in workflows.items()
        for identity, job in table(document["jobs"]).items()
    ]


def job_of(workflows: Workflows, name: str, identity: str) -> dict[str, object]:
    return table(table(workflows[name]["jobs"])[identity])


def action_references(workflows: Workflows) -> list[tuple[str, str]]:
    return [
        (location, str(entry["uses"]))
        for location, job in jobs_of(workflows)
        for entry in (job, *step_list(job))
        if "uses" in entry
    ]


def action_repository(uses: str) -> str:
    return "/".join(action_name(uses).lower().split("/")[:2])


def unpinned_actions(workflows: Workflows) -> list[str]:
    return [
        f"{location}: {uses}"
        for location, uses in action_references(workflows)
        if FULL_COMMIT_PIN.fullmatch(uses) is None
        or REVIEWED_ACTION_COMMITS.get(action_repository(uses))
        != uses.rpartition("@")[2]
    ]


def excess_job_permissions(location: str, job: dict[str, object]) -> list[str]:
    granted: object = job.get("permissions", {})
    if not isinstance(granted, dict):
        return [f"{location}: permissions {granted}"]
    return [
        f"{location}: {scope} {access}"
        for scope, access in cast(dict[str, object], granted).items()
        if access not in READ_ACCESS
        and not (access == "write" and f"{location}:{scope}" in ALLOWED_WRITE_SCOPES)
    ]


def persisted_tokens(location: str, job: dict[str, object]) -> list[str]:
    return [
        f"{location}: {step['uses']} persists credentials"
        for step in step_list(job)
        if action_name(step.get("uses", "")) == "actions/checkout"
        and table(step.get("with", {})).get("persist-credentials") is not False
    ]


def excess_token_access(workflows: Workflows) -> list[str]:
    violations = [
        f"{name}: workflow permissions {document.get('permissions')}"
        for name, document in workflows.items()
        if document.get("permissions") != READ_ONLY_WORKFLOW_PERMISSIONS
    ]
    for location, job in jobs_of(workflows):
        violations.extend(excess_job_permissions(location, job))
        violations.extend(persisted_tokens(location, job))
        if "secrets" in job:
            violations.append(f"{location}: passes secrets")
    return violations


def triggers(document: dict[str, object]) -> frozenset[str]:
    value = document["on"]
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list):
        return frozenset(str(entry) for entry in cast(list[object], value))
    return frozenset(table(value))


def privileged_triggers(workflows: Workflows) -> list[str]:
    return [
        f"{name}: {trigger}"
        for name, document in workflows.items()
        for trigger in sorted(triggers(document) & PRIVILEGED_TRIGGERS)
    ]


def status_names(job: dict[str, object]) -> list[str]:
    template = str(job.get("name", ""))
    matrix = table(table(job.get("strategy", {})).get("matrix", {}))
    rows = [table(row) for row in cast(list[object], matrix.get("include", []))]
    suffix = REUSABLE_WORKFLOW_JOBS.get(action_name(job.get("uses", "")))
    names: list[str] = []
    for row in rows or [{}]:
        name = template
        for key, value in row.items():
            name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
        names.append(name if suffix is None else f"{name} / {suffix}")
    return names


def unguarded_required_statuses(workflows: Workflows) -> list[str]:
    produced = [
        (location, job, name)
        for location, job in jobs_of(workflows)
        for name in status_names(job)
    ]
    gating = {
        location: job
        for location, job, name in produced
        if name in REQUIRED_STATUS_NAMES
    }
    counts = {
        required: [name for _, _, name in produced].count(required)
        for required in sorted(REQUIRED_STATUS_NAMES)
    }
    return [
        *(
            f"{name}: produced {count} times"
            for name, count in counts.items()
            if count != 1
        ),
        *(
            f"{location}: condition {job.get('if')}"
            for location, job in gating.items()
            if job.get("if") != GATING_JOB_CONDITIONS.get(location)
        ),
        *(
            f"{location}: continues on error"
            for location, job in gating.items()
            if "continue-on-error" in job
        ),
    ]


def unpin_checkout_to_a_tag(workflows: Workflows) -> None:
    step_list(job_of(workflows, "ci.yml", "links"))[0]["uses"] = "actions/checkout@v7"


def pin_checkout_to_an_unreviewed_commit(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "ci.yml", "links"))[0]
    step["uses"] = f"actions/checkout@{UNREVIEWED_COMMIT}"


def add_an_unreviewed_action(workflows: Workflows) -> None:
    steps = cast(list[object], job_of(workflows, "gitleaks.yml", "scan")["steps"])
    steps.append({"uses": f"example/action@{UNREVIEWED_COMMIT}"})


def call_a_reusable_workflow_by_tag(workflows: Workflows) -> None:
    job = job_of(workflows, "osv-scanner.yml", "pull-request")
    job["uses"] = f"{action_name(job['uses'])}@v2"


@pytest.mark.parametrize(
    "mutation",
    [
        unpin_checkout_to_a_tag,
        pin_checkout_to_an_unreviewed_commit,
        add_an_unreviewed_action,
        call_a_reusable_workflow_by_tag,
    ],
)
def test_pin_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unpinned_actions(workflows)

    assert violations


def test_actions_are_pinned_to_reviewed_full_commits() -> None:
    workflows = repository_workflows()

    violations = unpinned_actions(workflows)

    assert action_references(workflows)
    assert violations == []


def grant_write_all_to_a_workflow(workflows: Workflows) -> None:
    workflows["ci.yml"]["permissions"] = "write-all"


def drop_the_workflow_permissions(workflows: Workflows) -> None:
    del workflows["gitleaks.yml"]["permissions"]


def widen_the_workflow_permissions(workflows: Workflows) -> None:
    workflows["mutation.yml"]["permissions"] = {"contents": "write"}


def grant_write_all_to_a_job(workflows: Workflows) -> None:
    job_of(workflows, "codeql.yml", "analyze")["permissions"] = "write-all"


def grant_an_unlisted_write_scope(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "scaffold-tests")
    job["permissions"] = {"contents": "read", "pull-requests": "write"}


def move_an_allowed_write_scope(workflows: Workflows) -> None:
    job = job_of(workflows, "codeql.yml", "analyze")
    job["permissions"] = {**table(job["permissions"]), "contents": "write"}


def pass_secrets_to_a_reusable_workflow(workflows: Workflows) -> None:
    job_of(workflows, "osv-scanner.yml", "full-scan")["secrets"] = "inherit"


def persist_checkout_credentials(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "ci.yml", "markdown"))[0]
    table(step["with"])["persist-credentials"] = True


def checkout_with_default_credentials(workflows: Workflows) -> None:
    del step_list(job_of(workflows, "codeql.yml", "analyze"))[0]["with"]


@pytest.mark.parametrize(
    "mutation",
    [
        grant_write_all_to_a_workflow,
        drop_the_workflow_permissions,
        widen_the_workflow_permissions,
        grant_write_all_to_a_job,
        grant_an_unlisted_write_scope,
        move_an_allowed_write_scope,
        pass_secrets_to_a_reusable_workflow,
        persist_checkout_credentials,
        checkout_with_default_credentials,
    ],
)
def test_permission_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = excess_token_access(workflows)

    assert violations


def test_workflow_and_job_permissions_are_minimal() -> None:
    workflows = repository_workflows()

    violations = excess_token_access(workflows)
    granted_writes = {
        f"{location}:{scope}"
        for location, job in jobs_of(workflows)
        for scope, access in table(job.get("permissions", {})).items()
        if access == "write"
    }

    assert violations == []
    assert granted_writes == ALLOWED_WRITE_SCOPES


def subscribe_to_pull_request_target(workflows: Workflows) -> None:
    table(workflows["ci.yml"]["on"])["pull_request_target"] = {
        "types": ["opened", "synchronize", "reopened"]
    }


def trigger_only_on_pull_request_target(workflows: Workflows) -> None:
    workflows["gitleaks.yml"]["on"] = "pull_request_target"


def list_pull_request_target(workflows: Workflows) -> None:
    workflows["codeql.yml"]["on"] = ["push", "pull_request_target"]


def follow_another_workflow_run(workflows: Workflows) -> None:
    table(workflows["storage.yml"]["on"])["workflow_run"] = {
        "workflows": ["CI"],
        "types": ["completed"],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        subscribe_to_pull_request_target,
        trigger_only_on_pull_request_target,
        list_pull_request_target,
        follow_another_workflow_run,
    ],
)
def test_trigger_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = privileged_triggers(workflows)

    assert violations


def test_no_workflow_has_a_privileged_trigger() -> None:
    workflows = repository_workflows()

    violations = privileged_triggers(workflows)

    assert violations == []


def rename_the_secret_scan(workflows: Workflows) -> None:
    job_of(workflows, "gitleaks.yml", "scan")["name"] = "Secrets"


def drop_a_scaffold_join_row(workflows: Workflows) -> None:
    matrix = table(table(job_of(workflows, "ci.yml", "scaffold")["strategy"])["matrix"])
    cast(list[object], matrix["include"]).pop()


def drop_the_pull_request_dependency_scan(workflows: Workflows) -> None:
    del table(workflows["osv-scanner.yml"]["jobs"])["pull-request"]


def duplicate_the_browser_status(workflows: Workflows) -> None:
    jobs = table(workflows["ci.yml"]["jobs"])
    jobs["browser-copy"] = {**job_of(workflows, "ci.yml", "browser"), "if": "false"}


def skip_the_browser_status_after_a_failed_need(workflows: Workflows) -> None:
    del job_of(workflows, "ci.yml", "browser")["if"]


def condition_the_secret_scan(workflows: Workflows) -> None:
    job_of(workflows, "gitleaks.yml", "scan")["if"] = "github.event_name == 'push'"


def let_the_cel_status_continue_on_error(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "cel-measurements")["continue-on-error"] = True


@pytest.mark.parametrize(
    "mutation",
    [
        rename_the_secret_scan,
        drop_a_scaffold_join_row,
        drop_the_pull_request_dependency_scan,
        duplicate_the_browser_status,
        skip_the_browser_status_after_a_failed_need,
        condition_the_secret_scan,
        let_the_cel_status_continue_on_error,
    ],
)
def test_status_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unguarded_required_statuses(workflows)

    assert violations


def test_protected_statuses_come_from_unconditional_gating_jobs() -> None:
    workflows = repository_workflows()

    violations = unguarded_required_statuses(workflows)

    assert violations == []
