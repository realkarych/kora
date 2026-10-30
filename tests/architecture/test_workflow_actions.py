import re
from typing import cast

from tests.support.workflows import (
    action_name,
    command_lines,
    step_list,
    table,
    workflow_document,
    workflow_names,
)

PINNED_ACTION = re.compile(
    r"[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}"
)
ACTION_COMMITS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact": "634f93cb2916e3fdff6788551b99b062d0335ce0",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    "astral-sh/setup-uv": "c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "github/codeql-action": "6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee",
    "google/osv-scanner-action": "b1d267aea5211fa648d9d442fa64b90dbdcbdd36",
    "lycheeverse/lychee-action": "e7477775783ea5526144ba13e8db5eec57747ce8",
    "pnpm/action-setup": "ea17c68df8912ef543352723c149a84f56e3d413",
}
FAILURE_HANDLERS = frozenset({"||", "trap"})
RUNNER_OVERRIDE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:PYTEST_[A-Z0-9_]*|PYTHONWARNINGS|GITHUB_PATH|BASH_ENV|PATH)"
    r"(?![A-Za-z0-9_])"
)
UNMODELLED_QUOTING = ("$'", '$"')
ENVIRONMENT_FILE = "GITHUB_ENV"
ENVIRONMENT_FILE_NAMES = frozenset(
    {
        "KORA_BUILD_CONSTRAINT_DIR",
        "KORA_LIFETIME_FULL_DISK_ROOT",
        "KORA_PRICING_ISOLATION_VERIFIED",
        "KORA_PRICING_ROOT",
    }
)
ENVIRONMENT_FILE_ENTRY = re.compile(r"(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>%s|true)\\n")
SHELL_VARIABLE = re.compile(r"\$[A-Z][A-Z0-9_]*")
WORKFLOW_TRIGGERS = {
    "ci.yml": {"pull_request", "push"},
    "codeql.yml": {"pull_request", "push", "schedule"},
    "conformance-lifetime.yml": {"workflow_dispatch", "schedule"},
    "gitleaks.yml": {"pull_request", "push", "schedule", "workflow_dispatch"},
    "osv-scanner.yml": {"pull_request", "push", "schedule"},
    "storage.yml": {"workflow_dispatch"},
    "mutation.yml": {"schedule", "workflow_dispatch"},
}
ORDINARY_PULL_REQUEST_WORKFLOWS = (
    "ci.yml",
    "codeql.yml",
    "gitleaks.yml",
    "osv-scanner.yml",
)
GATING_JOB_CONDITIONS = {
    "ci.yml:browser": "${{ always() }}",
    "ci.yml:scaffold": "${{ always() }}",
    "ci.yml:cel-measurements": "${{ always() }}",
    "osv-scanner.yml:pull-request": "github.event_name == 'pull_request'",
    "storage.yml:storage-evidence": "${{ always() }}",
}
JOB_PERMISSIONS = {
    "mutation.yml:audit": {"contents": "read", "actions": "read"},
    "codeql.yml:analyze": {
        "contents": "read",
        "actions": "read",
        "security-events": "write",
    },
    "gitleaks.yml:scan": {"contents": "read", "pull-requests": "read"},
    "osv-scanner.yml:pull-request": {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    },
    "osv-scanner.yml:full-scan": {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    },
}
BRANCH_PROTECTION_CONTEXTS = frozenset(
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
        "Dependencies introduced by the pull request",
        "CEL evaluator lifetime",
        "Console browser checks",
    }
)
RELEASE_QUALIFICATION_CONTEXTS = frozenset({"Required storage evidence"})
GATING_CONTEXTS = BRANCH_PROTECTION_CONTEXTS | RELEASE_QUALIFICATION_CONTEXTS


def workflow_job_entries() -> list[tuple[str, dict[str, object]]]:
    return [
        (f"{name}:{job_id}", table(job))
        for name in workflow_names()
        for job_id, job in table(workflow_document(name)["jobs"]).items()
    ]


def action_references() -> list[tuple[str, dict[str, object]]]:
    references: list[tuple[str, dict[str, object]]] = []
    for location, job in workflow_job_entries():
        entries = [job, *(step_list(job) if "steps" in job else [])]
        references.extend((location, entry) for entry in entries if "uses" in entry)
    return references


def run_scripts() -> list[tuple[str, object]]:
    return [
        (location, entry["run"])
        for location, job in workflow_job_entries()
        for entry in (step_list(job) if "steps" in job else [])
        if "run" in entry
    ]


def defines_function(line: tuple[str, ...]) -> bool:
    return "function" in line or any(
        word == "()" and not previous.endswith("=")
        for previous, word in zip(line, line[1:], strict=False)
    )


def masks_a_failure(line: tuple[str, ...]) -> bool:
    handled = any(word in FAILURE_HANDLERS for word in line)
    relaxed = any(
        word == "set" and option.startswith("+")
        for word, option in zip(line, line[1:], strict=False)
    )
    overridden = any(RUNNER_OVERRIDE.search(word) for word in line)
    return handled or relaxed or overridden or defines_function(line)


def environment_file_entry(line: tuple[str, ...]) -> re.Match[str] | None:
    if line[:1] != ("printf",) or line[-2:] != (">>", f"${ENVIRONMENT_FILE}"):
        return None
    entry = ENVIRONMENT_FILE_ENTRY.fullmatch(line[1])
    values = line[2:-2]
    if entry is None or entry["name"] not in ENVIRONMENT_FILE_NAMES:
        return None
    if entry["value"] == "true":
        return entry if values == () else None
    single = len(values) == 1 and SHELL_VARIABLE.fullmatch(values[0]) is not None
    return entry if single else None


def triggers(document: dict[str, object]) -> set[str]:
    value = document["on"]
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(entry) for entry in cast(list[object], value)}
    return set(table(value))


def environments(document: dict[str, object]) -> list[dict[str, object]]:
    jobs = [table(job) for job in table(document["jobs"]).values()]
    scopes = [
        document,
        *jobs,
        *(step for job in jobs for step in (step_list(job) if "steps" in job else [])),
        *(
            table(job["container"])
            for job in jobs
            if isinstance(job.get("container"), dict)
        ),
        *(
            table(service)
            for job in jobs
            for service in table(job.get("services", {})).values()
        ),
    ]
    return [table(scope["env"]) for scope in scopes if "env" in scope]


def shells(document: dict[str, object]) -> list[object]:
    jobs = [table(job) for job in table(document["jobs"]).values()]
    defaults = [
        table(table(scope["defaults"]).get("run", {}))
        for scope in (document, *jobs)
        if "defaults" in scope
    ]
    steps = [
        step for job in jobs for step in (step_list(job) if "steps" in job else [])
    ]
    return [scope["shell"] for scope in (*defaults, *steps) if "shell" in scope]


def repository(uses: object) -> str:
    return "/".join(action_name(uses).lower().split("/")[:2])


def references_to(action: str) -> list[tuple[str, dict[str, object]]]:
    return [
        (location, entry)
        for location, entry in action_references()
        if action_name(entry["uses"]).lower() == action
    ]


def rendered_names(job: dict[str, object]) -> list[str]:
    template = str(job.get("name", ""))
    matrix = table(table(job.get("strategy", {})).get("matrix", {}))
    rows = [table(row) for row in cast(list[object], matrix.get("include", []))]
    names: list[str] = []
    for row in rows or [{}]:
        name = template
        for key, value in row.items():
            name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
        names.append(name)
    return names


def test_every_workflow_action_is_pinned_to_its_reviewed_commit() -> None:
    references = action_references()

    unpinned = [
        f"{location}: {entry['uses']}"
        for location, entry in references
        if PINNED_ACTION.fullmatch(str(entry["uses"])) is None
    ]
    unreviewed = [
        f"{location}: {entry['uses']}"
        for location, entry in references
        if ACTION_COMMITS.get(repository(entry["uses"]))
        != str(entry["uses"]).rpartition("@")[2]
    ]

    assert {location.partition(":")[0] for location, _ in references} == set(
        workflow_names()
    )
    assert unpinned == []
    assert unreviewed == []
    assert {repository(entry["uses"]) for _, entry in references} == set(ACTION_COMMITS)


def test_every_checkout_drops_persisted_credentials() -> None:
    checkouts = references_to("actions/checkout")

    persisted = [
        location
        for location, entry in checkouts
        if table(entry.get("with", {})).get("persist-credentials") is not False
    ]

    assert checkouts
    assert persisted == []


def test_every_artifact_upload_fails_when_its_files_are_missing() -> None:
    uploads = references_to("actions/upload-artifact")

    tolerant = [
        location
        for location, entry in uploads
        if table(entry.get("with", {})).get("if-no-files-found") != "error"
    ]

    assert uploads
    assert tolerant == []


def test_no_workflow_script_ignores_a_failed_command() -> None:
    scripts = run_scripts()

    masked = [
        f"{location}: {' '.join(line)}"
        for location, script in scripts
        for line in command_lines(script)
        if masks_a_failure(line)
    ]
    unmodelled = [
        location
        for location, script in scripts
        if any(quoting in str(script) for quoting in UNMODELLED_QUOTING)
    ]

    assert {location.partition(":")[0] for location, _ in scripts} >= {
        "ci.yml",
        "conformance-lifetime.yml",
        "storage.yml",
    }
    assert masked == []
    assert unmodelled == []


def test_workflows_keep_read_only_scope_and_their_reviewed_triggers() -> None:
    documents = {name: workflow_document(name) for name in workflow_names()}
    jobs = workflow_job_entries()

    granted = {
        location: job["permissions"] for location, job in jobs if "permissions" in job
    }

    assert {name: triggers(document) for name, document in documents.items()} == (
        WORKFLOW_TRIGGERS
    )
    assert all(
        document["permissions"] == {"contents": "read"}
        for document in documents.values()
    )
    assert granted == JOB_PERMISSIONS
    assert all("secrets" not in job for _, job in jobs)


def test_ordinary_pull_request_workflows_run_for_source_events_without_readiness() -> (
    None
):
    documents = [workflow_document(name) for name in ORDINARY_PULL_REQUEST_WORKFLOWS]

    subscriptions = [table(document["on"])["pull_request"] for document in documents]

    assert all(
        subscription == {"types": ["opened", "synchronize", "reopened"]}
        for subscription in subscriptions
    )


def test_ordinary_pull_request_jobs_and_steps_admit_drafts() -> None:
    jobs = [
        (location, job)
        for location, job in workflow_job_entries()
        if location.partition(":")[0] in ORDINARY_PULL_REQUEST_WORKFLOWS
    ]

    draft_conditions = [
        f"{location}: {entry['if']}"
        for location, job in jobs
        for entry in [job, *(step_list(job) if "steps" in job else [])]
        if "if" in entry and re.search(r"\bdraft\b", str(entry["if"]))
    ]

    assert jobs
    assert draft_conditions == []


def test_every_workflow_shell_exits_on_error() -> None:
    documents = [workflow_document(name) for name in workflow_names()]

    configured = [shell for document in documents for shell in shells(document)]

    assert configured
    assert set(configured) == {"bash"}


def test_no_workflow_overrides_pytest_or_command_lookup_through_the_environment() -> (
    None
):
    documents = [workflow_document(name) for name in workflow_names()]

    names = {
        name
        for document in documents
        for scope in environments(document)
        for name in scope
    }

    assert names
    assert not any(RUNNER_OVERRIDE.fullmatch(name) for name in names)


def test_every_environment_file_write_sets_one_reviewed_name() -> None:
    lines = [
        (location, line)
        for location, script in run_scripts()
        for line in command_lines(script)
        if any(ENVIRONMENT_FILE in word for word in line)
    ]

    entries = [
        (location, line, environment_file_entry(line)) for location, line in lines
    ]
    unreviewed = [
        f"{location}: {' '.join(line)}"
        for location, line, entry in entries
        if entry is None
    ]
    written = {entry["name"] for _, _, entry in entries if entry is not None}

    assert unreviewed == []
    assert written == ENVIRONMENT_FILE_NAMES


def test_every_gating_status_context_is_produced() -> None:
    jobs = workflow_job_entries()

    produced = {name for _, job in jobs for name in rendered_names(job)}
    gating = [
        (location, job)
        for location, job in jobs
        if GATING_CONTEXTS.intersection(rendered_names(job))
    ]

    assert produced >= GATING_CONTEXTS
    assert {
        location: job["if"] for location, job in gating if "if" in job
    } == GATING_JOB_CONDITIONS
    assert all("continue-on-error" not in job for _, job in gating)
