import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from tests.support.workflows import (
    Workflows,
    action_name,
    job_of,
    jobs_of,
    repository_workflows,
    step_list,
    table,
)

Mutation = Callable[[Workflows], None]
Leaf = tuple[tuple[str, ...], object]

ROOT = Path(__file__).resolve().parents[2]
FULL_COMMIT_PIN = re.compile(
    r"[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}"
)
REVIEWED_ACTION_COMMITS = {
    "actions/cache": "caa296126883cff596d87d8935842f9db880ef25",
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
        "daily-lanes.yml:report:issues",
        "osv-scanner.yml:pull-request:security-events",
        "osv-scanner.yml:full-scan:security-events",
    }
)
ALLOWED_TRIGGERS = frozenset({"pull_request", "push", "schedule", "workflow_dispatch"})
FORBIDDEN_ENVIRONMENT = re.compile(
    r"PYTEST_[A-Z0-9_]*|PYTHONWARNINGS|PATH|BASH_ENV|ENV|GITHUB_PATH"
    r"|LD_PRELOAD|LD_LIBRARY_PATH|DYLD_[A-Z0-9_]*"
)
EXPRESSION = r"\$\{\{(?:(?!\}\}).)*?"
UNTRUSTED_CONTEXT = re.compile(
    EXPRESSION + r"\b(?:github\.event\.|github\.event_name\b|github\.head_ref\b"
    r"|inputs\.)",
    re.S,
)
SECRET_CONTEXT = re.compile(EXPRESSION + r"\b(?:secrets\.|github\.token\b)", re.S)
ALLOWED_SECRET_SITES = frozenset(
    {
        "mutation.yml:jobs:audit:steps:"
        "Audit the frozen master revision:env:GITHUB_TOKEN",
        "daily-lanes.yml:jobs:report:steps:report:env:GITHUB_TOKEN",
    }
)
ISSUE_WRITER_CONDITIONS = {
    "daily-lanes.yml:report": "${{ always() && github.ref == 'refs/heads/master' }}",
}
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
GATE_SCRIPT = "scripts/ci/require_outcomes.sh"
GATE_JOBS = {
    "ci.yml:scaffold-tests": "scaffold-lane",
    "ci.yml:scaffold-evidence": "scaffold-evidence",
    "ci.yml:scaffold": "check",
    "ci.yml:cel-producer": "verify",
    "ci.yml:cel-measurements": "cel-measurements",
    "ci.yml:browser": "browser",
    "conformance-lifetime.yml:qualification": "verify",
    "conformance-lifetime.yml:installed-qualification": "check",
    "conformance-lifetime.yml:lifetime-evidence": "check",
    "daily-lanes.yml:resource-measurements": "verify",
    "daily-lanes.yml:provider-protocol-measurements": "verify",
    "daily-lanes.yml:pricing-measurements": "verify",
    "daily-lanes.yml:git-publication-measurements": "verify",
    "daily-lanes.yml:ir-evidence": "check",
    "daily-lanes.yml:lifetime-tests": "check",
    "daily-lanes.yml:storage-pressure-tests": "check",
    "storage.yml:storage-evidence": "check",
}
PROOF_ORIGINS = frozenset({"ci.yml:change-scope"})
ALWAYS = "${{ always() }}"
FLIPPED = {"success": "skipped", "skipped": "success", "true": "false", "false": "true"}
UNGATED_STEPS = {
    "ci.yml:scaffold-tests:unit_collisions": (
        "runs only on Linux local-runtime lanes, which the lane gate does not tell "
        "apart from macOS lanes; its failure fails the lane job itself"
    ),
    "ci.yml:scaffold-tests:cohort_evidence": (
        "copies cohort evidence that the shared Scaffold evidence job verifies; "
        "its failure fails the lane job itself"
    ),
}
MATRIX_REFERENCE = re.compile(r"matrix\.([\w-]+)")
MATRIX_VALUE = re.compile(r"\$\{\{ matrix\.([\w-]+) \}\}")
STEP_OUTCOME = re.compile(r"steps\.([\w-]+)\.outcome")
UNREVIEWED_COMMIT = "0" * 40
CLASSIFIER_PROGRAM = (
    "import os\n\n"
    'with open(os.environ["GITHUB_OUTPUT"], "a") as output:\n'
    '    output.write("full={origin}\\n")\n'
)
CLASSIFIED_FROM_BASE = "full=base\nverified=true\n"
CLASSIFIER_OUTPUTS = {
    "full": "${{ steps.scope.outputs.full }}",
    "verified": "${{ steps.scope.outputs.verified }}",
}
CHECKOUT_AUTHENTICATION_INPUT = "token"
ACQUISITION_CACHE_INPUTS = {"path": "${{ runner.temp }}/kora-acquisitions"}
ACQUISITION_CACHE_KEY = re.compile(
    r"kora-acquisitions-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}"
    r"-[a-z0-9.-]+(?:\$\{\{ matrix\.python \}\})?"
    r"-\$\{\{ hashFiles\('scripts/ci/acquire\.py'\) \}\}"
)


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


def writes_issues(job: dict[str, object]) -> bool:
    granted: object = job.get("permissions", {})
    if isinstance(granted, dict):
        return cast(dict[str, object], granted).get("issues") == "write"
    return granted == "write-all"


def issue_writers_beyond_master(workflows: Workflows) -> list[str]:
    jobs = dict(jobs_of(workflows))
    return [
        *(
            f"{location}: writes issues under {job.get('if')}"
            for location, job in jobs.items()
            if writes_issues(job)
            and job.get("if") != ISSUE_WRITER_CONDITIONS.get(location)
        ),
        *(
            f"{location}: missing issue writer"
            for location in ISSUE_WRITER_CONDITIONS
            if location not in jobs
        ),
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
    return [*violations, *issue_writers_beyond_master(workflows)]


def triggers(document: dict[str, object]) -> frozenset[str]:
    value = document["on"]
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list):
        return frozenset(str(entry) for entry in cast(list[object], value))
    return frozenset(table(value))


def unreviewed_triggers(workflows: Workflows) -> list[str]:
    return [
        f"{name}: {trigger}"
        for name, document in workflows.items()
        for trigger in sorted(triggers(document) - ALLOWED_TRIGGERS)
    ]


def item_name(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        named = cast(dict[str, object], entry)
        return str(named.get("id", named.get("name", index)))
    return str(index)


def leaves(value: object, path: tuple[str, ...]) -> list[Leaf]:
    if isinstance(value, dict):
        return [
            leaf
            for key, entry in cast(dict[str, object], value).items()
            for leaf in leaves(entry, (*path, str(key)))
        ]
    if isinstance(value, list):
        return [
            leaf
            for index, entry in enumerate(cast(list[object], value))
            for leaf in leaves(entry, (*path, item_name(entry, index)))
        ]
    return [(path, value)]


def workflow_leaves(workflows: Workflows) -> list[Leaf]:
    return [
        leaf
        for name, document in workflows.items()
        for leaf in leaves(document, (name,))
    ]


def environment_overrides(workflows: Workflows) -> list[str]:
    return [
        ":".join(path)
        for path, _ in workflow_leaves(workflows)
        if path[-1] == "env"
        or (
            len(path) > 1
            and path[-2] == "env"
            and FORBIDDEN_ENVIRONMENT.fullmatch(path[-1]) is not None
        )
    ]


def secret_sites(workflows: Workflows) -> list[str]:
    return [
        ":".join(path)
        for path, value in workflow_leaves(workflows)
        if path[-1] != "run" and SECRET_CONTEXT.search(str(value)) is not None
    ]


def unsafe_interpolations(workflows: Workflows) -> list[str]:
    in_scripts = [
        ":".join(path)
        for path, value in workflow_leaves(workflows)
        if path[-1] == "run"
        and (
            UNTRUSTED_CONTEXT.search(str(value)) is not None
            or SECRET_CONTEXT.search(str(value)) is not None
        )
    ]
    unlisted = [
        site for site in secret_sites(workflows) if site not in ALLOWED_SECRET_SITES
    ]
    return [*in_scripts, *unlisted]


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


def may_render_a_required_status(name: str) -> bool:
    parts = re.split(r"\$\{\{.*?\}\}", name)
    pattern = re.compile(".*".join(re.escape(part) for part in parts))
    return "${{" in name and any(
        pattern.fullmatch(required) for required in REQUIRED_STATUS_NAMES
    )


def needs_of(job: dict[str, object]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return [str(need) for need in cast(list[object], needs)]


def required_status_jobs(workflows: Workflows) -> dict[str, dict[str, object]]:
    return {
        location: job
        for location, job in jobs_of(workflows)
        if REQUIRED_STATUS_NAMES.intersection(status_names(job))
    }


def unguarded_required_statuses(workflows: Workflows) -> list[str]:
    produced = [
        (location, name)
        for location, job in jobs_of(workflows)
        for name in status_names(job)
    ]
    gating = required_status_jobs(workflows)
    names = [name for _, name in produced]
    return [
        *(
            f"{required}: produced {names.count(required)} times"
            for required in sorted(REQUIRED_STATUS_NAMES)
            if names.count(required) != 1
        ),
        *(
            f"{location}: unresolved name {name}"
            for location, name in produced
            if may_render_a_required_status(name)
        ),
        *(
            f"{location}: condition {job.get('if')}"
            for location, job in gating.items()
            if job.get("if") != GATING_JOB_CONDITIONS.get(location)
        ),
        *(
            f"{location}: needs without always()"
            for location, job in gating.items()
            if needs_of(job) and "always()" not in str(job.get("if", ""))
        ),
        *(
            f"{location}: continues on error"
            for location, job in gating.items()
            if "continue-on-error" in job
            or any("continue-on-error" in step for step in step_list(job))
        ),
    ]


def passing_value(expression: object) -> str:
    inner = str(expression).removeprefix("${{").removesuffix("}}").strip()
    if "==" in inner or "!=" in inner:
        return "false"
    if inner.endswith((".result", ".outcome")):
        return "success"
    if inner.endswith((".outputs.verified", ".outputs.full")):
        return "true"
    return ""


def recorded(output: Path) -> str:
    return output.read_text() if output.exists() else ""


def run_gate(
    gate: dict[str, object], values: dict[str, str], output: Path
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        name: value for name, value in os.environ.items() if name not in values
    }
    environment.update(values)
    environment["GITHUB_OUTPUT"] = str(output)
    return subprocess.run(
        ["/bin/bash", "-e", "-c", str(gate.get("run", ""))],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )


def matrix_scenarios(job: dict[str, object]) -> list[dict[str, str]]:
    conditions = " ".join(str(step.get("if", "")) for step in step_list(job))
    keys = sorted(set(MATRIX_REFERENCE.findall(conditions)))
    matrix = table(table(job.get("strategy", {})).get("matrix", {}))
    rows = [table(row) for row in cast(list[object], matrix.get("include", []))]
    scenarios: list[dict[str, str]] = []
    for row in rows or [{}]:
        scenario = {key: str(row.get(key, "")) for key in keys}
        if scenario not in scenarios:
            scenarios.append(scenario)
    return scenarios


def expected_outcome(step: dict[str, object], scenario: dict[str, str]) -> str:
    condition = str(step.get("if", ""))
    for key, value in scenario.items():
        admitted = re.findall(rf"matrix\.{re.escape(key)} == '([^']*)'", condition)
        if admitted and value not in admitted:
            return "skipped"
    return "success"


def matrix_context(job: dict[str, object], scenario: dict[str, str]) -> dict[str, str]:
    context: dict[str, str] = {}
    for name, expression in table(job.get("env", {})).items():
        reference = MATRIX_VALUE.fullmatch(str(expression))
        if reference is not None and reference[1] in scenario:
            context[str(name)] = scenario[reference[1]]
    return context


def passing_values(
    job: dict[str, object], gate: dict[str, object], scenario: dict[str, str]
) -> dict[str, str]:
    steps = {str(step["id"]): step for step in step_list(job) if "id" in step}
    values: dict[str, str] = {}
    for name, expression in table(gate.get("env", {})).items():
        inner = str(expression).removeprefix("${{").removesuffix("}}").strip()
        outcome = STEP_OUTCOME.fullmatch(inner)
        values[str(name)] = (
            expected_outcome(steps[outcome[1]], scenario)
            if outcome is not None and outcome[1] in steps
            else passing_value(expression)
        )
    return values


def misbehaving_gate(
    location: str, job: dict[str, object], gate: dict[str, object], workspace: Path
) -> list[str]:
    proof = "verified=true\n" if "verified" in table(job.get("outputs", {})) else ""
    violations: list[str] = []
    for scenario in matrix_scenarios(job):
        label = "-".join([location, *scenario.values()])
        context = matrix_context(job, scenario)
        passing = passing_values(job, gate, scenario)
        complete = workspace / f"{label}-complete"
        result = run_gate(gate, {**context, **passing}, complete)
        if result.returncode != 0 or recorded(complete) != proof:
            violations.append(f"{label}: refuses complete success")
        for name, value in passing.items():
            output = workspace / f"{label}-{name}"
            flipped = {**context, **passing, name: FLIPPED.get(value, "unexpected")}
            if run_gate(gate, flipped, output).returncode == 0 or recorded(output):
                violations.append(f"{label}: accepts a failed {name}")
    return violations


def ignored_inputs(
    location: str, job: dict[str, object], gate: dict[str, object]
) -> list[str]:
    declared = " ".join(str(value) for value in table(gate.get("env", {})).values())
    needs = needs_of(job) if "always()" in str(job.get("if", "")) else []
    references = [
        *(f"needs.{need}.result" for need in needs),
        *(
            f"steps.{step['id']}.outcome"
            for step in step_list(job)
            if "id" in step
            and step is not gate
            and f"{location}:{step['id']}" not in UNGATED_STEPS
        ),
    ]
    return [
        f"{location}: gate ignores {reference}"
        for reference in references
        if re.search(rf"(?<![\w.-]){re.escape(reference)}(?![\w-])", declared) is None
    ]


def unsound_gate(
    location: str, mode: str, job: dict[str, object], workspace: Path
) -> list[str]:
    gate = step_list(job)[-1]
    command = shlex.split(str(gate.get("run", "")).replace("\\\n", " "))
    violations = [
        *(
            [f"{location}: gate runs {gate.get('if')}"]
            if gate.get("if") != ALWAYS
            else []
        ),
        *(
            [f"{location}: gate runs {command[:3]}"]
            if command[:3] != ["bash", GATE_SCRIPT, mode]
            else []
        ),
        *(
            [f"{location}: continues on error"]
            if "continue-on-error" in job
            or any("continue-on-error" in step for step in step_list(job))
            else []
        ),
        *ignored_inputs(location, job, gate),
    ]
    return [*violations, *misbehaving_gate(location, job, gate, workspace)]


def unsound_gates(workflows: Workflows, workspace: Path) -> list[str]:
    jobs = dict(jobs_of(workflows))
    required = required_status_jobs(workflows)
    undeclared = [
        f"{location}: proof or required status without a gate"
        for location, job in jobs.items()
        if location not in GATE_JOBS
        and location not in PROOF_ORIGINS
        and (
            "verified" in table(job.get("outputs", {}))
            or (location in required and "always()" in str(job.get("if", "")))
        )
    ]
    missing = [f"{location}: missing" for location in GATE_JOBS if location not in jobs]
    return [
        *undeclared,
        *missing,
        *(
            violation
            for location, mode in GATE_JOBS.items()
            if location in jobs
            for violation in unsound_gate(location, mode, jobs[location], workspace)
        ),
    ]


def git_environment(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Workflow Test",
        "GIT_AUTHOR_EMAIL": "workflow@example.invalid",
        "GIT_COMMITTER_NAME": "Workflow Test",
        "GIT_COMMITTER_EMAIL": "workflow@example.invalid",
    }


def git_executable() -> str:
    executable = shutil.which("git")
    assert executable is not None
    return executable


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        [git_executable(), "-C", str(repository), *arguments],
        env=git_environment(repository.parent),
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout.decode().strip()


def commit_classifier(repository: Path, origin: str) -> str:
    program = repository / "scripts/ci/editorial_scope.py"
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text(CLASSIFIER_PROGRAM.format(origin=origin))
    git(repository, "add", "scripts/ci/editorial_scope.py")
    git(repository, "commit", "-q", "-m", origin)
    return git(repository, "rev-parse", "HEAD")


def classifier_repository(workspace: Path) -> tuple[Path, dict[str, str]]:
    repository = workspace / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    base = commit_classifier(repository, "base")
    head = commit_classifier(repository, "head")
    return repository, {
        "github.event.pull_request.base.sha": base,
        "github.event.pull_request.head.sha": head,
        "github.event_name": "pull_request",
    }


def classifier_step(workflows: Workflows) -> dict[str, object]:
    job = job_of(workflows, "ci.yml", "change-scope")
    return next(step for step in step_list(job) if step.get("id") == "scope")


def classification(workflows: Workflows, workspace: Path) -> str:
    repository, context = classifier_repository(workspace)
    step = classifier_step(workflows)
    output = workspace / "classification"
    declared = {
        str(name): context.get(
            str(value).removeprefix("${{").removesuffix("}}").strip(), ""
        )
        for name, value in table(step.get("env", {})).items()
    }
    subprocess.run(
        ["/bin/bash", "-e", "-c", str(step.get("run", ""))],
        cwd=repository,
        env={
            **git_environment(workspace),
            **declared,
            "GITHUB_OUTPUT": str(output),
            "TMPDIR": str(workspace),
        },
        capture_output=True,
        check=False,
        timeout=10,
    )
    return recorded(output)


def classifier_violations(workflows: Workflows, workspace: Path) -> list[str]:
    job = job_of(workflows, "ci.yml", "change-scope")
    output = classification(workflows, workspace)
    return [
        *(
            [f"ci.yml:change-scope: outputs {job.get('outputs')}"]
            if job.get("outputs") != CLASSIFIER_OUTPUTS
            else []
        ),
        *(["ci.yml:change-scope: conditional job"] if "if" in job else []),
        *(
            ["ci.yml:change-scope: conditional classifier"]
            if "if" in classifier_step(workflows)
            else []
        ),
        *(
            [f"ci.yml:change-scope: classified {output!r}"]
            if output != CLASSIFIED_FROM_BASE
            else []
        ),
    ]


def rewrite_run(step: dict[str, object], old: str, new: str) -> None:
    run = str(step["run"])
    assert old in run
    step["run"] = run.replace(old, new)


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


def cache_steps(job: dict[str, object]) -> list[dict[str, object]]:
    return [
        step
        for step in step_list(job)
        if "uses" in step and action_repository(str(step["uses"])) == "actions/cache"
    ]


def bound_cache(inputs: dict[str, object]) -> bool:
    key = inputs.get("key")
    return (
        {name: value for name, value in inputs.items() if name != "key"}
        == ACQUISITION_CACHE_INPUTS
        and isinstance(key, str)
        and ACQUISITION_CACHE_KEY.fullmatch(key) is not None
    )


def unbound_caches(workflows: Workflows) -> list[str]:
    return [
        f"{location}: {step.get('with')}"
        for location, job in jobs_of(workflows)
        for step in cache_steps(job)
        if not bound_cache(table(step.get("with", {})))
    ]


def links_cache_inputs(workflows: Workflows) -> dict[str, object]:
    return table(cache_steps(job_of(workflows, "ci.yml", "links"))[0]["with"])


def drop_the_manifest_digest_from_a_cache_key(workflows: Workflows) -> None:
    inputs = links_cache_inputs(workflows)
    inputs["key"] = "kora-acquisitions-${{ runner.os }}-${{ runner.arch }}-lychee"


def restore_a_prefix_matched_cache(workflows: Workflows) -> None:
    links_cache_inputs(workflows)["restore-keys"] = "kora-acquisitions-"


def cache_another_directory(workflows: Workflows) -> None:
    links_cache_inputs(workflows)["path"] = "~/.cache"


def share_a_cache_across_operating_systems(workflows: Workflows) -> None:
    links_cache_inputs(workflows)["enableCrossOsArchive"] = True


def restore_through_the_cache_subaction(workflows: Workflows) -> None:
    steps = cast(list[object], job_of(workflows, "ci.yml", "markdown")["steps"])
    commit = REVIEWED_ACTION_COMMITS["actions/cache"]
    steps.insert(
        1,
        {"uses": f"actions/cache/restore@{commit}", "with": {"path": "~", "key": "k"}},
    )


@pytest.mark.parametrize(
    "mutation",
    [
        drop_the_manifest_digest_from_a_cache_key,
        restore_a_prefix_matched_cache,
        cache_another_directory,
        share_a_cache_across_operating_systems,
        restore_through_the_cache_subaction,
    ],
)
def test_cache_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unbound_caches(workflows)

    assert violations


def test_caches_restore_only_the_acquisition_store_under_its_pinned_digest() -> None:
    workflows = repository_workflows()

    violations = unbound_caches(workflows)

    assert any(cache_steps(job) for _, job in jobs_of(workflows))
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


def let_a_daily_lane_write_issues(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "lifetime-tests")
    job["permissions"] = {"contents": "read", "issues": "write"}


def report_issues_from_any_branch(workflows: Workflows) -> None:
    job_of(workflows, "daily-lanes.yml", "report")["if"] = "${{ always() }}"


def report_issues_from_master_or_any_branch(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "report")
    job["if"] = "${{ always() || github.ref == 'refs/heads/master' }}"


def report_issues_from_every_branch_but_master(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "report")
    job["if"] = "${{ always() && !(github.ref == 'refs/heads/master') }}"


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
        let_a_daily_lane_write_issues,
        report_issues_from_any_branch,
        report_issues_from_master_or_any_branch,
        report_issues_from_every_branch_but_master,
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


def answer_issue_comments(workflows: Workflows) -> None:
    table(workflows["mutation.yml"]["on"])["issue_comment"] = {"types": ["created"]}


def accept_repository_dispatch(workflows: Workflows) -> None:
    table(workflows["conformance-lifetime.yml"]["on"])["repository_dispatch"] = None


@pytest.mark.parametrize(
    "mutation",
    [
        subscribe_to_pull_request_target,
        trigger_only_on_pull_request_target,
        list_pull_request_target,
        follow_another_workflow_run,
        answer_issue_comments,
        accept_repository_dispatch,
    ],
)
def test_trigger_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unreviewed_triggers(workflows)

    assert violations


def test_workflows_use_only_reviewed_triggers() -> None:
    workflows = repository_workflows()

    violations = unreviewed_triggers(workflows)

    assert violations == []


def add_pytest_options_to_a_step(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "ci.yml", "scaffold-tests"))[-1]
    table(step["env"])["PYTEST_ADDOPTS"] = "--deselect=tests"


def preload_a_library_for_a_job(workflows: Workflows) -> None:
    table(job_of(workflows, "ci.yml", "cel-producer")["env"])["LD_PRELOAD"] = "x.so"


def insert_a_library_for_a_workflow(workflows: Workflows) -> None:
    workflows["codeql.yml"]["env"] = {"DYLD_INSERT_LIBRARIES": "x.dylib"}


def source_a_shell_file_for_a_job(workflows: Workflows) -> None:
    job_of(workflows, "storage.yml", "storage-evidence")["env"] = {"BASH_ENV": "x"}


def compute_an_opaque_environment(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "markdown")["env"] = "${{ fromJSON(vars.OVERRIDES) }}"


@pytest.mark.parametrize(
    "mutation",
    [
        add_pytest_options_to_a_step,
        preload_a_library_for_a_job,
        insert_a_library_for_a_workflow,
        source_a_shell_file_for_a_job,
        compute_an_opaque_environment,
    ],
)
def test_environment_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = environment_overrides(workflows)

    assert violations


def test_no_workflow_environment_overrides_tests_or_command_lookup() -> None:
    workflows = repository_workflows()

    violations = environment_overrides(workflows)

    assert violations == []


def echo_the_pull_request_title(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "ci.yml", "links"))[-1]
    step["run"] = f"{step['run']}\necho '${{{{ github.event.pull_request.title }}}}'"


def check_out_the_head_branch_by_name(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "ci.yml", "markdown"))[-1]
    step["run"] = f"git switch ${{{{ github.head_ref }}}}\n{step['run']}"


def interpolate_a_dispatch_input(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "mutation.yml", "audit"))[-1]
    step["run"] = f"{step['run']} ${{{{ inputs.report }}}}"


def print_the_token(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "gitleaks.yml", "scan"))[1]
    step["run"] = "echo ${{ github.token }}"


def pass_a_secret_through_the_environment(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "markdown")["env"] = {
        "PUBLISH_TOKEN": "${{ secrets.PUBLISH_TOKEN }}"
    }


def hand_the_token_to_an_action(workflows: Workflows) -> None:
    step = step_list(job_of(workflows, "codeql.yml", "analyze"))[0]
    table(step["with"])[CHECKOUT_AUTHENTICATION_INPUT] = "${{ github.token }}"


@pytest.mark.parametrize(
    "mutation",
    [
        echo_the_pull_request_title,
        check_out_the_head_branch_by_name,
        interpolate_a_dispatch_input,
        print_the_token,
        pass_a_secret_through_the_environment,
        hand_the_token_to_an_action,
    ],
)
def test_interpolation_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unsafe_interpolations(workflows)

    assert violations


def test_scripts_interpolate_no_untrusted_or_secret_context() -> None:
    workflows = repository_workflows()

    violations = unsafe_interpolations(workflows)

    assert violations == []
    assert set(secret_sites(workflows)) == ALLOWED_SECRET_SITES


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


def duplicate_a_scaffold_status_through_a_product_matrix(workflows: Workflows) -> None:
    jobs = table(workflows["ci.yml"]["jobs"])
    jobs["scaffold-copy"] = {
        "name": "Scaffold (${{ matrix.os }}, Python ${{ matrix.python }})",
        "runs-on": "ubuntu-24.04",
        "strategy": {"matrix": {"os": ["macos-15"], "python": ["3.11.17"]}},
        "steps": [{"run": "true"}],
    }


def skip_the_browser_status_after_a_failed_need(workflows: Workflows) -> None:
    del job_of(workflows, "ci.yml", "browser")["if"]


def condition_the_secret_scan(workflows: Workflows) -> None:
    job_of(workflows, "gitleaks.yml", "scan")["if"] = "github.event_name == 'push'"


def let_the_cel_status_continue_on_error(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "cel-measurements")["continue-on-error"] = True


def let_a_secret_scan_step_continue_on_error(workflows: Workflows) -> None:
    step_list(job_of(workflows, "gitleaks.yml", "scan"))[2]["continue-on-error"] = True


def make_markdown_lint_wait_for_another_job(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "markdown")["needs"] = ["change-scope"]


@pytest.mark.parametrize(
    "mutation",
    [
        rename_the_secret_scan,
        drop_a_scaffold_join_row,
        drop_the_pull_request_dependency_scan,
        duplicate_the_browser_status,
        duplicate_a_scaffold_status_through_a_product_matrix,
        skip_the_browser_status_after_a_failed_need,
        condition_the_secret_scan,
        let_the_cel_status_continue_on_error,
        let_a_secret_scan_step_continue_on_error,
        make_markdown_lint_wait_for_another_job,
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


def replace_a_gate_with_success(workflows: Workflows) -> None:
    step_list(job_of(workflows, "ci.yml", "scaffold"))[-1]["run"] = "exit 0"


def drop_the_scaffold_proof_requirement(workflows: Workflows) -> None:
    gate = step_list(job_of(workflows, "ci.yml", "scaffold"))[-1]
    rewrite_run(gate, " \\\n  EVIDENCE_VERIFIED=true", "")


def drop_a_producer_outcome_requirement(workflows: Workflows) -> None:
    gate = step_list(job_of(workflows, "daily-lanes.yml", "pricing-measurements"))[-1]
    rewrite_run(gate, "  PRICING_UPLOAD_OUTCOME=success \\\n", "")


def disable_a_gate(workflows: Workflows) -> None:
    step_list(job_of(workflows, "storage.yml", "storage-evidence"))[-1]["if"] = "false"


def let_a_measurement_continue_on_error(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "resource-measurements")
    step = next(step for step in step_list(job) if step.get("id") == "measurement")
    step["continue-on-error"] = True


def add_an_ungated_step(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "git-publication-measurements")
    steps = cast(list[object], job["steps"])
    steps.insert(len(steps) - 1, {"id": "extra_measurement", "run": "true"})


def ignore_a_needed_job(workflows: Workflows) -> None:
    gate = step_list(job_of(workflows, "ci.yml", "cel-measurements"))[-1]
    del table(gate["env"])["CEL_RESULT"]


def publish_an_ungated_proof(workflows: Workflows) -> None:
    job = job_of(workflows, "daily-lanes.yml", "ir-measurements")
    job["outputs"] = {"verified": "${{ steps.measurement.outcome == 'success' }}"}


def replace_the_lane_gate_with_success(workflows: Workflows) -> None:
    step_list(job_of(workflows, "ci.yml", "scaffold-tests"))[-1]["run"] = "exit 0"


def drop_the_static_check_outcome(workflows: Workflows) -> None:
    gate = step_list(job_of(workflows, "ci.yml", "scaffold-tests"))[-1]
    del table(gate["env"])["STATIC_OUTCOME"]


def skip_the_static_checks(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "scaffold-tests")
    step = next(step for step in step_list(job) if step.get("id") == "static_checks")
    step["if"] = "false"


def wait_for_a_lifetime_shard_without_its_result(workflows: Workflows) -> None:
    job = job_of(workflows, "conformance-lifetime.yml", "lifetime-evidence")
    job["needs"] = [*needs_of(job), "storage-probe"]


@pytest.mark.parametrize(
    "mutation",
    [
        replace_a_gate_with_success,
        drop_the_scaffold_proof_requirement,
        drop_a_producer_outcome_requirement,
        disable_a_gate,
        let_a_measurement_continue_on_error,
        add_an_ungated_step,
        ignore_a_needed_job,
        publish_an_ungated_proof,
        wait_for_a_lifetime_shard_without_its_result,
        replace_the_lane_gate_with_success,
        drop_the_static_check_outcome,
        skip_the_static_checks,
    ],
)
def test_gate_check_refuses_a_mutated_workflow(
    tmp_path: Path, mutation: Mutation
) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = unsound_gates(workflows, tmp_path)

    assert violations


def test_gates_require_every_need_and_step_and_fail_on_each_input(
    tmp_path: Path,
) -> None:
    workflows = repository_workflows()

    violations = unsound_gates(workflows, tmp_path)

    assert violations == []


def point_the_classifier_at_the_head(workflows: Workflows) -> None:
    step = classifier_step(workflows)
    table(step["env"])["KORA_BASE_REVISION"] = (
        "${{ github.event.pull_request.head.sha }}"
    )


def run_the_classifier_from_the_checkout(workflows: Workflows) -> None:
    rewrite_run(
        classifier_step(workflows),
        'git show "$KORA_BASE_REVISION:scripts/ci/editorial_scope.py"',
        "cat scripts/ci/editorial_scope.py",
    )


def publish_a_literal_editorial_scope(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "change-scope")
    table(job["outputs"])["full"] = "false"


def condition_the_classifier(workflows: Workflows) -> None:
    classifier_step(workflows)["if"] = "github.event_name == 'pull_request'"


def condition_the_scope_job(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "change-scope")["if"] = "github.actor != 'renovate'"


@pytest.mark.parametrize(
    "mutation",
    [
        point_the_classifier_at_the_head,
        run_the_classifier_from_the_checkout,
        publish_a_literal_editorial_scope,
        condition_the_classifier,
        condition_the_scope_job,
    ],
)
def test_classifier_check_refuses_a_mutated_workflow(
    tmp_path: Path, mutation: Mutation
) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = classifier_violations(workflows, tmp_path)

    assert violations


def test_change_scope_runs_the_classifier_of_the_base_revision(tmp_path: Path) -> None:
    workflows = repository_workflows()

    violations = classifier_violations(workflows, tmp_path)

    assert violations == []
