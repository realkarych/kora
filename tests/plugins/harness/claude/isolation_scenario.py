import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import mkdtemp

from kora.conformance.capture import captured
from kora.conformance.common import utc_now
from kora.conformance.harness import ConformancePolicy, ConformanceSink, Trace
from kora.observability import LoggingSettings
from kora.plugins.harness.claude.session import COMPONENT
from kora.protocols.policy import (
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.time import Clock, SystemClock
from tests.plugins.harness.claude.contained import (
    network_counters,
    probe_outbound,
    released,
)
from tests.plugins.harness.claude.scripted_launch import ScriptedLaunches
from tests.plugins.harness.claude.scripted_model import (
    ScriptedState,
    mapping,
    objects,
    serve,
    stop_server,
)
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import context_for
from tests.plugins.harness.claude.test_session_lifecycle import session_spec


def scripted_calls(value: object) -> list[tuple[str, dict[str, object]]]:
    calls: list[tuple[str, dict[str, object]]] = []
    entries = objects(value)
    assert entries is not None
    for item in entries:
        pair = objects(item)
        assert pair is not None
        assert len(pair) == 2
        name, arguments = pair
        assert isinstance(name, str)
        payload = mapping(arguments)
        assert payload is not None
        calls.append((name, payload))
    return calls


class RaisingPolicy:
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        raise RuntimeError("kora-decision-failure-1646")


async def cancel_when_started(
    marker: Path, sink: ConformanceSink, clock: Clock
) -> None:
    deadline = clock.monotonic_ns() + 30_000_000_000
    while not marker.is_file():
        if clock.monotonic_ns() >= deadline:
            raise AssertionError("the tool child did not start")
        await clock.sleep_until(min(deadline, clock.monotonic_ns() + 50_000_000))
    sink.stop("cancel")


async def scenario(config: dict[str, object]) -> dict[str, object]:
    workspace = config["workspace"]
    markers = config["markers"]
    assert isinstance(workspace, str)
    assert isinstance(markers, str)
    contained = config.get("contained", False)
    denied_tool = config.get("denied_tool", "Forbidden")
    policy_name = config.get("policy", "conformance")
    cancel_marker = config.get("cancel_marker")
    assert isinstance(contained, bool)
    assert isinstance(denied_tool, str)
    assert policy_name in ("conformance", "raising")
    assert cancel_marker is None or isinstance(cancel_marker, str)
    before = network_counters() if contained else None
    state = ScriptedState(scripted_calls(config["calls"]))
    server, thread, base_url = serve(state)
    launches = ScriptedLaunches(
        Path(mkdtemp(prefix="ki", dir="/tmp")), Path(workspace), base_url
    )
    stderr = os.fstat(2)
    try:
        with captured(COMPONENT, LoggingSettings(), "isolation", utc_now) as capture:
            clock = SystemClock()
            adapter = make_adapter(Path(workspace), context_for(capture, clock))
            sink = ConformanceSink(None, Trace())
            policy: PolicyClient = (
                RaisingPolicy()
                if policy_name == "raising"
                else ConformancePolicy(denied_tool, sink.trace)
            )

            async with asyncio.timeout(60):
                watcher = (
                    asyncio.create_task(
                        cancel_when_started(Path(cancel_marker), sink, clock)
                    )
                    if cancel_marker is not None
                    else None
                )
                try:
                    result = await adapter.run(
                        session_spec(adapter), policy, sink, launches
                    )
                    if watcher is not None:
                        await watcher
                finally:
                    if watcher is not None and not watcher.done():
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)

            frames = [
                mapping(json.loads(line))
                for line in launches.stream.read_text().splitlines()
            ]
            init = next(
                frame
                for frame in frames
                if frame is not None and frame.get("subtype") == "init"
            )
            config_settings = launches.placements["harness_config"] / "settings.json"
            report: dict[str, object] = {
                "config_settings": config_settings.read_text()
                if config_settings.exists()
                else None,
                "cause": result.stop_reason,
                "diagnostics": capture.finish(),
                "error_class": result.error_class,
                "policy_bypass": result.policy_bypass,
                "denials": result.denials.model_dump(mode="json"),
                "init": {
                    key: init.get(key)
                    for key in (
                        "plugins",
                        "tools",
                        "mcp_servers",
                        "skills",
                        "agents",
                        "slash_commands",
                    )
                },
                "recorder": launches.recorder_report(),
                "starts": len(launches.starts.read_text().splitlines()),
                "probes": len(launches.probes.read_text().splitlines())
                if launches.probes.exists()
                else 0,
                "events": [event.model_dump(mode="json") for event in sink.events],
                "placements": {
                    name: str(path) for name, path in launches.placements.items()
                },
                "base_url": base_url,
                "child_stderr": [stderr.st_dev, stderr.st_ino],
                "markers": [str(path) for path in Path(markers).iterdir()],
                "control_socket": str(launches.root / "s"),
                "report_path": str(launches.report_path),
            }
    finally:
        await launches.close()
        stop_server(server, thread)
    bodies, clients, unauthorized = state.observed_requests()
    report.update(requests=bodies, clients=clients, unauthorized_requests=unauthorized)
    report["leftover"] = await released(
        workspace, clock, clock.monotonic_ns() + 5_000_000_000
    )
    if contained:
        after = network_counters()
        probe_outbound()
        report["network"] = {
            "before": before,
            "after": after,
            "probed": network_counters(),
        }
    else:
        report["network"] = None
    return report


if __name__ == "__main__":
    configuration = mapping(json.loads(Path(sys.argv[1]).read_text()))
    assert configuration is not None
    report = configuration["report"]
    assert isinstance(report, str)
    Path(report).write_text(json.dumps(asyncio.run(scenario(configuration))))
