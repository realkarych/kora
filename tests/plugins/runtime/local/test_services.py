import asyncio
import os
import sys
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher, StepLauncher
from kora.plugins.runtime.local.services import (
    ServiceFinding,
    ServiceProbe,
    ServiceReport,
    bus_names,
    bus_scalar,
    cgroup_units,
    property_rows,
    unit_findings,
    unmanaged_services,
)
from kora.protocols.errors import KoraProtocolError
from kora.runner.identity import (
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from tests.support.local_peers import until
from tests.support.local_runtime import open_descriptors, runtime_context
from tests.support.service_tools import (
    LingeringLauncher,
    ScriptedToolLauncher,
    ToolReply,
)

__all__ = ["kora_capture"]


def unit_output(name: str, group: str, kill_mode: str = "process") -> bytes:
    values = {
        "Id": name,
        "ControlGroup": group,
        "LoadState": "loaded",
        "ManagedOOMMemoryPressure": "auto",
        "ManagedOOMSwap": "auto",
        "TasksMax": "infinity",
        "MemoryMax": "infinity",
        "CPUWeight": "[not set]",
        "CPUQuotaPerSecUSec": "infinity",
    }
    if not name.endswith(".slice"):
        values["OOMPolicy"] = "continue"
    if name.endswith(".service"):
        values.update(
            KillMode=kill_mode,
            PrivateTmp="no",
            NotifyAccess="none",
            MainPID="23",
            ControlPID="0",
            PAMName="",
        )
    return "\n".join(f"{key}={value}" for key, value in values.items()).encode()


def direct_service_replies(
    kill_mode: str = "process", kill_user: bool = False, linger: bool = True
) -> tuple[ToolReply, ...]:
    return tuple(
        ToolReply(value)
        for value in (
            b'as 1 "org.freedesktop.login1"',
            b"as 0",
            b"b true" if kill_user else b"b false",
            b'o "/org/freedesktop/login1/user/_1001"',
            b"b true" if linger else b"b false",
            unit_output("-.slice", "/"),
            unit_output("system.slice", "/system.slice"),
            unit_output("worker.service", "/system.slice/worker.service", kill_mode),
            b"Id=worker.service\nMainPID=23\nControlPID=0\nPAMName=",
        )
    )


def session_replies(
    leader: int,
    private_tmp: bool = False,
    identified: bool = True,
    main_worker: bool = False,
) -> tuple[ToolReply, ...]:
    inventory = (
        f"Id=pam.service\nMainPID={leader}\nControlPID=0\n"
        f"PAMName={'login' if identified else ''}"
    ).encode()
    origin = (
        unit_output("pam.service", "/system.slice/pam.service", "mixed")
        .replace(b"MainPID=23", f"MainPID={leader}".encode())
        .replace(b"PAMName=", b"PAMName=login")
    )
    if private_tmp:
        origin = origin.replace(b"PrivateTmp=no", b"PrivateTmp=yes")
    prefix = direct_service_replies()[:5] + tuple(
        ToolReply(value)
        for value in (
            unit_output("-.slice", "/"),
            unit_output("user.slice", "/user.slice"),
            unit_output("user-1001.slice", "/user.slice/user-1001.slice"),
            unit_output(
                "session-7.scope", "/user.slice/user-1001.slice/session-7.scope"
            ),
            inventory,
        )
    )
    session = tuple(
        ToolReply(value)
        for value in (
            b'o "/org/freedesktop/login1/session/_7"',
            b's "session-7.scope"',
            f"u {leader}".encode(),
            b'(uo) 1001 "/org/freedesktop/login1/user/_1001"',
            origin,
        )
    )
    return (
        prefix
        + ((ToolReply(origin),) if main_worker else ())
        + session
        + (ToolReply(inventory),)
        + session
    )


def user_service_replies(private_tmp: bool, linger: bool) -> tuple[ToolReply, ...]:
    prefix = "/user.slice/user-1001.slice/user@1001.service"
    manager = unit_output("user@1001.service", prefix, "mixed").replace(
        b"MainPID=23", b"MainPID=42"
    )
    if private_tmp:
        manager = manager.replace(b"PrivateTmp=no", b"PrivateTmp=yes")
    return direct_service_replies(linger=linger)[:5] + tuple(
        ToolReply(value)
        for value in (
            unit_output("-.slice", "/"),
            unit_output("user.slice", "/user.slice"),
            unit_output("user-1001.slice", "/user.slice/user-1001.slice"),
            manager,
            unit_output("app.slice", prefix + "/app.slice"),
            unit_output("worker.service", prefix + "/app.slice/worker.service"),
            b"Id=user@1001.service\nMainPID=42\nControlPID=0\nPAMName=systemd-user",
            b"Id=worker.service\nMainPID=23\nControlPID=0\nPAMName=",
        )
    )


async def absent_identity(pid: int) -> IdentityObservation:
    return IdentityObservation(IdentityState.ABSENT)


def service_properties() -> dict[str, str]:
    return {
        "Id": "worker.service",
        "OOMPolicy": "continue",
        "ManagedOOMMemoryPressure": "auto",
        "ManagedOOMSwap": "auto",
        "KillMode": "process",
        "PrivateTmp": "no",
        "NotifyAccess": "main",
        "TasksMax": "1000",
        "MemoryMax": "infinity",
        "CPUWeight": "100",
        "CPUQuotaPerSecUSec": "500ms",
    }


@pytest.mark.parametrize(
    ("property_name", "value", "reason"),
    [
        ("KillMode", "control-group", "kill_mode"),
        ("KillMode", "mixed", "kill_mode"),
        ("PrivateTmp", "yes", "private_tmp"),
        ("PrivateTmp", "disconnected", "private_tmp"),
        ("OOMPolicy", "kill", "oom_policy"),
        ("ManagedOOMMemoryPressure", "kill", "managed_oom"),
        ("ManagedOOMSwap", "kill", "managed_oom"),
    ],
)
def test_service_manager_refusals(property_name: str, value: str, reason: str) -> None:
    properties = service_properties()
    properties[property_name] = value

    findings = unit_findings(properties, refusal=True)

    assert [finding.reason for finding in findings if finding.refusal] == [reason]


def test_pam_wrapper_private_tmp_is_checked_without_its_kill_mode() -> None:
    properties = service_properties()
    properties.update(KillMode="mixed", PrivateTmp="yes")

    findings = unit_findings(properties, refusal=True, inherited=True)

    assert [finding.reason for finding in findings] == ["private_tmp"]


@pytest.mark.parametrize(
    "property_name",
    ["OOMPolicy", "KillMode", "PrivateTmp", "TasksMax", "CPUQuotaPerSecUSec"],
)
def test_unreadable_service_property_is_not_defaulted(property_name: str) -> None:
    properties = service_properties()
    properties[property_name] = ""

    with pytest.raises(KoraProtocolError) as failure:
        unit_findings(properties, refusal=True)

    assert failure.value.code == "config_error"
    assert failure.value.details == {"reason": "property_invalid"}


def test_ancestor_oom_settings_are_findings_without_startup_refusals() -> None:
    properties = service_properties()
    properties.update(OOMPolicy="kill", ManagedOOMSwap="kill")

    findings = unit_findings(properties, refusal=False)

    assert not any(finding.refusal for finding in findings)
    assert {finding.reason for finding in findings} == {
        "oom_policy",
        "managed_oom",
        "shared_limit",
    }


def test_user_manager_private_tmp_is_an_inherited_refusal() -> None:
    properties = service_properties()
    properties.update(Id="user@1001.service", PrivateTmp="yes", KillMode="mixed")

    findings = unit_findings(properties, refusal=False)

    assert [finding.reason for finding in findings if finding.refusal] == [
        "private_tmp"
    ]


@pytest.mark.parametrize(
    "output", ["b yes", "s true", "b true false", "b", 'b "true', "b 1"]
)
def test_logind_boolean_requires_one_typed_value(output: str) -> None:
    with pytest.raises(KoraProtocolError) as failure:
        bus_scalar(output, "b")

    assert failure.value.details == {"reason": "property_invalid"}


@pytest.mark.parametrize(
    "output",
    [
        'as 2 "org.freedesktop.login1"',
        'as 2 "same" "same"',
        's "org.freedesktop.login1"',
    ],
)
def test_bus_inventory_requires_complete_unique_array(output: str) -> None:
    with pytest.raises(KoraProtocolError) as failure:
        bus_names(output)

    assert failure.value.details == {"reason": "property_invalid"}


def test_activatable_logind_is_retained_by_typed_parser() -> None:
    output = 'as 2 "org.freedesktop.login1" "org.freedesktop.systemd1"\n'

    names = bus_names(output)

    assert names == {"org.freedesktop.login1", "org.freedesktop.systemd1"}


@pytest.mark.parametrize(
    "output",
    [
        "Id=worker.service\nId=other.service",
        "Id=worker.service\n",
        "Id=worker.service\nExtra=yes\nMainPID=3",
    ],
)
def test_unit_properties_require_complete_unambiguous_fields(output: str) -> None:
    with pytest.raises(KoraProtocolError) as failure:
        property_rows(output, ("Id", "MainPID"))

    assert failure.value.details == {"reason": "property_invalid"}


@pytest.mark.parametrize(
    ("path", "unit", "user"),
    [
        ("/system.slice/worker.service/delegated", "worker.service", False),
        ("/user.slice/user-1001.slice/session-7.scope", "session-7.scope", False),
        (
            "/user.slice/user-1001.slice/user@1001.service/app.slice/worker.service",
            "worker.service",
            True,
        ),
    ],
)
def test_cgroup_manager_ownership(path: str, unit: str, user: bool) -> None:
    output = f"0::{path}\n"

    locations = cgroup_units(output, 1001)

    assert locations[-1].name == unit
    assert locations[-1].user is user


@pytest.mark.parametrize(
    "output",
    [
        "0::/\n",
        "0::/system.slice/../worker.service\n",
        "0::/system.slice/worker*.service\n",
        "0::/a.service\n1:name=systemd:/b.service\n",
    ],
)
def test_cgroup_unknown_or_ambiguous_unit_refuses(output: str) -> None:
    with pytest.raises(KoraProtocolError) as failure:
        cgroup_units(output, 1001)

    assert failure.value.details == {"reason": "unit_identity"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("import sys; sys.exit(3)", "tool_failed"),
        ("import os; os.write(1, b'x' * 65537)", "tool_overflow"),
        ("import os; os.write(2, b'x' * 65537)", "tool_overflow"),
        ("import os; os.write(1, bytes([255]))", "property_invalid"),
    ],
)
async def test_service_tool_refuses_real_process_failures(
    tmp_path: Path, code: str, reason: str
) -> None:
    probe = ServiceProbe(
        ProcessLauncher(),
        {"systemctl": sys.executable},
        os.getuid(),
        os.getpid(),
        tmp_path,
        absent_identity,
    )

    with pytest.raises(KoraProtocolError) as failure:
        await probe.tool("systemctl", ("-I", "-c", code))

    assert failure.value.code == "config_error"
    assert failure.value.details == {"reason": reason}


@pytest.mark.asyncio
async def test_service_tool_constructs_its_environment(tmp_path: Path) -> None:
    probe = ServiceProbe(
        ProcessLauncher(),
        {"busctl": "/usr/bin/env"},
        1001,
        os.getpid(),
        tmp_path,
        absent_identity,
    )

    output = await probe.tool("busctl", (), user=True)

    assert dict(line.split("=", 1) for line in output.splitlines()) == {
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": "/run/user/1001",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "kill_user", "linger", "refusals"),
    [
        ("process", False, True, set[str]()),
        ("mixed", False, True, {"kill_mode"}),
        ("process", True, True, {"kill_user_processes"}),
        ("process", False, False, set[str]()),
    ],
)
async def test_service_manager_complete_discovery(
    tmp_path: Path, mode: str, kill_user: bool, linger: bool, refusals: set[str]
) -> None:
    launcher = ScriptedToolLauncher(direct_service_replies(mode, kill_user, linger))
    probe = ServiceProbe(
        launcher,
        {"busctl": "/usr/bin/busctl", "systemctl": "/usr/bin/systemctl"},
        1001,
        23,
        tmp_path,
        absent_identity,
    )

    report = await probe.inspect("0::/system.slice/worker.service\n")

    assert {
        finding.reason for finding in report.findings if finding.refusal
    } == refusals
    assert report.socket_directory == (
        Path("/run/user/1001/kora") if linger else Path("/", "tmp", "kora-1001")
    )
    assert len(launcher.calls) == len(launcher.replies)
    assert all(
        call.stdout_limit == 65536
        and call.stderr_limit == 65536
        and call.timeout_ns == 5_000_000_000
        for call in launcher.calls
    )
    assert launcher.calls[7].argv[-2:] == ("--", "worker.service")
    assert launcher.calls[3].argv[-3:] == ("GetUser", "u", "1001")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [Ending.TIMED_OUT, Ending.UNKNOWN])
async def test_service_tool_uncertain_ending_refuses(
    tmp_path: Path, ending: Ending
) -> None:
    launcher = ScriptedToolLauncher((ToolReply(b"b false", ending=ending),))
    probe = ServiceProbe(
        launcher, {"busctl": "/usr/bin/busctl"}, 1001, 23, tmp_path, absent_identity
    )

    with pytest.raises(KoraProtocolError) as failure:
        await probe.tool("busctl", ())

    assert failure.value.details == {"reason": "tool_failed"}


@pytest.mark.asyncio
async def test_service_inventory_rejects_duplicate_units(tmp_path: Path) -> None:
    row = b"Id=worker.service\nMainPID=23\nControlPID=0\nPAMName=login"
    launcher = ScriptedToolLauncher((ToolReply(row + b"\n\n" + row),))
    probe = ServiceProbe(
        launcher,
        {"systemctl": "/usr/bin/systemctl"},
        1001,
        23,
        tmp_path,
        absent_identity,
    )

    with pytest.raises(KoraProtocolError) as failure:
        await probe.inventory()

    assert failure.value.details == {"reason": "unit_identity"}


@pytest.mark.asyncio
@pytest.mark.parametrize("constructed", ["native", "scripted", "linger"])
@pytest.mark.parametrize("exit_code", [0, 3])
async def test_step_launcher_capture_contract(
    tmp_path: Path, constructed: str, exit_code: int
) -> None:
    launcher: StepLauncher = (
        ScriptedToolLauncher((ToolReply(b"abcdef", b"uvwxyz", exit_code),))
        if constructed == "scripted"
        else LingeringLauncher(ProcessLauncher())
        if constructed == "linger"
        else ProcessLauncher()
    )
    code = (
        "import os, sys; os.write(1, b'abcdef'); os.write(2, b'uvwxyz'); "
        f"sys.exit({exit_code})"
    )
    spec = LaunchSpec(
        (sys.executable, "-I", "-c", code), tmp_path, {}, None, 5_000_000_000, 4, 3
    )

    result = await launcher.run(spec)

    assert result.ending is Ending.EXITED
    assert result.exit_code == exit_code
    assert result.stdout == b"abcd"
    assert result.stdout_overflow
    assert result.stderr == b"uvw"
    assert result.stderr_dropped == 3


def test_no_systemd_still_reports_socket_and_pid_one() -> None:
    report = unmanaged_services(1001, 1)

    assert {finding.reason for finding in report.findings} == {
        "temporary_socket",
        "container_pid_one",
    }
    assert not any(finding.refusal for finding in report.findings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private_tmp", "main_worker", "expected"),
    [
        (False, False, set[str]()),
        (True, False, {"private_tmp"}),
        (False, True, {"kill_mode"}),
    ],
)
async def test_session_origin_and_main_process_refusals(
    tmp_path: Path, private_tmp: bool, main_worker: bool, expected: set[str]
) -> None:
    leader = os.getpid()
    identity = NativeIdentityReader().read(leader)
    launcher = ScriptedToolLauncher(
        session_replies(leader, private_tmp, main_worker=main_worker)
    )

    async def read_identity(pid: int) -> IdentityObservation:
        assert pid == leader
        return identity

    worker = leader if main_worker else leader + 1
    probe = ServiceProbe(
        launcher,
        {"busctl": "/usr/bin/busctl", "systemctl": "/usr/bin/systemctl"},
        1001,
        worker,
        tmp_path,
        read_identity,
    )

    report = await probe.inspect("0::/user.slice/user-1001.slice/session-7.scope\n")

    assert {
        finding.reason for finding in report.findings if finding.refusal
    } == expected
    assert len(launcher.calls) == len(launcher.replies)
    assert any(
        call.argv[-3:] == ("GetSessionByPID", "u", str(worker))
        for call in launcher.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["unknown", "dead", "changed"])
async def test_session_unknown_or_changed_origin_refuses(
    tmp_path: Path, failure_mode: str
) -> None:
    leader = os.getpid()
    identity = NativeIdentityReader().read(leader)
    launcher = ScriptedToolLauncher(
        session_replies(leader, identified=failure_mode != "unknown")
    )
    reads = 0

    async def read_identity(pid: int) -> IdentityObservation:
        nonlocal reads
        assert pid == leader
        reads += 1
        if failure_mode == "dead" or failure_mode == "changed" and reads > 1:
            return IdentityObservation(IdentityState.ABSENT)
        return identity

    probe = ServiceProbe(
        launcher,
        {"busctl": "/usr/bin/busctl", "systemctl": "/usr/bin/systemctl"},
        1001,
        leader + 1,
        tmp_path,
        read_identity,
    )

    with pytest.raises(KoraProtocolError) as failure:
        await probe.inspect("0::/user.slice/user-1001.slice/session-7.scope\n")

    assert failure.value.details == {"reason": "session_origin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_local_runtime_releases_service_tool_resources(
    tmp_path: Path, cancelled: bool
) -> None:
    pid_file = tmp_path / "tool.pid"
    probe = ServiceProbe(
        ProcessLauncher(kill_grace_ns=100_000_000),
        {"busctl": sys.executable},
        os.getuid(),
        os.getpid(),
        tmp_path,
        absent_identity,
    )
    code = (
        "import os, pathlib, signal, sys; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); signal.pause()"
    )
    descriptors = open_descriptors()
    tasks = set(asyncio.all_tasks())
    running = asyncio.create_task(
        probe.tool("busctl", ("-I", "-c", code, str(pid_file)))
    )
    try:
        await until(lambda: pid_file.exists() and pid_file.stat().st_size > 0)
        pid = int(pid_file.read_text())

        if cancelled:
            running.cancel()
        outcome = await asyncio.gather(running, return_exceptions=True)

        if cancelled:
            assert isinstance(outcome[0], asyncio.CancelledError)
        else:
            assert isinstance(outcome[0], KoraProtocolError)
            assert outcome[0].details == {"reason": "tool_failed"}
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert open_descriptors() == descriptors
        assert set(asyncio.all_tasks()) == tasks
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private_tmp", "linger", "expected"),
    [
        (False, True, set[str]()),
        (True, True, {"private_tmp"}),
        (False, False, {"linger"}),
    ],
)
async def test_user_manager_inherited_settings(
    tmp_path: Path, private_tmp: bool, linger: bool, expected: set[str]
) -> None:
    launcher = ScriptedToolLauncher(user_service_replies(private_tmp, linger))
    probe = ServiceProbe(
        launcher,
        {"busctl": "/usr/bin/busctl", "systemctl": "/usr/bin/systemctl"},
        1001,
        23,
        tmp_path,
        absent_identity,
    )
    group = (
        "0::/user.slice/user-1001.slice/user@1001.service/app.slice/worker.service\n"
    )

    report = await probe.inspect(group)

    assert {
        finding.reason for finding in report.findings if finding.refusal
    } == expected
    assert len(launcher.calls) == len(launcher.replies)
    assert launcher.calls[-1].argv[1] == "--user"
    assert (
        launcher.calls[-1].env["DBUS_SESSION_BUS_ADDRESS"]
        == "unix:path=/run/user/1001/bus"
    )


def test_service_refusal_diagnostics_exclude_unit_content(
    tmp_path: Path, kora_capture: Capture
) -> None:
    marker = "service-private-content-7a2f"
    report = ServiceReport(
        (ServiceFinding("private_tmp", True, marker + ".service", marker),), tmp_path
    )

    with pytest.raises(KoraProtocolError) as failure:
        report.require_safe(runtime_context(kora_capture))
    records = kora_capture.finish()

    assert failure.value.details == {"reason": "private_tmp"}
    assert marker not in str(failure.value)
    assert marker not in str(records)
    assert len(records) == 1
    assert records[0]["event"] == "runtime.local.service_refused"


@pytest.mark.asyncio
async def test_lingering_fixture_changes_only_the_typed_user_property(
    tmp_path: Path,
) -> None:
    native = ScriptedToolLauncher(direct_service_replies()[:4])
    probe = ServiceProbe(
        LingeringLauncher(native),
        {"busctl": "/usr/bin/busctl"},
        1001,
        os.getpid(),
        tmp_path,
        absent_identity,
    )

    observed = await probe.logind()

    assert observed == (True, False, False)
    assert len(native.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("presence", ["absent", "activatable"])
async def test_service_discovery_uses_positive_logind_presence(
    tmp_path: Path, presence: str
) -> None:
    original = direct_service_replies()
    replies = (
        (ToolReply(b"as 0"), ToolReply(b"as 0"), *original[5:])
        if presence == "absent"
        else (ToolReply(b"as 0"), original[0], *original[2:])
    )
    probe = ServiceProbe(
        ScriptedToolLauncher(replies),
        {"systemctl": "/usr/bin/systemctl", "busctl": "/usr/bin/busctl"},
        1001,
        23,
        tmp_path,
        absent_identity,
    )

    report = await probe.inspect("0::/system.slice/worker.service\n")

    assert not any(finding.refusal for finding in report.findings)
    assert report.socket_directory == (
        Path("/", "tmp", "kora-1001")
        if presence == "absent"
        else Path("/run/user/1001/kora")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["tool", "bus", "user"])
async def test_service_discovery_refuses_missing_native_inputs(
    tmp_path: Path, failure_mode: str
) -> None:
    original = direct_service_replies()
    replies = (
        (*original[:3], ToolReply(b"", exit_code=1))
        if failure_mode == "user"
        else (ToolReply(b"", exit_code=1),)
    )
    probe = ServiceProbe(
        ScriptedToolLauncher(replies),
        {} if failure_mode == "tool" else {"busctl": "/usr/bin/busctl"},
        1001,
        23,
        tmp_path,
        absent_identity,
    )

    with pytest.raises(KoraProtocolError) as failure:
        await probe.inspect("0::/system.slice/worker.service\n")

    assert failure.value.details == {
        "reason": "tool_missing" if failure_mode == "tool" else "tool_failed"
    }
