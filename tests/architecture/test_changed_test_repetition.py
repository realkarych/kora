import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from scripts.ci.lane_plan import Record
from tests.changed_test_repetition import (
    Limits,
    Module,
    Outcome,
    RepetitionFailure,
    Report,
    annotations,
    changed_test_modules,
    collected_modules,
    lane_command,
    lane_of,
    ordered,
    process_execution,
    pull_request_changes,
    repetition,
    selection,
    summary,
)
from tests.scaffold_evidence import scheduled_modules
from tests.support.workflows import (
    Workflows,
    action_name,
    job_of,
    repository_workflows,
    step_list,
)

ROOT = Path(__file__).resolve().parents[2]

PASSING_TEST = "def test_passes() -> None:\n    assert True\n"
MEASURED_TEST = (
    "import pytest\n\n"
    "pytestmark = pytest.mark.measurement\n\n\n"
    "def test_measures() -> None:\n    assert True\n"
)
THIRD_INVOCATION_FAILS = (
    "from pathlib import Path\n\n\n"
    "def test_counts_invocations() -> None:\n"
    "    counter = Path('invocations')\n"
    "    count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
    "    counter.write_text(str(count))\n"
    "    assert count != 3\n"
)
FIXTURE_CONFIGURATION = (
    "[pytest]\n"
    'addopts = -m "not measurement"\n'
    "markers =\n    measurement: fixture marker\n"
)
PULL_REQUEST_ONLY = "${{ github.event_name == 'pull_request' }}"
REPETITION_COMMAND = "uv run --no-sync python -m tests.changed_test_repetition repeat"
REPETITION_STEP_KEYS = frozenset({"name", "id", "if", "run"})
REPETITION_JOB_SETTINGS = {
    "defaults": {"run": {"shell": "bash"}},
    "env": {
        "KORA_TEST_DATABASES": "sqlite,postgresql",
        "TESTCONTAINERS_RYUK_DISABLED": "true",
    },
}
REPETITION_JOB_ACTIONS = (
    "actions/checkout",
    "actions/cache",
    "actions/setup-node",
    "pnpm/action-setup",
    "astral-sh/setup-uv",
)
REPETITION_JOB_COMMANDS = (
    "bash scripts/ci/setup_scaffold_node.sh",
    "bash scripts/ci/setup_uv_cache.sh 3.11.17",
    "bash scripts/ci/scaffold_lane.sh dependencies",
    "pnpm --dir web install --frozen-lockfile",
    REPETITION_COMMAND,
)
Mutation = Callable[[Workflows], None]


def git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    completed = subprocess.run(
        [
            executable,
            "-C",
            str(repository),
            "-c",
            "user.name=Repetition Test",
            "-c",
            "user.email=repetition@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        capture_output=True,
        check=True,
        timeout=10,
    )
    return completed.stdout.decode().strip()


def write(repository: Path, path: str, content: str) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "--all")
    git(repository, "commit", "-q", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def initialized(workspace: Path) -> Path:
    repository = workspace / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "--initial-branch=base")
    return repository


def fixture_launcher(path: str) -> tuple[str, ...]:
    return (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path)


def fixture_project(workspace: Path, modules: dict[str, str]) -> Path:
    project = workspace / "project"
    project.mkdir()
    write(project, "pytest.ini", FIXTURE_CONFIGURATION)
    for path, content in modules.items():
        write(project, path, content)
    return project


class Timeline:
    def __init__(
        self, durations: dict[str, float], failing: tuple[str, int] | None
    ) -> None:
        self.now = 0.0
        self.calls: list[str] = []
        self.durations = durations
        self.failing = failing

    def clock(self) -> float:
        return self.now

    def execute(self, module: Module) -> Outcome:
        self.calls.append(module.path)
        self.now += self.durations[module.path]
        failed = self.failing == (module.path, self.calls.count(module.path))
        return Outcome(not failed, "recorded failure" if failed else "")


def test_selection_takes_added_modified_and_renamed_test_modules(
    tmp_path: Path,
) -> None:
    repository = initialized(tmp_path)
    for name in ("modified", "renamed", "deleted", "untouched"):
        write(repository, f"tests/test_{name}.py", f"{PASSING_TEST}\nNAME = '{name}'\n")
    write(repository, "tests/support/helper.py", "VALUE = 1\n")
    write(repository, "src/module.py", "VALUE = 1\n")
    base = commit(repository, "base")
    write(repository, "tests/test_added.py", PASSING_TEST)
    write(repository, "tests/nested/test_added_below.py", PASSING_TEST)
    write(repository, "tests/test_modified.py", f"{PASSING_TEST}\nNAME = 'changed'\n")
    git(repository, "mv", "tests/test_renamed.py", "tests/test_moved.py")
    git(repository, "rm", "-q", "tests/test_deleted.py")
    write(repository, "tests/support/helper.py", "VALUE = 2\n")
    write(repository, "tests/conftest.py", "VALUE = 2\n")
    write(repository, "tests/test_notes.txt", "notes\n")
    write(repository, "tests/nested/checks.py", PASSING_TEST)
    write(repository, "scripts/test_outside.py", PASSING_TEST)
    write(repository, "src/module.py", "VALUE = 2\n")
    head = commit(repository, "head")

    changes = changed_test_modules(repository, base, head)

    assert changes == {
        "tests/nested/test_added_below.py": True,
        "tests/test_added.py": True,
        "tests/test_modified.py": False,
        "tests/test_moved.py": False,
    }


def test_pull_request_changes_compare_the_merge_commit_with_its_base(
    tmp_path: Path,
) -> None:
    repository = initialized(tmp_path)
    write(repository, "tests/test_existing.py", PASSING_TEST)
    commit(repository, "base")
    git(repository, "checkout", "-q", "-b", "topic")
    write(repository, "tests/test_topic.py", PASSING_TEST)
    commit(repository, "topic")
    git(repository, "checkout", "-q", "base")
    write(repository, "tests/test_base_only.py", PASSING_TEST)
    commit(repository, "base moved")
    git(repository, "merge", "-q", "--no-ff", "-m", "merge", "topic")

    changes = pull_request_changes(repository)

    assert changes == {"tests/test_topic.py": True}


def test_pull_request_changes_refuse_a_checkout_without_a_merge_commit(
    tmp_path: Path,
) -> None:
    repository = initialized(tmp_path)
    write(repository, "tests/test_existing.py", PASSING_TEST)
    commit(repository, "base")

    with pytest.raises(RepetitionFailure):
        pull_request_changes(repository)


def test_changed_test_modules_refuse_an_unusual_path(tmp_path: Path) -> None:
    repository = initialized(tmp_path)
    write(repository, "tests/test_existing.py", PASSING_TEST)
    base = commit(repository, "base")
    write(repository, "tests/test_with space.py", PASSING_TEST)
    head = commit(repository, "head")

    with pytest.raises(RepetitionFailure):
        changed_test_modules(repository, base, head)


@pytest.mark.parametrize(
    ("path", "lane", "command"),
    [
        (
            "tests/plugins/runtime/local/test_command.py",
            "local-runtime",
            ("bash", "scripts/ci/run-local-runtime-tests.sh", "uv", "run", "--no-sync")
            + ("pytest", "-q", "tests/plugins/runtime/local/test_command.py"),
        ),
        (
            "tests/plugins/storage/test_layout.py",
            "storage",
            ("uv", "run", "--no-sync", "pytest", "-q")
            + ("tests/plugins/storage/test_layout.py",),
        ),
        (
            "tests/conformance/test_installed_conformance.py",
            "storage",
            ("uv", "run", "--no-sync", "pytest", "-q")
            + ("tests/conformance/test_installed_conformance.py",),
        ),
        (
            "tests/plugins/runtime/local_peers/test_peer.py",
            "general",
            ("uv", "run", "--no-sync", "python", "-m", "tests.support.ordinary_pytest")
            + ("-q", "tests/plugins/runtime/local_peers/test_peer.py"),
        ),
        (
            "tests/ir/test_graph.py",
            "general",
            ("uv", "run", "--no-sync", "python", "-m", "tests.support.ordinary_pytest")
            + ("-q", "tests/ir/test_graph.py"),
        ),
    ],
)
def test_modules_run_with_the_launcher_of_their_lane_and_the_default_selection(
    path: str, lane: str, command: tuple[str, ...]
) -> None:
    launched = lane_command(path)

    assert lane_of(path) == lane
    assert launched == command


def test_selection_keeps_only_modules_the_ordinary_linux_lanes_collect(
    tmp_path: Path,
) -> None:
    scheduled = scheduled_modules("ubuntu-24.04")[0]
    project = fixture_project(
        tmp_path,
        {
            "tests/test_ordinary.py": PASSING_TEST,
            "tests/test_measured.py": MEASURED_TEST,
            scheduled: PASSING_TEST,
        },
    )
    changes = {
        "tests/test_measured.py": True,
        "tests/test_ordinary.py": False,
        scheduled: False,
    }
    durations = {"tests/test_ordinary.py": Record(4000, 1)}

    modules = selection(changes, durations, (sys.executable, "-m", "pytest"), project)

    assert modules == (Module("tests/test_ordinary.py", False, 4.0),)


def test_selection_of_only_uncollected_modules_is_empty(tmp_path: Path) -> None:
    project = fixture_project(tmp_path, {"tests/test_measured.py": MEASURED_TEST})
    durations = {"tests/test_other.py": Record(1000, 1)}

    modules = selection(
        {"tests/test_measured.py": True},
        durations,
        (sys.executable, "-m", "pytest"),
        project,
    )

    assert modules == ()


def test_selection_refuses_modules_that_cannot_be_collected(tmp_path: Path) -> None:
    project = fixture_project(tmp_path, {"tests/test_broken.py": "def test_broken(:\n"})
    durations = {"tests/test_other.py": Record(1000, 1)}

    with pytest.raises(RepetitionFailure):
        selection(
            {"tests/test_broken.py": True},
            durations,
            (sys.executable, "-m", "pytest"),
            project,
        )


def test_added_modules_come_first_and_modified_ones_by_recorded_duration() -> None:
    durations = {
        "tests/test_slow.py": Record(9000, 3),
        "tests/test_middle.py": Record(3000, 1),
        "tests/test_fast.py": Record(1000, 1),
    }
    changes = {
        "tests/test_slow.py": False,
        "tests/test_fast.py": False,
        "tests/test_unrecorded.py": False,
        "tests/test_new.py": True,
    }

    modules = ordered(changes, durations)

    assert modules == (
        Module("tests/test_new.py", True, 3.0),
        Module("tests/test_fast.py", False, 1.0),
        Module("tests/test_unrecorded.py", False, 3.0),
        Module("tests/test_slow.py", False, 9.0),
    )


@pytest.mark.parametrize(
    ("duration", "executions"),
    [(1.0, 40), (7.5, 40), (7.6, 39), (100.0, 3), (100.5, 2), (300.0, 1), (400.0, 1)],
)
def test_no_execution_starts_that_would_end_after_the_window(
    duration: float, executions: int
) -> None:
    module = Module("tests/test_changed.py", True, 1.0)
    timeline = Timeline({module.path: duration}, None)

    report = repetition([module], timeline.execute, timeline.clock, Limits())

    assert report == Report({module.path: executions}, None, "")


@pytest.mark.parametrize(("estimate", "executions"), [(300.0, 1), (300.5, 0)])
def test_a_module_whose_estimate_exceeds_the_window_is_not_repeated(
    estimate: float, executions: int
) -> None:
    oversized = Module("tests/test_oversized.py", False, estimate)
    small = Module("tests/test_small.py", True, 1.0)
    timeline = Timeline({oversized.path: 10.0, small.path: 30.0}, None)

    report = repetition(
        [oversized, small], timeline.execute, timeline.clock, Limits(rounds=1)
    )

    assert report.executions == {oversized.path: executions, small.path: 1}
    assert report.failed is None


def test_a_module_slower_than_its_estimate_yields_the_window_to_the_others() -> None:
    slow = Module("tests/test_slow.py", True, 1.0)
    quick = Module("tests/test_quick.py", False, 1.0)
    timeline = Timeline({slow.path: 200.0, quick.path: 10.0}, None)

    report = repetition([slow, quick], timeline.execute, timeline.clock, Limits())

    assert report.executions == {slow.path: 1, quick.path: 10}
    assert timeline.calls == [slow.path, *[quick.path] * 10]


def test_modules_alternate_in_selection_order() -> None:
    first = Module("tests/test_first.py", True, 1.0)
    second = Module("tests/test_second.py", False, 1.0)
    timeline = Timeline({first.path: 1.0, second.path: 1.0}, None)

    repetition([first, second], timeline.execute, timeline.clock, Limits(rounds=3))

    assert timeline.calls == [first.path, second.path] * 3


def test_the_first_failed_execution_ends_the_repetition() -> None:
    first = Module("tests/test_first.py", True, 1.0)
    second = Module("tests/test_second.py", False, 1.0)
    timeline = Timeline({first.path: 1.0, second.path: 1.0}, (second.path, 2))

    report = repetition([first, second], timeline.execute, timeline.clock, Limits())

    assert report == Report(
        {first.path: 2, second.path: 2}, second.path, "recorded failure"
    )
    assert timeline.calls == [first.path, second.path] * 2


def test_a_failing_invocation_is_reported_and_never_executed_again(
    tmp_path: Path,
) -> None:
    project = fixture_project(
        tmp_path, {"tests/test_counted.py": THIRD_INVOCATION_FAILS}
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    module = Module("tests/test_counted.py", True, 1.0)
    execute = process_execution(fixture_launcher, project, logs)

    report = repetition([module], execute, time.monotonic, Limits(rounds=6))

    assert report.failed == module.path
    assert report.executions == {module.path: 3}
    assert "test_counts_invocations" in report.output
    assert (project / "invocations").read_text() == "3"
    assert len(list(logs.iterdir())) == 3


def test_passing_modules_run_in_a_fresh_process_every_round(tmp_path: Path) -> None:
    project = fixture_project(tmp_path, {"tests/test_passing.py": PASSING_TEST})
    logs = tmp_path / "logs"
    logs.mkdir()
    module = Module("tests/test_passing.py", False, 1.0)
    execute = process_execution(fixture_launcher, project, logs)

    report = repetition([module], execute, time.monotonic, Limits(rounds=4))

    assert report == Report({module.path: 4}, None, "")
    assert len(list(logs.iterdir())) == 4


def test_the_collection_reader_refuses_a_listing_without_the_changed_modules(
    tmp_path: Path,
) -> None:
    silent_collector = (sys.executable, "-c", "pass")

    with pytest.raises(RepetitionFailure):
        collected_modules(["tests/test_changed.py"], silent_collector, tmp_path)


def test_the_collection_reader_reads_the_repository_configuration() -> None:
    module = "tests/architecture/test_ci_gates.py"

    collected = collected_modules([module], (sys.executable, "-m", "pytest"), ROOT)

    assert collected == frozenset({module})


def test_the_report_lists_executions_and_modules_that_were_not_repeated() -> None:
    report = Report({"tests/test_new.py": 12, "tests/test_large.py": 0}, None, "")

    rendered = summary(report, ["tests/test_daily.py"])
    emitted = annotations(report)

    assert "2 selected test modules, 12 executions." in rendered
    assert "| `tests/test_new.py` | 12 |" in rendered
    assert "| `tests/test_large.py` | 0 |" in rendered
    assert "- `tests/test_daily.py`" in rendered
    assert emitted.count("::warning ") == 1
    assert "tests/test_large.py was not repeated" in emitted


def test_the_report_states_an_empty_selection() -> None:
    report = Report({}, None, "")

    rendered = summary(report, [])
    emitted = annotations(report)

    assert "0 selected test modules, 0 executions." in rendered
    assert "| Module |" not in rendered
    assert emitted == ""


def test_the_report_of_a_failure_names_the_module_as_an_error() -> None:
    report = Report(
        {"tests/test_new.py": 3, "tests/test_later.py": 0}, "tests/test_new.py", ""
    )

    rendered = summary(report, [])
    emitted = annotations(report)

    assert "An execution of `tests/test_new.py` failed." in rendered
    assert emitted.startswith("::error ")
    assert "::warning " not in emitted


def repetition_job_violations(workflows: Workflows) -> list[str]:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    join = job_of(workflows, "ci.yml", "scaffold")
    steps = step_list(job)
    return [
        *(
            f"job declares {key}"
            for key in ("if", "needs", "continue-on-error")
            if key in job
        ),
        *(
            ["job leaves its runner or deadline"]
            if (job.get("runs-on"), job.get("timeout-minutes")) != ("ubuntu-24.04", 10)
            else []
        ),
        *(
            f"step {index} runs under {step.get('if')}"
            for index, step in enumerate(steps)
            if step.get("if") != PULL_REQUEST_ONLY
        ),
        *(
            f"step {index} continues on error"
            for index, step in enumerate(steps)
            if "continue-on-error" in step
        ),
        *(
            ["the last step does not repeat the changed test modules"]
            if not steps
            or steps[-1].get("run") != REPETITION_COMMAND
            or frozenset(steps[-1]) != REPETITION_STEP_KEYS
            else []
        ),
        *(
            f"job changes its {key}"
            for key, expected in REPETITION_JOB_SETTINGS.items()
            if job.get(key) != expected
        ),
        *(
            ["job uses other actions"]
            if tuple(action_name(step["uses"]) for step in steps if "uses" in step)
            != REPETITION_JOB_ACTIONS
            else []
        ),
        *(
            ["job runs other commands"]
            if tuple(step["run"] for step in steps if "run" in step)
            != REPETITION_JOB_COMMANDS
            else []
        ),
        *(
            ["the scaffold join does not wait for the repetition"]
            if "changed-test-repetition"
            not in [str(need) for need in list_of(join["needs"])]
            else []
        ),
    ]


def list_of(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def repetition_step(workflows: Workflows) -> dict[str, object]:
    return step_list(job_of(workflows, "ci.yml", "changed-test-repetition"))[-1]


def let_a_failed_repetition_pass(workflows: Workflows) -> None:
    repetition_step(workflows)["continue-on-error"] = True


def disable_the_repetition(workflows: Workflows) -> None:
    repetition_step(workflows)["if"] = (
        "${{ github.event_name == 'pull_request' && false }}"
    )


def replace_the_repetition_command(workflows: Workflows) -> None:
    repetition_step(workflows)["run"] = "true"


def append_a_step_after_the_repetition(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    list_of(job["steps"]).append({"if": PULL_REQUEST_ONLY, "run": "true"})


def run_the_repetition_under_another_shell(workflows: Workflows) -> None:
    repetition_step(workflows)["shell"] = "true {0}"


def run_the_job_under_another_shell(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    job["defaults"] = {"run": {"shell": "true {0}"}}


def drop_a_database_from_the_job(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    job["env"] = {**REPETITION_JOB_SETTINGS["env"], "KORA_TEST_DATABASES": "sqlite"}


def insert_a_command_before_the_repetition(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    list_of(job["steps"]).insert(-1, {"if": PULL_REQUEST_ONLY, "run": "true"})


def skip_the_job_on_master_pushes(workflows: Workflows) -> None:
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    job["if"] = "${{ github.event_name == 'pull_request' }}"


def make_the_job_wait_for_the_scope(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "changed-test-repetition")["needs"] = ["change-scope"]


def let_the_job_continue_on_error(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "changed-test-repetition")["continue-on-error"] = True


def run_a_setup_step_on_master_pushes(workflows: Workflows) -> None:
    del step_list(job_of(workflows, "ci.yml", "changed-test-repetition"))[0]["if"]


def move_the_job_to_another_runner(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "changed-test-repetition")["runs-on"] = "macos-15"


def detach_the_scaffold_join(workflows: Workflows) -> None:
    job_of(workflows, "ci.yml", "scaffold")["needs"] = ["scaffold-evidence"]


@pytest.mark.parametrize(
    "mutation",
    [
        let_a_failed_repetition_pass,
        disable_the_repetition,
        replace_the_repetition_command,
        append_a_step_after_the_repetition,
        run_the_repetition_under_another_shell,
        run_the_job_under_another_shell,
        drop_a_database_from_the_job,
        insert_a_command_before_the_repetition,
        skip_the_job_on_master_pushes,
        make_the_job_wait_for_the_scope,
        let_the_job_continue_on_error,
        run_a_setup_step_on_master_pushes,
        move_the_job_to_another_runner,
        detach_the_scaffold_join,
    ],
)
def test_repetition_job_check_refuses_a_mutated_workflow(mutation: Mutation) -> None:
    workflows = repository_workflows()
    mutation(workflows)

    violations = repetition_job_violations(workflows)

    assert violations


def test_the_repetition_job_always_runs_and_fails_with_its_repetition() -> None:
    workflows = repository_workflows()

    violations = repetition_job_violations(workflows)

    assert violations == []
