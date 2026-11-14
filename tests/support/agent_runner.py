import sys
from functools import partial
from pathlib import Path
from typing import Final

from kora.ir.json import JsonObject
from kora.observability import ProcessDiagnostics
from kora.plugins.harness.fake.adapter import COMPONENT as FAKE_COMPONENT
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.supervisor import run
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import AgentExecution
from kora.protocols.policy import Policy
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from tests.plugins.harness.fake.fake_support import result, turn
from tests.runner.agent_support import PAUSE, FakeResolver, script

REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
SCRIPTS: Final[dict[str, tuple[JsonObject, ...]]] = {
    "result": (turn("m1", "hello"), result()),
    "pause": (turn("m1"), PAUSE),
}


class AllowDenyPolicies:
    def policy(self, execution: AgentExecution) -> Policy:
        return AllowDenyPolicy()


def fake_sources(scenario: str, diagnostics: ProcessDiagnostics) -> AgentSources:
    ctx = ProtocolContext(
        namespace_id="testing-runner",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(FAKE_COMPONENT),
        secrets=None,
        secret_admission=SecretScope(diagnostics).admission(),
        request_digest=None,
        deadline=None,
        clock=SystemClock(),
        activity=NoActivity(),
    )
    return AgentSources(
        FakeResolver(script(SCRIPTS[scenario]), ctx), AllowDenyPolicies()
    )


def agent_runner_command(scenario: str) -> tuple[str, ...]:
    program = "; ".join(
        (
            "import sys",
            f"sys.path.insert(0, {str(REPOSITORY)!r})",
            "from tests.support.agent_runner import main",
            f"main({scenario!r})",
        )
    )
    return (sys.executable, "-I", "-c", program)


def main(scenario: str) -> None:
    sys.exit(run(sys.argv[1:], partial(fake_sources, scenario)))
