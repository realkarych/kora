import glob
import hashlib
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
    AGGREGATE_LIMIT,
    CHILD_LIMIT,
    COMPLETION_LIMIT,
    MANIFEST_LIMIT,
    PricingCollectorMemory,
    PricingDatabaseFactory,
    PricingEvidence,
    PricingEvidenceError,
    PricingPublication,
    prepare_pricing_publication,
    pricing_command_run,
    pricing_containers,
    pricing_environment,
    pricing_files,
    pricing_process_runs,
    pricing_provenance,
    register_pricing_ownership,
    release_pricing_environment,
    release_pricing_publication,
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
    sealed = directory.with_name(directory.name + "-sealed")
    manifest = json.loads((sealed / "manifest.json").read_bytes())
    assert manifest["environment_release"]["temporary_environment_released"] is True
    assert json.loads((sealed / "completion.json").read_bytes())["complete"] is False


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


@pytest.mark.parametrize(
    "missing",
    [
        "parent.json",
        "sqlite-native-0.json",
        "sqlite-native-0.native-cycle-0.json",
        "sqlite-native-0.stdout",
    ],
)
def test_pricing_complete_seal_refuses_removed_collected_file(
    tmp_path: Path, missing: str
) -> None:
    evidence = PricingEvidence(tmp_path / "evidence", ("sqlite", "postgresql"))
    payload = new_report().to_json().encode()
    for name in evidence.expected:
        if name not in {"parent.json", "manifest.json"}:
            evidence.retain(
                name, b"" if name.endswith((".stdout", ".stderr")) else payload
            )
    evidence.finish(new_report(), {})
    assert evidence.complete
    (evidence.directory / missing).unlink()

    with pytest.raises(BaseExceptionGroup):
        pricing_command_run(
            evidence.directory, [sys.executable, "-I", "-c", "raise SystemExit(0)"]
        )

    sealed = tmp_path / "evidence-sealed"
    completion = json.loads((sealed / "completion.json").read_bytes())
    manifest = json.loads((sealed / "manifest.json").read_bytes())
    assert completion["complete"] is False
    assert manifest["command_complete"] is False
    assert missing in completion["upload_missing"]
    assert completion["command_exit_code"] == 0


def test_pricing_environment_publication_preserves_command_redaction(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "evidence"
    root = tmp_path / "owned-environment"
    root.mkdir()
    secret = b"composition-secret-2917"
    url = b"postgresql+asyncpg://fixture:" + secret + b"@127.0.0.1/test"
    script = (
        "import os,json,sys; from pathlib import Path; "
        "owner=Path(os.environ['KORA_PRICING_OWNER']); "
        "owner.write_text(json.dumps({'containers':[],'sessions':[],"
        f"'secrets':[{secret.decode()!r},{url.decode()!r}]}})); "
        "p=Path(sys.argv[1]); p.mkdir(); "
        f"(p/'sqlite-native-0.json').write_bytes(b'invalid '+{url!r}+b' '+{secret!r}); "
        f"(p/'sqlite-native-0.stdout').write_bytes(b'prefix '+{secret[:12]!r}); "
        "raise SystemExit(29)"
    )

    assert (
        pricing_command_run(
            directory, [sys.executable, "-I", "-c", script, str(directory)]
        )
        == 29
    )
    sealed = tmp_path / "evidence-sealed"
    initial = {path.name: path.read_bytes() for path in sealed.iterdir()}
    release_pricing_environment(root, tmp_path, directory, True)
    final = {path.name: path.read_bytes() for path in sealed.iterdir()}

    assert initial.keys() == final.keys()
    for content in [*initial.values(), *final.values()]:
        assert secret not in content
        assert url not in content
        assert secret[:12] not in content
    assert final["sqlite-native-0.json"] == initial["sqlite-native-0.json"]
    assert final["sqlite-native-0.stdout"] == initial["sqlite-native-0.stdout"]
    assert sum(map(len, final.values())) <= AGGREGATE_LIMIT
    completion = json.loads(final["completion.json"])
    assert completion["command_exit_code"] == 29
    assert completion["complete"] is False
    assert not root.exists()
    assert not (tmp_path / "evidence-owner.json").exists()


@pytest.mark.parametrize("registration", [False, True])
def test_pricing_private_owner_errors_are_content_free(
    tmp_path: Path, registration: bool
) -> None:
    directory = tmp_path / "evidence"
    secret = b"private-s2-marker".decode()
    url = f"postgres://u:{secret}@local/db"
    malformed = '{"secrets":["' + url + '","' + secret
    inner = (
        "import os; from pathlib import Path; "
        f"Path(os.environ['KORA_PRICING_OWNER']).write_text({malformed!r}); "
        + (
            "from tests.measurements.pricing_lifetime_support import "
            "register_pricing_ownership; register_pricing_ownership(set())"
            if registration
            else "raise SystemExit(31)"
        )
    )
    outer = (
        "import sys; from pathlib import Path; "
        "from tests.measurements.pricing_lifetime_support import pricing_command_run; "
        "sys.exit(pricing_command_run(Path(sys.argv[1]), "
        "[sys.executable,'-c',sys.argv[2]]))"
    )

    result = subprocess.run(
        [sys.executable, "-c", outer, str(directory), inner],
        capture_output=True,
        check=False,
    )

    (tmp_path / "outer.stdout").write_bytes(result.stdout)
    (tmp_path / "outer.stderr").write_bytes(result.stderr)
    assert result.returncode != 0
    assert secret.encode() not in result.stdout + result.stderr
    assert url.encode() not in result.stdout + result.stderr
    assert b"private pricing owner validation failed" in result.stderr
    sealed = tmp_path / "evidence-sealed"
    for path in sealed.iterdir():
        assert secret.encode() not in path.read_bytes()
        assert url.encode() not in path.read_bytes()
    completion = json.loads((sealed / "completion.json").read_bytes())
    assert completion["command_exit_code"] == (1 if registration else 31)
    assert completion["complete"] is False
    assert completion["command_reaped"] is True
    assert completion["private_owner_removed"] is True


@pytest.mark.parametrize(
    "fault", ["reserved_nonregular", "working_nonregular", "reserved_symlink"]
)
def test_pricing_reserved_write_failure_still_releases_owned_resources(
    tmp_path: Path,
    fault: str,
) -> None:
    docker = shutil.which("docker")
    assert docker is not None
    before = pricing_containers()
    descriptors = len(os.listdir("/dev/fd"))
    directory = tmp_path / "evidence"
    identity_path = tmp_path / "owned-container.id"
    reserved = tmp_path / "evidence-sealed" / "completion.json"
    sentinel = tmp_path / "untouched-target"
    if fault == "reserved_nonregular":
        reserved.mkdir(parents=True)
    elif fault == "working_nonregular":
        directory.write_bytes(b"working sentinel")
    else:
        sentinel.mkdir()
        (sentinel / "completion.json").write_bytes(b"reserved sentinel")
        reserved.parent.symlink_to(sentinel, target_is_directory=True)
    script = (
        "import os,json,subprocess,sys; from pathlib import Path; "
        "created=subprocess.run([sys.argv[1],'create','--entrypoint','/bin/true',"
        "sys.argv[2]],check=True,capture_output=True); "
        "identity=created.stdout.decode().strip(); "
        "Path(sys.argv[3]).write_text(identity); "
        "Path(os.environ['KORA_PRICING_OWNER']).write_text(json.dumps("
        "{'containers':[identity],'sessions':[],'secrets':[]})); "
        "subprocess.run([sys.argv[1],'start','--attach',identity],"
        "check=True,capture_output=True); raise SystemExit(37)"
    )

    try:
        with pytest.raises(BaseExceptionGroup) as caught:
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
                ],
            )
        assert any(
            isinstance(error, (OSError, PricingEvidenceError))
            for error in caught.value.exceptions
        )
        assert pricing_containers() == before
        assert not (tmp_path / "evidence-owner.json").exists()
        if fault == "reserved_nonregular":
            assert reserved.is_dir()
        elif fault == "working_nonregular":
            assert directory.read_bytes() == b"working sentinel"
        else:
            assert reserved.parent.is_symlink()
            assert (sentinel / "completion.json").read_bytes() == b"reserved sentinel"
        assert len(os.listdir("/dev/fd")) == descriptors
        completion_path = (
            reserved if fault == "working_nonregular" else directory / "completion.json"
        )
        completion = json.loads(completion_path.read_bytes())
        assert completion["command_exit_code"] == 37
        assert completion["command_reaped"] is True
        assert completion["complete"] is False
    finally:
        if identity_path.exists() and identity_path.read_text() in pricing_containers():
            subprocess.run(
                [docker, "rm", "--force", identity_path.read_text()],
                check=True,
                capture_output=True,
            )
        (tmp_path / "evidence-owner.json").unlink(missing_ok=True)


def test_pricing_noncooperative_owner_reaches_real_cleanup_deadline(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    assert docker is not None
    before = pricing_containers()
    descriptors = len(os.listdir("/dev/fd"))
    handlers = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    directory = tmp_path / "evidence"
    identity_path = tmp_path / "owned-container.id"
    descendant_path = tmp_path / "descendant.pid"
    script = "\n".join(
        [
            "import json,os,signal,subprocess,sys",
            "from pathlib import Path",
            "signal.signal(signal.SIGINT,signal.SIG_IGN)",
            "created=subprocess.run([sys.argv[1],'create','-i','--entrypoint','/bin/cat',sys.argv[2]],check=True,capture_output=True)",
            "identity=created.stdout.decode().strip()",
            "Path(sys.argv[3]).write_text(identity)",
            "Path(os.environ['KORA_PRICING_OWNER']).write_text(json.dumps({'containers':[identity],'sessions':[],'secrets':[]}))",
            "subprocess.run([sys.argv[1],'start',identity],check=True,capture_output=True)",
            "child=subprocess.Popen([sys.executable,'-I','-c',"
            "'import os,signal; os.write(1,b\"ready\"); signal.pause()'],"
            "stdout=subprocess.PIPE,start_new_session=True)",
            "assert child.stdout.read(5)==b'ready'",
            "child.stdout.close()",
            "Path(sys.argv[4]).write_text(str(child.pid))",
            "os.kill(os.getppid(),signal.SIGINT)",
            "signal.pause()",
        ]
    )

    try:
        with pytest.raises(BaseExceptionGroup) as caught:
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
                    str(descendant_path),
                ],
            )
        manifest = json.loads((directory / "manifest.json").read_bytes())
        completion = json.loads((directory / "completion.json").read_bytes())
        descendant = int(descendant_path.read_text())
        assert any(
            isinstance(error, KeyboardInterrupt) for error in caught.value.exceptions
        )
        assert any(
            isinstance(error, PricingEvidenceError)
            and "cooperative cleanup deadline" in str(error)
            for error in caught.value.exceptions
        )
        assert manifest["pytest_process_seconds"] >= 60.0
        assert completion["command_exit_code"] == -signal.SIGKILL
        assert completion["command_interruption"] == signal.SIGINT
        assert completion["command_reaped"] is True
        assert completion["complete"] is False
        released = manifest["escalated_descendant_release"][str(descendant)]
        assert released["parent_pid"] == completion["command_pid"]
        assert released["signal"] == signal.SIGKILL
        assert released["signal_sent"] is True
        assert released["absent_after_owner_exit"] is True
        assert released["nonchild_reaped"] is None
        with pytest.raises(ChildProcessError):
            os.waitpid(completion["command_pid"], os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant, 0)
        assert manifest["containers_owned"] == [identity_path.read_text()]
        assert manifest["containers_released"] is True
        assert pricing_containers() == before
        assert not (tmp_path / "evidence-owner.json").exists()
        assert len(os.listdir("/dev/fd")) == descriptors
        assert (
            signal.getsignal(signal.SIGINT),
            signal.getsignal(signal.SIGTERM),
        ) == handlers
    finally:
        if identity_path.exists() and identity_path.read_text() in pricing_containers():
            subprocess.run(
                [docker, "rm", "--force", identity_path.read_text()],
                check=True,
                capture_output=True,
            )


@pytest.mark.parametrize("state", ["complete", "failure", "root_symlink", "subtree"])
def test_pricing_final_publication_selects_only_validated_files(
    tmp_path: Path, state: str
) -> None:
    directory = tmp_path / "evidence"
    temporary = tmp_path / "runner"
    temporary.mkdir()
    environment = temporary / "environment"
    environment.mkdir()
    sealed = tmp_path / "evidence-sealed"
    marker = b"private-publication-marker-937"
    target = tmp_path / "unsafe-target"
    unsafe = state in {"root_symlink", "subtree"}
    if state == "complete":
        evidence = PricingEvidence(directory, ("sqlite", "postgresql"))
        payload = new_report().to_json().encode()
        for name in evidence.expected:
            if name not in {"parent.json", "manifest.json"}:
                evidence.retain(
                    name, b"" if name.endswith((".stdout", ".stderr")) else payload
                )
        evidence.finish(new_report(), {})
    elif unsafe:
        target = target if state == "root_symlink" else sealed / "completion.json"
        target.mkdir(parents=True)
        (target / "private.json").write_bytes(marker)
        with (target / "oversized.json").open("wb") as output:
            output.write(marker)
            output.truncate(CHILD_LIMIT + 1)
        if state == "root_symlink":
            sealed.symlink_to(target, target_is_directory=True)
    script = (
        "import os,json,sys; from pathlib import Path; "
        "owner=Path(os.environ['KORA_PRICING_OWNER']); "
        f"owner.write_text(json.dumps({{'containers':[],'sessions':[],"
        f"'secrets':[{marker.decode()!r}]}})); "
        "\nif sys.argv[3]=='failure':\n"
        " p=Path(sys.argv[2]); p.mkdir(); "
        f"(p/'sqlite-native-0.stdout').write_bytes(b'diagnostic '+{marker!r})\n"
        "raise SystemExit(int(sys.argv[1]))"
    )
    exit_code = 0 if state == "complete" else 29
    descriptors = len(os.listdir("/dev/fd"))
    before = pricing_containers()

    if unsafe:
        with pytest.raises(BaseExceptionGroup):
            pricing_command_run(
                directory,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(exit_code),
                    str(directory),
                    state,
                ],
            )
        with pytest.raises(BaseExceptionGroup):
            release_pricing_environment(environment, temporary, directory, True)
    else:
        assert (
            pricing_command_run(
                directory,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(exit_code),
                    str(directory),
                    state,
                ],
            )
            == exit_code
        )
        release_pricing_environment(environment, temporary, directory, True)
    publication = prepare_pricing_publication(
        directory,
        temporary,
        "success" if state == "complete" else "failure",
        "failure" if unsafe else "success",
    )
    selected = sorted(
        path
        for name in glob.iglob(str(publication.directory / "**/*"), recursive=True)
        if not (path := Path(name)).is_dir()
    )
    (tmp_path / "publication-selection.json").write_text(
        json.dumps(
            {
                "publication": json.loads(publication.model_dump_json()),
                "selected": [
                    {
                        "path": str(path),
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in selected
                ],
            }
        )
    )

    try:
        assert selected
        assert all(marker not in path.read_bytes() for path in selected)
        limits = pricing_files(("sqlite", "postgresql"))
        limits["completion.json"] = COMPLETION_LIMIT
        assert all(path.name in limits for path in selected)
        assert all(path.is_file() and not path.is_symlink() for path in selected)
        assert all(path.parent == publication.directory for path in selected)
        assert all(path.stat().st_size <= limits[path.name] for path in selected)
        assert sum(path.stat().st_size for path in selected) <= AGGREGATE_LIMIT
        assert publication.directory.parent == temporary.resolve()
        completion = json.loads(
            (publication.directory / "completion.json").read_bytes()
        )
        (tmp_path / "publication-completion.json").write_text(json.dumps(completion))
        (tmp_path / "publication-manifest.json").write_bytes(
            (publication.directory / "manifest.json").read_bytes()
        )
        assert completion["complete"] is (state == "complete")
        assert completion["command_exit_code"] == exit_code
        assert completion["command_reaped"] is True
        if state == "complete":
            assert {path.name for path in selected} == set(limits)
        if state == "failure":
            assert (publication.directory / "sqlite-native-0.stdout").read_bytes() == (
                b"diagnostic " + b"*" * len(marker)
            )
        if unsafe:
            assert {path.name for path in selected} == {
                "manifest.json",
                "completion.json",
            }
            assert (target / "private.json").read_bytes() == marker
            assert (target / "oversized.json").stat().st_size == CHILD_LIMIT + 1
            assert completion["working_evidence_available"] is False
        if state == "root_symlink":
            assert sealed.is_symlink()
        assert not environment.exists()
        assert not (tmp_path / "evidence-owner.json").exists()
        assert pricing_containers() == before
        assert len(os.listdir("/dev/fd")) == descriptors
    finally:
        if publication.directory.parent == temporary.resolve():
            proof = release_pricing_publication(publication, temporary)
            (tmp_path / "publication-release.json").write_text(json.dumps(proof))
            assert proof["publication_released"] is True
    assert not publication.directory.exists()


@pytest.mark.parametrize("fault", ["inode", "symlink"])
def test_pricing_publication_release_refuses_changed_ownership(
    tmp_path: Path, fault: str
) -> None:
    temporary = tmp_path / "runner"
    temporary.mkdir()
    directory = temporary / "kora-pricing-upload-owned"
    directory.mkdir()
    (directory / "sentinel").write_bytes(b"preserved")
    metadata = directory.stat()
    publication = PricingPublication(
        directory=directory,
        device=metadata.st_dev,
        inode=metadata.st_ino + (1 if fault == "inode" else 0),
    )
    target = directory
    if fault == "symlink":
        target = temporary / "untouched-target"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(PricingEvidenceError, match="ownership mismatch"):
        release_pricing_publication(publication, temporary)

    assert (target / "sentinel").read_bytes() == b"preserved"
    assert directory.exists()


def test_pricing_publication_output_failure_releases_allocated_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "evidence"
    sealed = tmp_path / "evidence-sealed"
    sealed.mkdir()
    (sealed / "manifest.json").write_text('{"command_complete":false}')
    (sealed / "completion.json").write_text(
        '{"complete":false,"command_exit_code":29,"command_reaped":true}'
    )
    temporary = tmp_path / "runner"
    temporary.mkdir()
    output = tmp_path / "nonregular-output"
    output.mkdir()
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(temporary),
        "GITHUB_OUTPUT": str(output),
        "KORA_PRICING_EVIDENCE": str(directory),
        "KORA_PRICING_MEASUREMENT_OUTCOME": "failure",
        "KORA_PRICING_RELEASE_OUTCOME": "success",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.measurements.pricing_lifetime_support import "
            "pricing_publication_prepare; pricing_publication_prepare()",
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=60,
    )

    (tmp_path / "publication.stdout").write_bytes(result.stdout)
    (tmp_path / "publication.stderr").write_bytes(result.stderr)
    assert result.returncode == 1
    assert b"IsADirectoryError" in result.stderr
    assert list(temporary.iterdir()) == []
    assert output.is_dir()
    assert (
        json.loads((sealed / "completion.json").read_bytes())["command_exit_code"] == 29
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_pricing_fallback_handoff_is_ready_while_step_fails(
    tmp_path: Path, malformed: bool
) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    marker = b"private-fallback-marker-381"
    (directory / "completion.json").write_bytes(
        b'{"private":"' + marker
        if malformed
        else b'{"command_exit_code":29,"command_reaped":true}'
    )
    target = tmp_path / "untouched"
    target.mkdir()
    (target / "private.json").write_bytes(marker)
    (tmp_path / "evidence-sealed").symlink_to(target, target_is_directory=True)
    temporary = tmp_path / "runner"
    temporary.mkdir()
    output = tmp_path / "workflow-output"
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(temporary),
        "GITHUB_OUTPUT": str(output),
        "KORA_PRICING_EVIDENCE": str(directory),
        "KORA_PRICING_MEASUREMENT_OUTCOME": "failure",
        "KORA_PRICING_RELEASE_OUTCOME": "failure",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.measurements.pricing_lifetime_support "
            "import pricing_publication_prepare; pricing_publication_prepare()",
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=60,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    publication = PricingPublication.model_validate_json(outputs["identity"])

    try:
        (tmp_path / "publication.stderr").write_bytes(result.stderr)
        assert result.returncode == 1
        assert outputs["ready"] == "true"
        assert outputs["directory"] == str(publication.directory)
        assert marker not in result.stderr + result.stdout
        assert {path.name for path in publication.directory.iterdir()} == {
            "manifest.json",
            "completion.json",
        }
        completion = json.loads(
            (publication.directory / "completion.json").read_bytes()
        )
        assert completion["complete"] is False
        assert completion["command_exit_code"] == (None if malformed else 29)
        assert completion["working_proof_available"] is (not malformed)
        assert all(
            marker not in path.read_bytes() for path in publication.directory.iterdir()
        )
        assert (target / "private.json").read_bytes() == marker
    finally:
        release_pricing_publication(publication, temporary)
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("nested_source", ["sealed", "working"])
def test_pricing_recursive_envelope_retains_bounded_failure_publication(
    tmp_path: Path, nested_source: str
) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    marker = b"private-recursive-envelope-marker-742"
    nested = b'{"private":' + b"[" * 4096 + b'"' + marker + b'"'
    nested += b"]" * 4096 + b',"complete":false}'
    original = subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(29)"])
    original_code = original.wait(timeout=10)
    known: dict[str, JsonValue] = {
        "command_exit_code": original_code,
        "command_pid": original.pid,
        "command_reaped": True,
        "command_interruption": None,
        "private_owner_removed": None,
        "private_owner_valid": None,
    }
    (directory / "completion.json").write_bytes(
        nested if nested_source == "working" else json.dumps(known).encode()
    )
    target = tmp_path / "untouched"
    target.mkdir()
    (target / "private.json").write_bytes(marker)
    sealed = tmp_path / "evidence-sealed"
    if nested_source == "sealed":
        sealed.mkdir()
        (sealed / "completion.json").write_bytes(nested)
        (sealed / "manifest.json").write_text('{"command_complete":false}')
    else:
        sealed.symlink_to(target, target_is_directory=True)
    temporary = tmp_path / "runner"
    temporary.mkdir()
    output = tmp_path / "workflow-output"
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(temporary),
        "GITHUB_OUTPUT": str(output),
        "KORA_PRICING_EVIDENCE": str(directory),
        "KORA_PRICING_MEASUREMENT_OUTCOME": "failure",
        "KORA_PRICING_RELEASE_OUTCOME": "failure",
    }
    descriptors = len(os.listdir("/dev/fd"))

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.measurements.pricing_lifetime_support "
            "import pricing_publication_prepare; pricing_publication_prepare()",
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=60,
    )

    (tmp_path / "publication.stdout").write_bytes(result.stdout)
    (tmp_path / "publication.stderr").write_bytes(result.stderr)
    (tmp_path / "publication.exit").write_text(str(result.returncode))
    assert original_code == 29
    assert result.returncode == 1
    assert output.is_file()
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    publication = PricingPublication.model_validate_json(outputs["identity"])
    try:
        assert outputs["ready"] == "true"
        assert outputs["directory"] == str(publication.directory)
        assert marker not in result.stderr + result.stdout
        assert b"RecursionError" not in result.stderr
        assert {path.name for path in publication.directory.iterdir()} == {
            "manifest.json",
            "completion.json",
        }
        completion = json.loads(
            (publication.directory / "completion.json").read_bytes()
        )
        (tmp_path / "publication-completion.json").write_text(json.dumps(completion))
        (tmp_path / "publication-manifest.json").write_bytes(
            (publication.directory / "manifest.json").read_bytes()
        )
        assert completion["complete"] is False
        assert completion["working_proof_available"] is (nested_source == "sealed")
        for field, value in known.items():
            assert completion[field] == (value if nested_source == "sealed" else None)
        assert all(
            marker not in path.read_bytes() for path in publication.directory.iterdir()
        )
        assert (target / "private.json").read_bytes() == marker
    finally:
        release = release_pricing_publication(publication, temporary)
        (tmp_path / "publication-release.json").write_text(json.dumps(release))
    assert list(temporary.iterdir()) == []
    assert len(os.listdir("/dev/fd")) == descriptors
    with pytest.raises(ChildProcessError):
        os.waitpid(original.pid, os.WNOHANG)


@pytest.mark.parametrize(
    "fault", ["interrupt", "blocked_interrupt", "identity", "identity_interrupt"]
)
def test_pricing_publication_acquisition_preserves_masks_and_owned_cleanup(
    tmp_path: Path, fault: str
) -> None:
    temporary = tmp_path / "runner"
    temporary.mkdir()
    target = tmp_path / "foreign-target"
    target.mkdir()
    (target / "sentinel").write_bytes(b"foreign-preserved")
    proof_path = tmp_path / "acquisition-proof.json"
    moved = tmp_path / "moved-owned"
    script = """
import json
import os
import signal
import sys
from pathlib import Path
from tests.measurements.pricing_lifetime_support import (
    pricing_publication_allocation,
    release_pricing_publication,
)

root = Path(sys.argv[1])
fault = sys.argv[2]
blocked = {signal.SIGUSR1}
if fault == "blocked_interrupt":
    blocked.add(signal.SIGINT)
outer = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
expected = signal.pthread_sigmask(signal.SIG_BLOCK, set())
proof: dict[str, object] = {
    "outer_mask": sorted(int(item) for item in outer),
    "expected_mask": sorted(int(item) for item in expected),
}
try:
    try:
        with pricing_publication_allocation(root / "runner") as allocation:
            directory = allocation.allocate()
            proof["directory"] = str(directory)
            if fault in {"identity", "identity_interrupt"}:
                directory.rename(root / "moved-owned")
                directory.symlink_to(root / "foreign-target", target_is_directory=True)
            if fault != "identity":
                os.kill(os.getpid(), signal.SIGINT)
                proof["pending_before_receipt"] = sorted(
                    int(item) for item in signal.sigpending()
                )
                assert signal.SIGINT in signal.sigpending()
            publication = allocation.receipt()
            proof["receipt"] = json.loads(publication.model_dump_json())
        if fault == "blocked_interrupt":
            proof["release"] = release_pricing_publication(
                publication, root / "runner"
            )
    finally:
        proof["mask_after_owner"] = sorted(
            int(item) for item in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        )
        proof["pending_after_owner"] = sorted(
            int(item) for item in signal.sigpending()
        )
        proof["remaining_after_owner"] = sorted(
            str(item) for item in (root / "runner").iterdir()
        )
        if fault == "blocked_interrupt":
            signal.pthread_sigmask(signal.SIG_SETMASK, outer)
finally:
    proof["mask_before_outer_restore"] = sorted(
        int(item) for item in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    )
    (root / "acquisition-proof.json").write_text(json.dumps(proof))
    signal.pthread_sigmask(signal.SIG_SETMASK, outer)
"""
    descriptors = len(os.listdir("/dev/fd"))

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), fault],
        capture_output=True,
        check=False,
        timeout=10,
    )

    (tmp_path / "acquisition.stdout").write_bytes(result.stdout)
    (tmp_path / "acquisition.stderr").write_bytes(result.stderr)
    (tmp_path / "acquisition.exit").write_text(str(result.returncode))
    assert result.returncode != 0
    proof = json.loads(proof_path.read_text())
    assert proof["mask_after_owner"] == proof["expected_mask"]
    assert int(signal.SIGUSR1) in proof["expected_mask"]
    assert b"ready=true" not in result.stdout
    assert b"foreign-preserved" not in result.stdout + result.stderr
    assert (target / "sentinel").read_bytes() == b"foreign-preserved"
    if fault != "identity":
        assert int(signal.SIGINT) in proof["pending_before_receipt"]
        assert b"KeyboardInterrupt" in result.stderr
    if fault == "blocked_interrupt":
        assert int(signal.SIGINT) in proof["expected_mask"]
        assert int(signal.SIGINT) in proof["pending_after_owner"]
        assert proof["release"]["publication_released"] is True
        assert proof["mask_before_outer_restore"] == proof["outer_mask"]
    else:
        assert int(signal.SIGINT) not in proof["pending_after_owner"]
    if fault in {"identity", "identity_interrupt"}:
        assert b"publication allocation identity is unavailable" in result.stderr
        assert b"publication preparation and release failures" in result.stderr
        if fault == "identity_interrupt":
            assert (
                b"publication acquisition and signal restoration failures"
                in result.stderr
            )
        assert Path(proof["directory"]).is_symlink()
        assert moved.is_dir()
        moved.rmdir()
    else:
        assert not Path(proof["directory"]).exists()
        assert list(temporary.iterdir()) == []
    assert len(os.listdir("/dev/fd")) == descriptors
