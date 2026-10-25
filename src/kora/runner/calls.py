from collections import OrderedDict
from dataclasses import dataclass
from typing import Final, Literal

from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    MAX_SAMPLE_ITEMS,
    DenialSample,
    Enforcement,
    UnconfirmedCall,
    UnconfirmedSample,
)
from kora.protocols.policy import (
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.runner.outcome import Bypass

LIMITED_KINDS: Final[frozenset[str]] = frozenset({"turn", "tool_call", "tool_result"})
TRACKING_CEILING: Final[int] = 100_000
GRACE_DENIALS: Final[int] = 1_000
MAX_DECIDED: Final[int] = TRACKING_CEILING + GRACE_DENIALS
OUTPUT_CHANNEL_TOOL: Final[str] = "StructuredOutput"
PolicyFailure = Literal["policy_error", "policy_timeout"]


class PolicyCallFailed(Exception):
    def __init__(self, kind: Literal["error", "timeout"]) -> None:
        super().__init__(f"the policy call failed: {kind}")
        self.kind: Literal["error", "timeout"] = kind


class EventCount:
    def __init__(self, max_events: int | None) -> None:
        if max_events is None:
            self.limit = TRACKING_CEILING
        else:
            self.limit = min(max_events, TRACKING_CEILING)
        self.events = 0

    def count(self, kind: str) -> Enforcement:
        if kind in LIMITED_KINDS and self.events < self.limit:
            self.events += 1
        return self.enforcement()

    def enforcement(self) -> Enforcement:
        if self.events >= self.limit:
            return Enforcement("terminate", "max_events")
        return Enforcement("continue", None)


@dataclass(frozen=True)
class CallVerdict:
    allow: bool
    counted: bool
    output_channel: bool
    cause: PolicyFailure | None
    decision: PolicyDecision | None


class ToolCallLedger:
    def __init__(self) -> None:
        self.decided_ids: set[str] = set()
        self.decided_overflow = 0
        self.held: OrderedDict[str, UnconfirmedCall] = OrderedDict()
        self.held_overflow = 0
        self.confirmed = False

    def decided(self, tool_use_id: str) -> None:
        if tool_use_id in self.decided_ids:
            return
        if len(self.decided_ids) >= MAX_DECIDED:
            self.decided_overflow += 1
            return
        self.decided_ids.add(tool_use_id)

    def result(self, tool: str, tool_use_id: str, is_error: bool) -> bool:
        if tool_use_id in self.decided_ids:
            return False
        if not is_error:
            self.confirmed = True
            return True
        if tool_use_id not in self.held and len(self.held) >= MAX_SAMPLE_ITEMS:
            self.held_overflow += 1
            self.confirmed = True
            return True
        self.held[tool_use_id] = UnconfirmedCall(tool=tool, tool_use_id=tool_use_id)
        return False

    def bypass(self, denials: DenialSample, result_seen: bool) -> Bypass:
        denied = {
            item.tool_use_id for item in denials.items if item.source == "harness"
        }
        unmatched = tuple(
            call for call in self.held.values() if call.tool_use_id not in denied
        )
        confirmed = self.confirmed or (result_seen and bool(unmatched))
        pending = () if result_seen else unmatched
        return Bypass(
            confirmed=confirmed,
            unconfirmed=UnconfirmedSample(items=pending, total=len(pending)),
        )


def closed_exception(
    ledger: ToolCallLedger,
    enforcement: Enforcement,
    tool: str,
    tool_use_id: str,
    agent_id: str | None,
    output_format: bool,
) -> CallVerdict | None:
    if enforcement.cause == "max_events":
        ledger.decided(tool_use_id)
        return CallVerdict(False, False, False, None, None)
    if tool == OUTPUT_CHANNEL_TOOL and output_format and agent_id is None:
        ledger.decided(tool_use_id)
        return CallVerdict(True, True, True, None, None)
    return None


def refused_call(ledger: ToolCallLedger, tool_use_id: str) -> CallVerdict:
    ledger.decided(tool_use_id)
    return CallVerdict(False, True, False, None, None)


async def decide_call(
    ledger: ToolCallLedger,
    enforcement: Enforcement,
    policy: PolicyClient,
    request: ToolCallRequest,
    layers: PolicyLayers,
    protected_paths: tuple[str, ...],
    output_format: bool,
) -> CallVerdict:
    closed = closed_exception(
        ledger,
        enforcement,
        request.tool,
        request.tool_use_id,
        request.agent_id,
        output_format,
    )
    if closed is not None:
        return closed
    ledger.decided(request.tool_use_id)
    try:
        decision: object = await policy.decide(request, layers, protected_paths)
    except PolicyCallFailed as failure:
        cause: PolicyFailure = (
            "policy_timeout" if failure.kind == "timeout" else "policy_error"
        )
        return CallVerdict(False, True, False, cause, None)
    except KoraProtocolError:
        return CallVerdict(False, True, False, "policy_error", None)
    if type(decision) is not PolicyDecision:
        return CallVerdict(False, True, False, "policy_error", None)
    return CallVerdict(decision.allow, True, False, None, decision)
