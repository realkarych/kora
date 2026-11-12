import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
    lane_command,
    lane_of,
    ordered,
    process_execution,
    pull_request_changes,
    repetition,
    selection,
    selection_outputs,
    summary,
)
from tests.scaffold_evidence import scheduled_modules
from tests.support.workflows import job_of, repository_workflows, step_list

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
FIXTURE_CONFIGURATION = "[pytest]\nmarkers =\n    measurement: fixture marker\n"
PULL_REQUEST_ONLY = "github.event_name == 'pull_request'"


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


@pytest.mark.parametrize(
    ("path", "lane", "launcher"),
    [
        ("tests/plugins/runtime/local/test_command.py", "local-runtime", "bash"),
        ("tests/plugins/storage/test_layout.py", "storage", "uv"),
        ("tests/conformance/test_installed_conformance.py", "storage", "uv"),
        ("tests/conformance/test_installed_listing.py", "general", "uv"),
        ("tests/plugins/runtime/local_peers/test_peer.py", "general", "uv"),
        ("tests/ir/test_graph.py", "general", "uv"),
    ],
)
def test_modules_run_as_their_ordinary_lane_runs_them(
    path: str, lane: str, launcher: str
) -> None:
    command = lane_command(path)

    assert lane_of(path) == lane
    assert command[0] == launcher
    assert command[-1] == path
    assert ("scripts/ci/run-local-runtime-tests.sh" in command) is (
        lane == "local-runtime"
    )
    assert ("tests.support.ordinary_pytest" in command) is (lane == "general")


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


def test_selection_outputs_name_the_count_and_the_host_preparation() -> None:
    general = Module("tests/ir/test_graph.py", False, 1.0)
    local = Module("tests/plugins/runtime/local/test_command.py", True, 1.0)

    outputs = [
        selection_outputs([]),
        selection_outputs([general]),
        selection_outputs([general, local]),
    ]

    assert outputs == [
        "modules=0\nlocal_runtime=false\n",
        "modules=1\nlocal_runtime=false\n",
        "modules=2\nlocal_runtime=true\n",
    ]


def test_the_report_lists_executions_and_modules_that_were_not_repeated() -> None:
    report = Report({"tests/test_new.py": 12, "tests/test_large.py": 0}, None, "")

    rendered = summary(report)
    emitted = annotations(report)

    assert "2 changed test modules, 12 executions." in rendered
    assert "| `tests/test_new.py` | 12 |" in rendered
    assert "| `tests/test_large.py` | 0 |" in rendered
    assert emitted.count("::warning ") == 1
    assert "tests/test_large.py was not repeated" in emitted


def test_the_report_of_a_failure_names_the_module_as_an_error() -> None:
    report = Report(
        {"tests/test_new.py": 3, "tests/test_later.py": 0}, "tests/test_new.py", ""
    )

    rendered = summary(report)
    emitted = annotations(report)

    assert "An execution of `tests/test_new.py` failed." in rendered
    assert emitted.startswith("::error ")
    assert "::warning " not in emitted


def test_the_repetition_job_always_reports_a_result_to_the_scaffold_join() -> None:
    workflows = repository_workflows()
    job = job_of(workflows, "ci.yml", "changed-test-repetition")
    join = job_of(workflows, "ci.yml", "scaffold")

    conditions = [str(step.get("if", "")) for step in step_list(job)]

    assert "changed-test-repetition" in str(join["needs"])
    assert "if" not in job
    assert "needs" not in job
    assert "continue-on-error" not in job
    assert (job["runs-on"], job["timeout-minutes"]) == ("ubuntu-24.04", 10)
    assert all(PULL_REQUEST_ONLY in condition for condition in conditions)
