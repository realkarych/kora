import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from kora.executor.launcher import LaunchSpec, StepLauncher
from kora.executor.steps import ToolMissing, resolve_tools
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.spawn import (
    default_socket_directory,
    runner_environment,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.runner.identity import IdentityObservation, IdentityState
from kora.runner.registry import COMPONENT

TOOL_LIMIT: Final[int] = 65536
TOOL_TIMEOUT_NS: Final[int] = 5_000_000_000
SYSTEM_DIRECTORIES: Final[tuple[str, ...]] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
UNIT_NAME: Final[re.Pattern[str]] = re.compile(
    r"(?:[A-Za-z0-9:_.@-]|\\x[0-9a-fA-F]{2}){1,255}\.(?:service|scope|slice)"
)
OBJECT_PATH: Final[re.Pattern[str]] = re.compile(r"/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+")
NUMBER: Final[re.Pattern[str]] = re.compile(r"0|[1-9][0-9]{0,19}")
DURATION: Final[re.Pattern[str]] = re.compile(
    r"(?:[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d|w|month|y|µs) ?)+"
)
LOGIN: Final[str] = "org.freedesktop.login1"
LOGIN_PATH: Final[str] = "/org/freedesktop/login1"
INVENTORY: Final[tuple[str, ...]] = ("Id", "MainPID", "ControlPID", "PAMName")
COMMON_PROPERTIES: Final[tuple[str, ...]] = (
    "Id",
    "ControlGroup",
    "LoadState",
    "ManagedOOMMemoryPressure",
    "ManagedOOMSwap",
    "TasksMax",
    "MemoryMax",
    "CPUQuotaPerSecUSec",
    "CPUWeight",
)
SERVICE_PROPERTIES: Final[tuple[str, ...]] = (
    "KillMode",
    "PrivateTmp",
    "NotifyAccess",
    "MainPID",
    "ControlPID",
    "PAMName",
)
REMEDIES: Final[dict[str, str]] = {
    "kill_mode": "set KillMode=process on the worker service",
    "linger": "enable lingering for the worker user with loginctl enable-linger",
    "kill_user_processes": "set KillUserProcesses=no in logind configuration",
    "private_tmp": "set PrivateTmp=no on the worker and its service or PAM ancestors",
    "oom_policy": "set OOMPolicy=continue on the worker unit",
    "managed_oom": "set ManagedOOMMemoryPressure=auto and ManagedOOMSwap=auto",
    "session_origin": "run the worker in an explicitly configured safe systemd service",
    "unit_identity": "run the worker in an identifiable systemd service",
    "tool_missing": "install systemctl and busctl in trusted system directories",
    "tool_failed": "restore access to the service manager and logind",
    "tool_overflow": "reduce service-manager inventory to fit the supported read bound",
    "property_invalid": "restore readable, supported service-manager properties",
}
SERVICE_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.service_refused",
        COMPONENT,
        Level.WARNING,
        "local runtime startup refused its service configuration",
        {"reason": FieldKind.STRING},
    )
)


def config_error(reason: str) -> KoraProtocolError:
    return KoraProtocolError(
        "config_error", REMEDIES[reason], False, {"reason": reason}
    )


def unit_name(value: str) -> str:
    if not UNIT_NAME.fullmatch(value):
        raise config_error("unit_identity")
    return value


def integer(value: str) -> int:
    if not NUMBER.fullmatch(value) or int(value) > 2**64 - 1:
        raise config_error("property_invalid")
    return int(value)


def choice(value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise config_error("property_invalid")
    return value


def bus_values(value: str, signature: str) -> tuple[str, ...]:
    try:
        words = shlex.split(value)
    except ValueError:
        raise config_error("property_invalid") from None
    if not words or words[0] != signature:
        raise config_error("property_invalid")
    return tuple(words[1:])


def bus_scalar(value: str, signature: Literal["b", "s", "o", "u"]) -> str:
    values = bus_values(value, signature)
    if len(values) != 1:
        raise config_error("property_invalid")
    result = values[0]
    if signature == "b":
        choice(result, ("true", "false"))
    elif signature == "o" and not OBJECT_PATH.fullmatch(result):
        raise config_error("property_invalid")
    elif signature == "u" and integer(result) > 2**32 - 1:
        raise config_error("property_invalid")
    return result


def bus_names(value: str) -> frozenset[str]:
    values = bus_values(value, "as")
    if not values or integer(values[0]) != len(values) - 1:
        raise config_error("property_invalid")
    names = frozenset(values[1:])
    if len(names) != len(values) - 1 or any(
        not name or any(c.isspace() for c in name) for name in names
    ):
        raise config_error("property_invalid")
    return names


def property_rows(output: str, required: tuple[str, ...]) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for block in output.strip().split("\n\n"):
        if not block:
            continue
        row: dict[str, str] = {}
        for line in block.splitlines():
            name, separator, value = line.partition("=")
            if not separator or name not in required or name in row:
                raise config_error("property_invalid")
            row[name] = value
        if set(row) != set(required):
            raise config_error("property_invalid")
        rows.append(row)
    return tuple(rows)


@dataclass(frozen=True)
class UnitLocation:
    name: str
    group: str
    user: bool


def cgroup_units(value: str, uid: int) -> tuple[UnitLocation, ...]:
    groups: set[str] = set()
    for line in value.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise config_error("unit_identity")
        if fields[0] == "0" and not fields[1] or "name=systemd" in fields[1].split(","):
            groups.add(fields[2])
    if len(groups) != 1:
        raise config_error("unit_identity")
    group = groups.pop()
    if not group.startswith("/") or any(
        part in (".", "..", "") for part in group.split("/")[1:]
    ):
        raise config_error("unit_identity")
    locations: list[UnitLocation] = [UnitLocation("-.slice", "/", False)]
    user = False
    parts = group.split("/")[1:]
    for position, part in enumerate(parts):
        if part.endswith((".service", ".scope", ".slice")):
            locations.append(
                UnitLocation(
                    unit_name(part), "/" + "/".join(parts[: position + 1]), user
                )
            )
        if part == f"user@{uid}.service":
            user = True
    if not any(item.name.endswith((".service", ".scope")) for item in locations):
        raise config_error("unit_identity")
    return tuple(locations)


@dataclass(frozen=True)
class ServiceFinding:
    reason: str
    refusal: bool
    unit: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class ServiceReport:
    findings: tuple[ServiceFinding, ...]
    socket_directory: Path

    def require_safe(self, ctx: ProtocolContext) -> None:
        for finding in self.findings:
            if finding.refusal:
                ctx.logger.emit(SERVICE_REFUSED, reason=finding.reason)
                raise config_error(finding.reason)


def unmanaged_services(uid: int, pid: int) -> ServiceReport:
    findings = [ServiceFinding("temporary_socket", False)]
    if pid == 1:
        findings.append(ServiceFinding("container_pid_one", False))
    return ServiceReport(tuple(findings), default_socket_directory("linux", uid))


def unit_findings(
    row: Mapping[str, str], refusal: bool, inherited: bool = False
) -> list[ServiceFinding]:
    name = unit_name(row["Id"])
    findings: list[ServiceFinding] = []
    oom = (
        None
        if name.endswith(".slice")
        else choice(row["OOMPolicy"], ("continue", "stop", "kill"))
    )
    managed = tuple(
        choice(row[key], ("auto", "kill"))
        for key in ("ManagedOOMMemoryPressure", "ManagedOOMSwap")
    )
    if not inherited:
        if oom == "kill":
            findings.append(ServiceFinding("oom_policy", refusal, name))
        elif oom == "stop":
            findings.append(ServiceFinding("oom_stops_worker", False, name))
        if "kill" in managed:
            findings.append(ServiceFinding("managed_oom", refusal, name))
    if name.endswith(".service"):
        kill = choice(row["KillMode"], ("process", "none", "mixed", "control-group"))
        private = choice(row["PrivateTmp"], ("yes", "no", "disconnected"))
        notify = choice(row["NotifyAccess"], ("none", "main", "exec", "all"))
        if not inherited and kill in ("mixed", "control-group") and refusal:
            findings.append(ServiceFinding("kill_mode", True, name))
        if private != "no":
            findings.append(ServiceFinding("private_tmp", True, name))
        if notify == "all" and not inherited:
            findings.append(ServiceFinding("notify_access", False, name))
    for key in ("TasksMax", "MemoryMax", "CPUWeight", "CPUQuotaPerSecUSec"):
        value = row[key]
        if (
            value != "infinity"
            and not (key == "CPUWeight" and value in ("idle", "[not set]"))
            and not NUMBER.fullmatch(value)
            and not (key == "CPUQuotaPerSecUSec" and DURATION.fullmatch(value))
        ):
            raise config_error("property_invalid")
        if not inherited:
            findings.append(
                ServiceFinding("shared_limit", False, name, f"{key}={value}")
            )
    return findings


class ServiceProbe:
    def __init__(
        self,
        launcher: StepLauncher,
        tools: Mapping[str, str],
        uid: int,
        pid: int,
        home: Path,
        identity: Callable[[int], Awaitable[IdentityObservation]],
    ) -> None:
        self.launcher = launcher
        self.tools = dict(tools)
        self.uid = uid
        self.pid = pid
        self.environment = runner_environment("linux", home)
        self.identity = identity

    async def tool(
        self, name: str, arguments: tuple[str, ...], user: bool = False
    ) -> str:
        path = self.tools.get(name)
        if path is None or not Path(path).is_absolute():
            raise config_error("tool_missing")
        environment = dict(self.environment)
        if user:
            environment.update(
                XDG_RUNTIME_DIR=f"/run/user/{self.uid}",
                DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{self.uid}/bus",
            )
        try:
            outcome = await self.launcher.run(
                LaunchSpec(
                    argv=(path, *arguments),
                    cwd=Path("/"),
                    env=environment,
                    stdin=None,
                    timeout_ns=TOOL_TIMEOUT_NS,
                    stdout_limit=TOOL_LIMIT,
                    stderr_limit=TOOL_LIMIT,
                )
            )
        except OSError:
            raise config_error("tool_failed") from None
        if outcome.stdout_overflow or outcome.stderr_dropped:
            raise config_error("tool_overflow")
        if not outcome.succeeded:
            raise config_error("tool_failed")
        try:
            return outcome.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeError:
            raise config_error("property_invalid") from None

    async def properties(
        self, names: tuple[str, ...], properties: tuple[str, ...], user: bool = False
    ) -> tuple[dict[str, str], ...]:
        output = await self.tool(
            "systemctl",
            (
                "--user" if user else "--system",
                "show",
                "--no-pager",
                "--all",
                "--property=" + ",".join(properties),
                "--",
                *names,
            ),
            user,
        )
        return property_rows(output, properties)

    async def unit(self, name: str, user: bool = False) -> dict[str, str]:
        unit_name(name)
        properties = (
            COMMON_PROPERTIES
            + (() if name.endswith(".slice") else ("OOMPolicy",))
            + (SERVICE_PROPERTIES if name.endswith(".service") else ())
        )
        rows = await self.properties((name,), properties, user)
        if len(rows) != 1 or rows[0]["Id"] != name or rows[0]["LoadState"] != "loaded":
            raise config_error("unit_identity")
        return rows[0]

    async def login_property(
        self,
        path: str,
        interface: str,
        name: str,
        signature: Literal["b", "s", "o", "u"],
    ) -> str:
        output = await self.tool(
            "busctl",
            ("--system", "get-property", LOGIN, path, LOGIN + "." + interface, name),
        )
        return bus_scalar(output, signature)

    async def login_object(self, method: str, identifier: int) -> str:
        output = await self.tool(
            "busctl",
            (
                "--system",
                "call",
                LOGIN,
                LOGIN_PATH,
                LOGIN + ".Manager",
                method,
                "u",
                str(identifier),
            ),
        )
        return bus_scalar(output, "o")

    async def logind(self) -> tuple[bool, bool, bool]:
        names: set[str] = set()
        for method in ("ListNames", "ListActivatableNames"):
            output = await self.tool(
                "busctl",
                (
                    "--system",
                    "call",
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    method,
                ),
            )
            names.update(bus_names(output))
        if LOGIN not in names:
            return False, False, False
        kill = await self.login_property(
            LOGIN_PATH, "Manager", "KillUserProcesses", "b"
        )
        user = await self.login_object("GetUser", self.uid)
        linger = await self.login_property(user, "User", "Linger", "b")
        return True, kill == "true", linger == "true"

    async def session_origin(
        self, scope: str, inventory: tuple[dict[str, str], ...]
    ) -> tuple[int, IdentityObservation, dict[str, str]]:
        session = await self.login_object("GetSessionByPID", self.pid)
        actual = await self.login_property(session, "Session", "Scope", "s")
        leader = integer(await self.login_property(session, "Session", "Leader", "u"))
        user = bus_values(
            await self.tool(
                "busctl",
                (
                    "--system",
                    "get-property",
                    LOGIN,
                    session,
                    LOGIN + ".Session",
                    "User",
                ),
            ),
            "(uo)",
        )
        if (
            actual != scope
            or len(user) != 2
            or integer(user[0]) != self.uid
            or not OBJECT_PATH.fullmatch(user[1])
        ):
            raise config_error("session_origin")
        observed = await self.identity(leader)
        if (
            observed.state is not IdentityState.PRESENT
            or observed.process is None
            or observed.process.dead
        ):
            raise config_error("session_origin")
        matches = [
            row
            for row in inventory
            if integer(row["MainPID"]) == leader and row["PAMName"]
        ]
        if len(matches) != 1:
            raise config_error("session_origin")
        origin = await self.unit(matches[0]["Id"])
        if (
            integer(origin["MainPID"]) != leader
            or origin["PAMName"] != matches[0]["PAMName"]
        ):
            raise config_error("session_origin")
        return leader, observed, origin

    async def inventory(self, user: bool = False) -> tuple[dict[str, str], ...]:
        rows = await self.properties(("*.service",), INVENTORY, user)
        names: set[str] = set()
        for row in rows:
            name = unit_name(row["Id"])
            if not name.endswith(".service") or name in names:
                raise config_error("unit_identity")
            names.add(name)
            if max(integer(row["MainPID"]), integer(row["ControlPID"])) > 2**32 - 1:
                raise config_error("property_invalid")
        return rows

    async def inspect(self, cgroup: str) -> ServiceReport:
        locations = cgroup_units(cgroup, self.uid)
        own = next(
            item
            for item in reversed(locations)
            if item.name.endswith((".service", ".scope"))
        )
        present, kill, linger = await self.logind()
        findings: list[ServiceFinding] = []
        if kill:
            findings.append(ServiceFinding("kill_user_processes", True))
        if own.user and not linger:
            findings.append(ServiceFinding("linger", True))
        for location in locations:
            row = await self.unit(location.name, location.user)
            if row["ControlGroup"] != location.group:
                raise config_error("unit_identity")
            findings.extend(unit_findings(row, location == own))
        inventory = await self.inventory()
        associated = [
            row
            for row in inventory
            if self.pid in (integer(row["MainPID"]), integer(row["ControlPID"]))
        ]
        for row in associated:
            if row["Id"] != own.name or own.user:
                findings.extend(unit_findings(await self.unit(row["Id"]), True))
        if own.user:
            for row in await self.inventory(user=True):
                if row["Id"] != own.name and self.pid in (
                    integer(row["MainPID"]),
                    integer(row["ControlPID"]),
                ):
                    findings.extend(
                        unit_findings(await self.unit(row["Id"], user=True), True)
                    )
        if own.name.endswith(".scope"):
            if not present:
                raise config_error("session_origin")
            leader, identity, origin = await self.session_origin(own.name, inventory)
            findings.extend(unit_findings(origin, True, inherited=True))
            (
                checked_leader,
                checked_identity,
                checked_origin,
            ) = await self.session_origin(own.name, await self.inventory())
            if (leader, identity, origin) != (
                checked_leader,
                checked_identity,
                checked_origin,
            ):
                raise config_error("session_origin")
        sockets = (
            Path(f"/run/user/{self.uid}/kora")
            if linger
            else default_socket_directory("linux", self.uid)
        )
        if not linger:
            findings.append(ServiceFinding("temporary_socket", False))
        if self.pid == 1:
            findings.append(ServiceFinding("container_pid_one", False))
        return ServiceReport(tuple(findings), sockets)


def service_tools() -> dict[str, str]:
    try:
        return resolve_tools(("systemctl", "busctl"), SYSTEM_DIRECTORIES)
    except ToolMissing:
        raise config_error("tool_missing") from None


def service_cgroup(platform: str) -> str | None:
    if platform != "linux":
        return None
    try:
        try:
            Path("/run/systemd/system").stat()
        except FileNotFoundError:
            return None
        with Path("/proc/self/cgroup").open("rb") as source:
            value = source.read(TOOL_LIMIT + 1)
        if len(value) > TOOL_LIMIT:
            raise config_error("unit_identity")
        return value.decode("utf-8", errors="strict")
    except (OSError, UnicodeError):
        raise config_error("unit_identity") from None
