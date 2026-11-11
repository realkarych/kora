import os
import select
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import pytest

from tests.measurements.storage_evidence_common import EvidenceFailure, junit_nodes
from tests.scaffold_evidence import (
    COHORTS,
    GENERAL_ARGUMENTS,
    SCHEDULED_LIFETIME_MODULES,
)

ROOT = Path(__file__).resolve().parents[2]
COHORT_SCRIPT = ROOT / "scripts/ci/ordinary_cohorts.sh"
LANE_SCRIPT = ROOT / "scripts/ci/scaffold_lane.sh"
IR_RESOURCE_MODULES = frozenset(
    {
        "tests/ir/test_approval_predicate_resources.py",
        "tests/ir/test_export_type_resources.py",
        "tests/ir/test_fan_out_type_resources.py",
        "tests/ir/test_inline_schema_resources.py",
        "tests/ir/test_type_schema_resources.py",
        "tests/ir/test_output_schema_resources.py",
    }
)
INTEGRATION_SELECTION = (
    "tests/api",
    "tests/cli",
    "tests/ir/test_structured_schema_string_lengths.py",
)
FIRST_MODULES = (
    "tests/architecture/test_ir_measurement_ci.py",
    "tests/architecture/test_no_suppression.py",
    "tests/conformance/test_local_process_lifetime.py",
    "tests/ir/test_structured_schema_assertions.py",
    "tests/measurements/test_ir_measurement_evidence.py",
    "tests/ir/test_input_path_resources.py",
    "tests/ir/test_expression_syntax_resources.py",
    "tests/ir/test_edge_type_resources.py",
    "tests/ir/test_approval_predicate_resources.py",
    "tests/engine/test_graph_output_inventory.py",
    "tests/ir/test_structured_schema_collection_predicates.py",
    "tests/engine/test_graph_outputs.py",
    "tests/engine/test_graph_native_outputs.py",
)
THIRD_MODULES = (
    "tests/conformance/test_conformance_cases_detect_broken_providers.py",
    "tests/conformance/test_parent_lifetime.py",
    "tests/conformance/test_recovered_process_lifetime.py",
    "tests/engine/test_graph_projection_paths.py",
    "tests/conformance/test_storage_case_runner.py",
)
ENGINE_MODULES = (
    "tests/engine/test_graph_execution_contract.py",
    "tests/engine/test_start_parameter_cut.py",
    "tests/browser/test_release_wait.py",
    "tests/engine/test_graph_native_sources.py",
    "tests/ir/test_tool_policy_resources.py",
    "tests/ir/test_type_schema_resources.py",
    "tests/db/test_pricing_migration.py",
    "tests/db/test_attempt_outcome_migration.py",
    "tests/db/test_db_upgrade_process.py",
    "tests/db/test_attempt_deadline_migration.py",
)
SURFACE_MODULES = (
    "tests/cli/test_conformance.py",
    "tests/api/test_graph_execution_preparation.py",
)


EXPECTED_ORDINARY = {
    ("ubuntu-24.04", "conformance"): ("first", "second", "third"),
    ("ubuntu-24.04", "remainder"): ("measurements", "other", "graph", "engine"),
    ("ubuntu-24.04", "integration"): ("integration", "surface"),
    ("macos-15", "conformance"): ("first", "second", "third", "surface"),
    ("macos-15", "remainder"): ("measurements", "integration", "other"),
}


def shard_evidence(children: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"{child}.{suffix}" for child in children for suffix in ("log", "xml", "exit")
    )


def sourced(script: Path, commands: str) -> list[str]:
    return ["/bin/bash", "-c", f'source "$1"\n{commands}', "bash", str(script)]


def toml_table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def configured_testpaths() -> tuple[str, ...]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = toml_table(
        toml_table(toml_table(document["tool"])["pytest"])["ini_options"]
    )
    return tuple(str(path) for path in cast(list[object], options["testpaths"]))


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def selected_files(arguments: list[str]) -> frozenset[str]:
    candidates = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("test_*.py")
    ]
    ignored = [
        argument.removeprefix("--ignore=")
        for argument in arguments
        if argument.startswith("--ignore=")
    ]
    roots = [argument for argument in arguments if not argument.startswith("-")]
    return frozenset(
        path
        for path in candidates
        if any(is_under(path, root) for root in roots or configured_testpaths())
        and not any(is_under(path, ignore) for ignore in ignored)
    )


def test_ordinary_cohorts_partition_the_general_lane_files(tmp_path: Path) -> None:
    printing = "".join(
        f'printf "%s\\0" "${{{name}[@]}}" > "$KORA_COHORT_OUTPUT/{name}"\n'
        for name in ("general", *COHORTS)
    )

    result = subprocess.run(
        sourced(COHORT_SCRIPT, f"select_groups\n{printing}"),
        env={
            **os.environ,
            "KORA_SCAFFOLD_LANE": "conformance",
            "KORA_SCAFFOLD_OS": "ubuntu-24.04",
            "KORA_PYTEST_ARGUMENTS": " ".join(GENERAL_ARGUMENTS),
            "KORA_COHORT_OUTPUT": str(tmp_path),
        },
        capture_output=True,
        check=False,
        timeout=5,
    )
    arrays = {
        name: (tmp_path / name).read_text(encoding="utf-8").split("\0")[:-1]
        for name in ("general", *COHORTS)
    }
    cohorts = {cohort: selected_files(arrays[cohort]) for cohort in COHORTS}
    scheduled = frozenset(SCHEDULED_LIFETIME_MODULES)
    general = selected_files(arrays["general"]) - scheduled

    assert result.returncode == 0, result.stderr
    assert tuple(arrays["first"]) == FIRST_MODULES
    assert tuple(arrays["third"]) == THIRD_MODULES
    assert tuple(arrays["engine"]) == ENGINE_MODULES
    assert tuple(arrays["surface"]) == SURFACE_MODULES
    assert IR_RESOURCE_MODULES & cohorts["measurements"] == {
        "tests/ir/test_type_schema_resources.py",
        "tests/ir/test_output_schema_resources.py",
    }
    assert cohorts["second"] - selected_files(["tests/conformance"]) == {
        "tests/ir/test_inline_schema_resources.py",
        "tests/ir/test_fan_out_type_resources.py",
        "tests/ir/test_export_type_resources.py",
        "tests/ir/test_diagnostic_collection.py",
        "tests/ir/test_subgraph_interface_resources.py",
        "tests/ir/test_structured_schema_container_counts.py",
        "tests/plugins/artifact_type/test_agent_session.py",
    }
    assert IR_RESOURCE_MODULES.isdisjoint(cohorts["other"])
    assert cohorts["integration"] == selected_files(list(INTEGRATION_SELECTION))
    assert tuple(arrays["integration"]) == INTEGRATION_SELECTION
    assert (
        "tests/plugins/artifact_type/test_agent_session.py"
        not in cohorts["measurements"]
    )
    assert "tests/ir/test_output_schema_resources.py" not in cohorts["integration"]
    assert "tests/architecture/test_ir_measurement_ci.py" not in cohorts["other"]
    assert all(
        not argument.startswith("-") or argument.startswith("--ignore=")
        for values in arrays.values()
        for argument in values
    )
    assert all(
        (ROOT / argument).exists()
        for values in arrays.values()
        for argument in values
        if not argument.startswith("-")
    )
    engine = cohorts["engine"]
    graph = cohorts["graph"]
    surface = cohorts["surface"]
    placed = ("first", "second", "third")
    placed_engine = {
        path
        for path in cohorts["first"] | cohorts["third"] | engine
        if path.startswith("tests/engine/")
    }
    ubuntu = {
        **{name: cohorts[name] for name in placed},
        "measurements": cohorts["measurements"] - engine,
        "other": cohorts["other"] - engine - graph,
        "graph": graph,
        "engine": engine,
        "integration": cohorts["integration"] - surface,
        "surface": surface,
    }
    macos = {
        **{name: cohorts[name] for name in placed},
        "measurements": cohorts["measurements"],
        "other": cohorts["other"],
        "integration": cohorts["integration"] - surface,
        "surface": surface,
    }

    assert all(cohorts.values())
    assert scheduled <= selected_files(arrays["general"])
    assert all(scheduled.isdisjoint(files) for files in cohorts.values())
    assert engine <= cohorts["measurements"] | cohorts["other"]
    assert graph == selected_files(["tests/engine"]) - placed_engine
    assert graph <= cohorts["other"]
    assert surface <= cohorts["integration"]
    assert engine.isdisjoint(
        frozenset[str]().union(
            *(cohorts[name] for name in (*placed, "integration", "graph"))
        )
    )
    assert surface.isdisjoint(
        frozenset[str]().union(
            *(
                cohorts[name]
                for name in (*placed, "measurements", "other", "graph", "engine")
            )
        )
    )
    assert sum(len(files) for files in ubuntu.values()) == len(general)
    assert frozenset[str]().union(*ubuntu.values()) == general
    assert sum(len(files) for files in macos.values()) == len(general)
    assert frozenset[str]().union(*macos.values()) == general


def test_shards_partition_every_inventory_group() -> None:
    for system in ("ubuntu-24.04", "macos-15"):
        children = [
            child
            for (owner, _), assigned in EXPECTED_ORDINARY.items()
            if owner == system
            for child in assigned
        ]

        assert len(children) == len(set(children))
        if system == "ubuntu-24.04":
            assert sorted(children) == sorted(COHORTS)
        else:
            assert sorted((*children, "engine", "graph")) == sorted(COHORTS)
            assert "engine" not in children
            assert "graph" not in children


@pytest.mark.parametrize(
    ("system", "lane"),
    [
        *EXPECTED_ORDINARY,
        ("macos-15", "integration"),
        ("ubuntu-24.04", ""),
        ("ubuntu-24.04", "all"),
        ("macos-26", "conformance"),
        ("ubuntu-24.04", "storage"),
    ],
)
def test_ordinary_shell_selects_exact_groups_or_refuses_before_collection(
    tmp_path: Path, system: str, lane: str
) -> None:
    listing = (
        'select_groups\nfor name in "${names[@]}"; do\n'
        '  group_arguments "$name"\n'
        '  printf "%s\\0" "$name" "${arguments[@]}"\n'
        '  printf "\\n"\ndone\n'
    )
    environment = {
        **os.environ,
        "KORA_SCAFFOLD_OS": system,
        "KORA_SCAFFOLD_LANE": lane,
        "KORA_PYTEST_ARGUMENTS": "",
    }

    result = subprocess.run(
        sourced(COHORT_SCRIPT, listing),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )
    outer = subprocess.run(
        sourced(LANE_SCRIPT, "admit_lane\nprintf admitted\n"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )

    children = EXPECTED_ORDINARY.get((system, lane), ())
    assert result.returncode == (0 if children else 1)
    groups = tuple(
        line.removesuffix("\0").split("\0")
        for line in result.stdout.decode().splitlines()
    )
    assert tuple(name for name, *_ in groups) == children
    if lane == "remainder" and children:
        schema_resources = "tests/ir/test_type_schema_resources.py"
        owners = tuple(
            name for name, *arguments in groups if schema_resources in arguments
        )
        assert owners == (
            ("engine",) if system == "ubuntu-24.04" else ("measurements",)
        )
    if (system, lane) == ("ubuntu-24.04", "remainder"):
        other_arguments = next(
            arguments for name, *arguments in groups if name == "other"
        )
        graph_arguments = next(
            arguments for name, *arguments in groups if name == "graph"
        )
        assert "tests/engine" in graph_arguments
        assert "--ignore=tests/engine" in other_arguments
        assert "tests/engine" not in other_arguments
    if (system, lane) in {
        ("ubuntu-24.04", "integration"),
        ("macos-15", "conformance"),
    }:
        for path in SURFACE_MODULES:
            assert tuple(name for name, *arguments in groups if path in arguments) == (
                "surface",
            )
    if (system, lane) in {
        ("ubuntu-24.04", "integration"),
        ("macos-15", "remainder"),
    }:
        integration_arguments = next(
            arguments for name, *arguments in groups if name == "integration"
        )
        for path in SURFACE_MODULES:
            assert f"--ignore={path}" in integration_arguments
    assert outer.returncode == (0 if children or lane == "storage" else 1)
    assert (outer.stdout == b"admitted") is bool(children or lane == "storage")


@pytest.mark.parametrize("failed_child", [None, 0, 1, 2])
def test_ordinary_shell_joins_all_owned_children_after_failure(
    tmp_path: Path, failed_child: int | None
) -> None:
    launches = ["pids=()", "names=(measurements integration other)"]
    for index in range(3):
        exit_code = 1 if index == failed_child else 0
        launches.append(f'(printf complete > "{index}"; exit {exit_code}) &')
        launches.append('pids+=("$!")')
    script = "\n".join(launches) + '\njoin_groups\nexit "$result"\n'

    result = subprocess.run(
        sourced(COHORT_SCRIPT, script),
        cwd=tmp_path,
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == (0 if failed_child is None else 1)
    assert all((tmp_path / str(index)).read_text() == "complete" for index in range(3))
    for index, name in enumerate(("measurements", "integration", "other")):
        assert (tmp_path / f"{name}.exit").read_text() == (
            "1\n" if index == failed_child else "0\n"
        )


def test_ordinary_background_pytest_preserves_interruptible_children(
    tmp_path: Path,
) -> None:
    source = tmp_path / "test_signal.py"
    source.write_text(
        "import signal\n\n"
        "def test_interruptible_child() -> None:\n"
        "    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
    )
    script = (
        '"$KORA_TEST_PYTHON" -m tests.support.ordinary_pytest -q '
        '"$KORA_TEST_SOURCE" &\n'
        'pid="$!"\nwait "$pid"\n'
    )

    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "KORA_TEST_PYTHON": sys.executable,
            "KORA_TEST_SOURCE": str(source),
        },
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert b"1 passed" in result.stdout


@pytest.mark.parametrize("fails", [False, True])
def test_ordinary_first_failure_finalizes_fixture_and_evidence(
    tmp_path: Path, fails: bool
) -> None:
    source = tmp_path / "test_exitfirst.py"
    report = tmp_path / "junit.xml"
    source.write_text(
        "from collections.abc import Iterator\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        f"FAILS = {fails!r}\n\n"
        '@pytest.fixture(scope="session")\n'
        "def owned_resource() -> Iterator[Path]:\n"
        '    resource = Path(__file__).with_name("owned-resource")\n'
        "    resource.mkdir()\n"
        "    try:\n"
        "        yield resource\n"
        "    finally:\n"
        "        resource.rmdir()\n"
        '        resource.with_name("released").write_text("released")\n\n'
        "def test_first(owned_resource: Path) -> None:\n"
        "    assert owned_resource.is_dir()\n"
        '    assert not FAILS, "first failure evidence"\n\n'
        "def test_later(owned_resource: Path) -> None:\n"
        "    assert owned_resource.is_dir()\n"
        '    owned_resource.with_name("later-ran").write_text("ran")\n'
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.ordinary_pytest",
            "-v",
            "--exitfirst",
            f"--rootdir={tmp_path}",
            f"--junitxml={report}",
            str(source),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == int(fails), result.stdout + result.stderr
    assert not (tmp_path / "owned-resource").exists()
    assert (tmp_path / "released").read_text() == "released"
    assert (tmp_path / "later-ran").exists() is (not fails)
    if fails:
        assert b"AssertionError: first failure evidence" in result.stdout
        assert b"1 failed" in result.stdout
        with pytest.raises(EvidenceFailure, match="test did not pass"):
            junit_nodes(report)
    else:
        assert b"2 passed" in result.stdout
        assert junit_nodes(report) == frozenset(
            {"test_exitfirst.test_first", "test_exitfirst.test_later"}
        )


def test_interrupted_cohort_preserves_only_regular_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    environment_file = tmp_path / "environment"
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(environment_file),
        "KORA_SCAFFOLD_DIRECTORY": str(evidence),
    }
    script = (
        "open_ordinary_output\n"
        'printf entered > "$KORA_ORDINARY_OUTPUT/measurements.log"\n'
        "printf ready\\n\nread -r finish\n"
    )

    with subprocess.Popen(
        sourced(LANE_SCRIPT, script),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 5)
            assert readable
            assert process.stdout.read(5) == b"ready"
            output = Path(environment_file.read_text().strip().partition("=")[2])
            assert (output / "measurements.log").read_text() == "entered"
            process.terminate()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    (output / "cache-other").mkdir()
    (output / "tmp-other").mkdir()
    (output / "tmp-other" / "private").write_text("fixture-payload")
    (output / "other.log").symlink_to(output / "tmp-other" / "private")
    os.mkfifo(output / "other.xml")
    (output / "integration.xml").mkdir()
    copied = subprocess.run(
        ["/bin/bash", str(LANE_SCRIPT), "preserve"],
        env={**environment, "KORA_ORDINARY_OUTPUT": str(output)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert process.returncode is not None
    assert process.returncode != 0
    assert copied.returncode == 0, copied.stderr
    assert sorted(path.name for path in evidence.iterdir()) == ["measurements.log"]
    assert (evidence / "measurements.log").read_text() == "entered"
    assert not (evidence / "receipt.json").exists()


@pytest.mark.parametrize("children", list(EXPECTED_ORDINARY.values()))
@pytest.mark.parametrize("invalid", [None, "missing", "empty", "symlink"])
def test_ordinary_shell_requires_all_regular_nonempty_evidence(
    tmp_path: Path, children: tuple[str, ...], invalid: str | None
) -> None:
    evidence = shard_evidence(children)
    for name in evidence:
        (tmp_path / name).write_text("complete")
    target = tmp_path / evidence[-1]
    if invalid == "missing":
        target.unlink()
    elif invalid == "empty":
        target.write_text("")
    elif invalid == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "inventory.json")
    assignment = " ".join(evidence)

    result = subprocess.run(
        sourced(COHORT_SCRIPT, f"evidence=({assignment})\nrequire_group_evidence\n"),
        env={**os.environ, "KORA_ORDINARY_OUTPUT": str(tmp_path)},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert (result.returncode == 0) is (invalid is None)
