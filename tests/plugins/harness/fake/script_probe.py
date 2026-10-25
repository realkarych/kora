import asyncio
import json
import sys
from datetime import UTC, datetime


def main(arguments: list[str]) -> None:
    repository, root = arguments
    sys.path.insert(0, repository)
    from kora.conformance.capture import captured
    from kora.conformance.common import CONFORMANCE_COMPONENT, context_for
    from kora.conformance.harness import NoLaunches
    from kora.observability import LoggingSettings
    from kora.protocols.vcs import WorkspaceTarget
    from tests.plugins.harness.fake.fake_support import (
        AllowingPolicy,
        ScriptedSink,
        harness,
        result,
        scripted,
        turn,
    )

    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(),
        "script-probe",
        lambda: datetime.now(UTC),
    ) as capture:
        adapter = harness(scripted([turn("m1"), result()]), context_for(capture), root)
        sink = ScriptedSink()
        spec = adapter.build_session_spec(
            {}, WorkspaceTarget(root=root, host_path=None), None
        )
        outcome = asyncio.run(adapter.run(spec, AllowingPolicy(), sink, NoLaunches()))
    sys.stdout.write(
        json.dumps({"kinds": sink.kinds(), "stop_reason": outcome.stop_reason})
    )


if __name__ == "__main__":
    main(sys.argv[1:])
