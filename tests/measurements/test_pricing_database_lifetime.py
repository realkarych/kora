import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from testcontainers.core.labels import SESSION_ID

from kora.db.types import JsonValue
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.harness import Report, new_report, settled_counts
from tests.measurements.metadata_lifetime_support import gate, record
from tests.measurements.pricing_lifetime_support import (
    CHILD_LIMIT,
    COMPLETION_LIMIT,
    MANIFEST_LIMIT,
    PricingCollectorMemory,
    PricingDatabaseFactory,
    PricingEvidence,
    PricingEvidenceError,
    pricing_command_run,
    pricing_containers,
    pricing_environment,
    pricing_process_runs,
    pricing_provenance,
    register_pricing_ownership,
    release_pricing_environment,
    remove_pricing_request,
    validate_pricing_child,
)
from tests.support.databases import (
    POSTGRES_DIGEST,
    POSTGRES_TAG,
    DatabaseKind,
    PostgresServer,
    selected_databases,
)

pytestmark = pytest.mark.measurement


def test_pricing_failure_retains_invalid_bytes_and_actual_exit(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    child_directory = tmp_path / "child"
    child_directory.mkdir()
    argv = [
        sys.executable,
        "-I",
        "-c",
        "import os; os.write(2, b'failed\\xff'); raise SystemExit(7)",
    ]
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child("sqlite-native-0", argv, child_directory, ())

    completion = json.loads((evidence.directory / "completion.json").read_text())
    assert completion["complete"] is False
    assert evidence.processes["sqlite-native-0"]["code"] == 7
    assert (evidence.directory / "sqlite-native-0.stderr").read_bytes() == b"failed\xff"
    assert len(os.listdir("/dev/fd")) == before
    pid = evidence.processes["sqlite-native-0"]["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_pricing_overflow_redacts_capture_boundary(tmp_path: Path, stream: str) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "child"
    source.mkdir()
    secret = b"disposable-fixture-credential"
    descriptor = 1 if stream == "stdout" else 2
    content = b"x" * 240 + secret + b"tail"
    argv = [
        sys.executable,
        "-I",
        "-c",
        f"import os; os.write({descriptor}, {content!r})",
    ]
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child("sqlite-native-0", argv, source, (secret,), output_limit=256)

    retained = (evidence.directory / f"sqlite-native-0.{stream}").read_bytes()
    assert retained == b"x" * 240 + b"*" * 16
    assert (
        evidence.entries[f"sqlite-native-0.{stream}"]["observed_is_lower_bound"] is True
    )
    assert (
        evidence.entries[f"sqlite-native-0.{stream}"]["modified_by_redaction"] is True
    )
    assert evidence.processes["sqlite-native-0"]["overflow"] is True
    assert not evidence.complete
    assert len(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize("file_kind", ["child", "fragment"])
@pytest.mark.parametrize(
    "condition", ["missing", "malformed", "oversized", "symlink", "directory"]
)
def test_pricing_harvesting_refuses_invalid_reports(
    tmp_path: Path, file_kind: str, condition: str
) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "child"
    source.mkdir()
    name = (
        "sqlite-native-0.json"
        if file_kind == "child"
        else "sqlite-native-0.native-cycle-0.json"
    )
    path = source / name
    limit = evidence.expected[name]
    code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
    if condition == "malformed":
        code += "p.write_bytes(b'invalid\\xff')"
    elif condition == "oversized":
        code += f"p.write_bytes(b'x' * {limit + 10})"
    elif condition == "symlink":
        code += "p.symlink_to('/etc/hosts')"
    elif condition == "directory":
        code += "p.mkdir()"
    else:
        code += "sys.exit(0)"
    argv = [sys.executable, "-I", "-c", code, str(path)]

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child("sqlite-native-0", argv, source, ())

    assert not evidence.complete
    assert evidence.processes["sqlite-native-0"]["code"] == 0
    if condition in {"malformed", "oversized"}:
        retained = (evidence.directory / name).read_bytes()
        assert retained == (
            b"invalid\xff" if condition == "malformed" else b"x" * limit
        )
    else:
        assert not (evidence.directory / name).exists()
    if condition == "malformed":
        assert f"{name}:ValidationError" in evidence.failures
    assert (evidence.directory / "completion.json").stat().st_size <= COMPLETION_LIMIT


def test_pricing_aggregate_exhaustion_preserves_completion(tmp_path: Path) -> None:
    evidence = PricingEvidence(
        tmp_path / "evidence", ("sqlite",), aggregate_limit=COMPLETION_LIMIT + 8
    )
    source = tmp_path / "child"
    source.mkdir()
    argv = [
        sys.executable,
        "-I",
        "-c",
        "import os; os.write(2, b'0123456789'); raise SystemExit(9)",
    ]

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child("sqlite-native-0", argv, source, ())

    assert evidence.processes["sqlite-native-0"]["code"] == 9
    assert (evidence.directory / "sqlite-native-0.stderr").read_bytes() == b"01234567"
    assert evidence.entries["sqlite-native-0.stderr"]["overflow"] is True
    assert not evidence.complete
    assert (
        sum(path.stat().st_size for path in evidence.directory.iterdir())
        <= COMPLETION_LIMIT + 8
    )


def test_pricing_timeout_retains_and_reaps_child(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "child"
    source.mkdir()
    argv = [sys.executable, "-I", "-c", "import signal; signal.pause()"]
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child("sqlite-native-0", argv, source, (), deadline_seconds=0.2)

    assert evidence.processes["sqlite-native-0"]["timed_out"] is True
    pid = evidence.processes["sqlite-native-0"]["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert len(os.listdir("/dev/fd")) == before
    assert not evidence.complete


def test_pricing_interrupt_harvests_without_fabricating_result(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "child"
    source.mkdir()
    pid_file = tmp_path / "child.pid"
    code = (
        "import os,signal,sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "Path(sys.argv[2]).write_bytes(b'incomplete'); "
        "os.kill(os.getppid(), signal.SIGINT); signal.pause()"
    )
    argv = [
        sys.executable,
        "-I",
        "-c",
        code,
        str(pid_file),
        str(source / "sqlite-native-0.json"),
    ]
    before = len(os.listdir("/dev/fd"))

    with pytest.raises(BaseExceptionGroup) as caught:
        evidence.run_child("sqlite-native-0", argv, source, ())

    assert any(
        isinstance(error, KeyboardInterrupt) for error in caught.value.exceptions
    )
    process = evidence.processes["sqlite-native-0"]
    assert process["pid"] is None
    assert process["code"] is None
    assert process["stdout_available"] is False
    assert process["stderr_available"] is False
    assert (evidence.directory / "sqlite-native-0.json").read_bytes() == b"incomplete"
    pid = int(pid_file.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert len(os.listdir("/dev/fd")) == before
    assert not evidence.complete


@pytest.mark.parametrize(
    ("fault", "refusal"),
    [
        ("runtime", "interpreter or revision"),
        ("import", "imported source or executable"),
        ("inventory", "lifecycle inventory is incomplete"),
    ],
)
def test_pricing_checker_refuses_false_completion(
    tmp_path: Path, fault: str, refusal: str
) -> None:
    report = new_report()
    report.revision = "regression"
    pricing_environment(report, "native")
    report.gates["pricing_child_resources_released"] = True
    if fault == "runtime":
        report.python = "3.15.0"
    elif fault == "import":
        report.environment["database_source"] = "/unrelated/connection.py"
    path = tmp_path / "sqlite-native-0.json"
    payload = report.to_json().encode()
    argv = [
        sys.executable,
        "-I",
        "-c",
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text(sys.argv[2])",
        str(path),
        payload.decode(),
    ]

    result = run_process(argv)
    with pytest.raises(PricingEvidenceError, match=refusal):
        validate_pricing_child(tmp_path, "sqlite-native-0", "regression")

    assert result.code == 0
    assert not result.stderr
    assert path.read_bytes() == payload


@pytest.fixture
def pricing_report() -> tuple[Report, float]:
    started = time.perf_counter()
    return new_report(), started


def test_pricing_database_lifetime(
    pricing_report: tuple[Report, float], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    report, started = pricing_report
    fixture_seconds = 0.0
    kinds = selected_databases(os.environ)
    output = Path(os.environ.get("KORA_PRICING_EVIDENCE", str(tmp_path / "evidence")))
    evidence = PricingEvidence(output, tuple(kind.value for kind in kinds))
    provenance: dict[str, JsonValue] = {}
    evidence.memory = PricingCollectorMemory()
    evidence.memory.observe("parent_baseline", report, len(evidence.entries))
    try:
        pricing_environment(report, "coordinator")
        provenance = pricing_provenance()
        if sys.version_info[:3] != (3, 11, 17):
            raise PricingEvidenceError("pricing measurements require Python 3.11.17")
        for kind in kinds:
            if kind is DatabaseKind.POSTGRESQL:
                register_pricing_ownership(set(), session_id=SESSION_ID)
            fixture_started = time.perf_counter()
            server: PostgresServer | None = (
                request.getfixturevalue("postgres_server")
                if kind is DatabaseKind.POSTGRESQL
                else None
            )
            elapsed = time.perf_counter() - fixture_started
            report.durations[f"{kind.value}_fixture"] = elapsed
            if kind is DatabaseKind.POSTGRESQL:
                fixture_seconds += elapsed
                report.environment["postgres_image"] = (
                    f"{POSTGRES_TAG}@{POSTGRES_DIGEST}"
                )
            directory = tmp_path / kind.value
            directory.mkdir()
            baseline_threads, baseline_descriptors = settled_counts()
            baseline_connections = None if server is None else server.connection_count()
            factory = PricingDatabaseFactory(kind, directory, server)
            try:
                pricing_process_runs(report, kind, factory, evidence)
            finally:
                original = sys.exception()
                try:
                    factory.close()
                finally:
                    cleanup = sys.exception()
                    if (
                        original is not None
                        and cleanup is not None
                        and original is not cleanup
                    ):
                        raise BaseExceptionGroup(
                            "pricing workload and factory cleanup failed",
                            [original, cleanup],
                        )
            threads, descriptors = settled_counts()
            connections = factory.server_connections()
            gate(
                report, "threads_return_to_baseline", kind, threads <= baseline_threads
            )
            gate(
                report,
                "descriptors_return_to_baseline",
                kind,
                descriptors <= baseline_descriptors,
            )
            gate(
                report,
                "server_connections_return_to_baseline",
                kind,
                connections is None
                or baseline_connections is None
                or connections <= baseline_connections,
            )
            record(
                report,
                "resource_counts",
                1,
                0.0,
                kind,
                baseline_threads=baseline_threads,
                threads=threads,
                baseline_descriptors=baseline_descriptors,
                descriptors=descriptors,
            )
            record(
                report,
                "server_connections",
                1,
                0.0,
                kind,
                baseline=baseline_connections,
                after=connections,
            )
        expected_gates = {
            f"{kind.value}_{name}"
            for kind in kinds
            for name in (
                "threads_return_to_baseline",
                "descriptors_return_to_baseline",
                "server_connections_return_to_baseline",
            )
        }
        expected_gates.update(
            f"{kind.value}_pricing_child_resources_released_{mode}_{repeat}"
            for kind in kinds
            for mode in ("native", "heap")
            for repeat in range(3)
        )
        assert set(report.gates) == expected_gates
        assert all(report.gates.values()), report.gates
    finally:
        provenance["workload_started"] = started
        provenance["excluded_common_postgresql_fixture"] = fixture_seconds
        evidence.finish(report, provenance)


def test_pricing_complete_artifact_inventory_fails_aggregate_exhaustion(
    tmp_path: Path,
) -> None:
    evidence = PricingEvidence(
        tmp_path / "evidence",
        ("sqlite", "postgresql"),
        aggregate_limit=COMPLETION_LIMIT + 64,
    )
    payload = new_report().to_json()
    code = (
        "import sys; from pathlib import Path; "
        "root=Path(sys.argv[1]); identity=sys.argv[2]; mode=identity.split('-')[1]; "
        "names=[identity+'.json']+"
        "[f'{identity}.{mode}-cycle-{i}.json' for i in range(3)]; "
        "[(root/name).write_text(sys.argv[3]) for name in names]"
    )

    for kind in ("sqlite", "postgresql"):
        for mode in ("native", "heap"):
            for repeat in range(3):
                identity = f"{kind}-{mode}-{repeat}"
                source = tmp_path / identity
                source.mkdir()
                argv = [
                    sys.executable,
                    "-I",
                    "-c",
                    code,
                    str(source),
                    identity,
                    payload,
                ]
                with pytest.raises(BaseExceptionGroup):
                    evidence.run_child(identity, argv, source, ())
    with pytest.raises(PricingEvidenceError, match="inventory is incomplete"):
        evidence.finish(new_report(), {})

    assert len(list(evidence.directory.iterdir())) == 75
    assert set(evidence.entries) == set(evidence.expected)
    assert len(evidence.processes) == 12
    assert all(process["code"] == 0 for process in evidence.processes.values())
    assert (
        sum(path.stat().st_size for path in evidence.directory.iterdir())
        <= COMPLETION_LIMIT + 64
    )
    assert not evidence.complete


def test_pricing_failure_artifacts_redact_known_literals(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "child"
    source.mkdir()
    secret = b"disposable-credential-3719"
    url = b"postgresql+asyncpg://kora:" + secret + b"@127.0.0.1:5432/fixture"
    raw = b"invalid\xff " + url + b" " + secret
    child = source / "sqlite-native-0.json"
    code = (
        "import os,sys; from pathlib import Path; "
        f"Path(sys.argv[1]).write_bytes({raw!r}); os.write(2, {raw!r}); "
        "raise SystemExit(11)"
    )

    with pytest.raises(BaseExceptionGroup):
        evidence.run_child(
            "sqlite-native-0",
            [sys.executable, "-I", "-c", code, str(child)],
            source,
            (secret, url),
        )

    assert evidence.processes["sqlite-native-0"]["code"] == 11
    for path in evidence.directory.iterdir():
        content = path.read_bytes()
        assert secret not in content
        assert url not in content
    assert (
        (evidence.directory / "sqlite-native-0.json")
        .read_bytes()
        .startswith(b"invalid\xff ")
    )
    assert evidence.entries["sqlite-native-0.json"]["modified_by_redaction"] is True
    assert evidence.entries["sqlite-native-0.stderr"]["modified_by_redaction"] is True
    assert not evidence.complete


@pytest.mark.parametrize("name", ["completion.json", "manifest.json"])
@pytest.mark.parametrize("fault", ["truncated", "oversized", "directory"])
def test_pricing_command_preserves_exit_with_malformed_envelope(
    tmp_path: Path, name: str, fault: str
) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    if fault == "directory":
        (directory / name).mkdir()
    else:
        (directory / name).write_bytes(
            b'{"truncated":' if fault == "truncated" else b"x" * (MANIFEST_LIMIT + 1)
        )
    argv = [sys.executable, "-I", "-c", "raise SystemExit(17)"]

    with pytest.raises(BaseExceptionGroup):
        pricing_command_run(directory, argv)

    sealed = directory.with_name(directory.name + "-sealed")
    completion = json.loads((sealed / "completion.json").read_bytes())
    manifest = json.loads((sealed / "manifest.json").read_bytes())
    assert completion["command_exit_code"] == 17
    assert completion["complete"] is False
    assert manifest["containers_released"] is True
    assert manifest["collection_failures"]
    if fault == "truncated":
        assert manifest["malformed_inputs"][name]["raw_hex"] == b'{"truncated":'.hex()
    elif fault == "oversized":
        assert manifest["malformed_inputs"][name]["truncated"] is True


def test_pricing_command_seals_only_bounded_allowed_files(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    (directory / "private-request.json").write_bytes(b"excluded-private-data")
    (directory / "sqlite-native-0.json").write_bytes(b"x" * (CHILD_LIMIT + 1))
    (directory / "sqlite-heap-0.json").symlink_to("/etc/hosts")
    argv = [sys.executable, "-I", "-c", "raise SystemExit(7)"]

    with pytest.raises(BaseExceptionGroup):
        pricing_command_run(directory, argv)

    sealed = tmp_path / "evidence-sealed"
    assert {path.name for path in sealed.iterdir()} == {
        "sqlite-native-0.json",
        "manifest.json",
        "completion.json",
    }
    assert (sealed / "sqlite-native-0.json").stat().st_size == CHILD_LIMIT
    assert json.loads((sealed / "completion.json").read_bytes())["complete"] is False
    assert all(path.is_file() and not path.is_symlink() for path in sealed.iterdir())


def test_pricing_unlistable_source_retains_available_fragment(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    source = tmp_path / "source"
    source.mkdir()
    name = "sqlite-native-0.native-cycle-0.json"
    script = (
        "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
        f"(p/{name!r}).write_bytes(b'partial disposable-secret'); "
        "p.chmod(0o300); raise SystemExit(19)"
    )

    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            evidence.run_child(
                "sqlite-native-0",
                [sys.executable, "-I", "-c", script, str(source)],
                source,
                (b"disposable-secret",),
            )
    finally:
        source.chmod(0o700)

    assert any(isinstance(error, PermissionError) for error in caught.value.exceptions)
    assert evidence.processes["sqlite-native-0"]["code"] == 19
    assert (evidence.directory / name).read_bytes() == b"partial " + b"*" * 17
    assert not evidence.complete
    pid = evidence.processes["sqlite-native-0"]["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_pricing_request_removal_preserves_original_failure(tmp_path: Path) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite",))
    path = tmp_path / "private-request.json"
    path.mkdir()

    def fail_and_remove_request() -> None:
        try:
            raise PricingEvidenceError("original child failure")
        finally:
            remove_pricing_request(path, evidence)

    with pytest.raises(BaseExceptionGroup) as caught:
        fail_and_remove_request()

    assert any(
        isinstance(error, PricingEvidenceError) for error in caught.value.exceptions
    )
    assert any(isinstance(error, OSError) for error in caught.value.exceptions)
    completion = json.loads((evidence.directory / "completion.json").read_bytes())
    assert completion["request_removed"] is False
    assert completion["complete"] is False
    assert path.exists()


def test_pricing_environment_removes_owned_root_after_provenance_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owned-environment"
    root.mkdir()
    (root / "constraints.txt").mkdir()
    directory = tmp_path / "evidence"
    descriptors = len(os.listdir("/dev/fd"))

    with pytest.raises(BaseExceptionGroup) as caught:
        release_pricing_environment(root, tmp_path, directory, True)

    assert any(
        isinstance(error, (PricingEvidenceError, OSError))
        for error in caught.value.exceptions
    )
    assert not root.exists()
    assert len(os.listdir("/dev/fd")) == descriptors
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert manifest["environment_release"]["temporary_environment_released"] is True
    assert json.loads((directory / "completion.json").read_bytes())["complete"] is False


def test_pricing_outer_interrupt_releases_child_and_live_resource(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "evidence"
    resource = tmp_path / "live-resource"
    pid_file = tmp_path / "child.pid"
    script = "\n".join(
        [
            "import os,signal,subprocess,sys",
            "from pathlib import Path",
            "resource=Path(sys.argv[1]); resource.write_text('owned')",
            "child=subprocess.Popen([sys.executable,'-I','-c',"
            "'import signal; signal.pause()'],start_new_session=True)",
            "Path(sys.argv[2]).write_text(str(child.pid))",
            "try:",
            "    os.kill(os.getppid(),signal.SIGINT)",
            "    signal.pause()",
            "finally:",
            "    child.kill(); child.wait(); resource.unlink()",
        ]
    )
    before = len(os.listdir("/dev/fd"))

    try:
        with pytest.raises((BaseExceptionGroup, KeyboardInterrupt)) as caught:
            pricing_command_run(
                directory,
                [sys.executable, "-I", "-c", script, str(resource), str(pid_file)],
            )
        assert not resource.exists()
        assert isinstance(caught.value, BaseExceptionGroup)
        assert any(
            isinstance(error, KeyboardInterrupt) for error in caught.value.exceptions
        )
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert len(os.listdir("/dev/fd")) == before
        completion = json.loads((directory / "completion.json").read_bytes())
        assert completion["command_exit_code"] is not None
        assert completion["complete"] is False
    finally:
        if resource.exists():
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
            resource.unlink()


@pytest.mark.parametrize(
    ("registered", "same_session"), [(True, True), (False, True), (False, False)]
)
def test_pricing_stopped_container_is_detected_and_removed(
    tmp_path: Path, registered: bool, same_session: bool
) -> None:
    docker = shutil.which("docker")
    assert docker is not None
    before = pricing_containers()
    directory = tmp_path / "evidence"
    identity_path = tmp_path / "owned-container.id"
    script = "\n".join(
        [
            "import json,os,subprocess,sys",
            "from pathlib import Path",
            "owner=Path(os.environ['KORA_PRICING_OWNER'])",
            "owner.write_text(json.dumps({'containers':[],'sessions':['pricing-fixture-owner'],'secrets':[]}))",
            "label='org.testcontainers.session-id=' + sys.argv[5]",
            "created=subprocess.run([sys.argv[1],'create','--label',label,'--entrypoint','/bin/true',sys.argv[2]],check=True,capture_output=True)",
            "identity=created.stdout.decode().strip()",
            "Path(sys.argv[3]).write_text(identity)",
            "owner=Path(os.environ['KORA_PRICING_OWNER'])",
            "owner.write_text(json.dumps({'containers':[identity] "
            "if sys.argv[4]=='registered' else [],"
            "'sessions':['pricing-fixture-owner'],'secrets':[]}))",
            "subprocess.run([sys.argv[1],'start','--attach',identity],check=True,capture_output=True)",
            "raise SystemExit(23)",
        ]
    )

    try:
        with pytest.raises(BaseExceptionGroup):
            pricing_command_run(
                directory,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    docker,
                    f"{POSTGRES_TAG}@{POSTGRES_DIGEST}",
                    str(identity_path),
                    "registered" if registered else "acquiring",
                    "pricing-fixture-owner" if same_session else "unrelated-fixture",
                ],
            )
        identity = identity_path.read_text()
        manifest = json.loads((directory / "manifest.json").read_bytes())
        assert manifest["containers_retained_before_cleanup"] == (
            [identity] if same_session else []
        )
        assert manifest["containers_owned"] == ([identity] if same_session else [])
        assert manifest["containers_released"] is same_session
        assert manifest["uncontended_inventory_verified"] is same_session
        assert manifest["command_exit_code"] == 23
        assert pricing_containers() == (before if same_session else before | {identity})
    finally:
        if identity_path.exists() and identity_path.read_text() in pricing_containers():
            subprocess.run(
                [docker, "rm", "--force", identity_path.read_text()],
                check=True,
                capture_output=True,
            )


def test_pricing_finalization_observes_and_releases_parent_payload(
    tmp_path: Path,
) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ())
    report = new_report()
    report.environment["retained_payload"] = "x" * 100000
    started = time.perf_counter()

    evidence.finish(report, {"workload_started": started})

    manifest = json.loads((evidence.directory / "manifest.json").read_bytes())
    assert "parent_collector_memory" in manifest
    assert report.environment == {}
    assert manifest["pricing_workload_total"] >= 0
    assert manifest["excluded_parent_serialization_seconds"] >= 0
    assert manifest["parent_collector_memory"][-1]["phase"] == "report_payload_released"
