from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from enum import StrEnum

from kora.conformance.common import check
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import (
    ChangeRequestCall,
    ChangeRequestDispatch,
    ChangeRequestFact,
    ChangeRequestResult,
    ChangeRequestSearch,
    VcsChangeRequests,
)


class ChangeRequestCase(StrEnum):
    CREATE = "create"
    PREEXISTING = "preexisting"
    RESPONSE_LOSS = "response_loss"


@dataclass(frozen=True)
class ChangeRequestCounts:
    posts: int
    pull_requests: int


@dataclass(frozen=True)
class ChangeRequestExercise:
    dispatch: ChangeRequestDispatch
    precreate: ChangeRequestCall
    recovery: ChangeRequestCall
    expected: ChangeRequestFact
    counts: Callable[[], ChangeRequestCounts]

    def check_counts(self, posts: int, pull_requests: int) -> None:
        check(
            self.counts() == ChangeRequestCounts(posts, pull_requests),
            "unexpected POST or pull request count",
        )


@dataclass(frozen=True)
class ChangeRequestConformanceInputs:
    prepare: Callable[[ChangeRequestCase, ProtocolContext], ChangeRequestExercise]


def call_context(ctx: ProtocolContext, call: ChangeRequestCall) -> ProtocolContext:
    grant = call.grant
    return replace(
        ctx,
        namespace_id=grant.namespace_id,
        attempt_id=grant.request.attempt_id,
        secret_admission=grant.scope.admission(),
    )


async def check_change_request(
    provider: VcsChangeRequests,
    inputs: ChangeRequestConformanceInputs,
    scenario: ChangeRequestCase,
    ctx: ProtocolContext,
) -> None:
    case = inputs.prepare(scenario, ctx)
    with ExitStack() as scopes:
        for call in (case.precreate, case.dispatch.call, case.recovery):
            scopes.callback(call.grant.scope.release)

        fact = case.expected
        observed = ChangeRequestResult("observed", (fact.identity,), fact)
        precreate = await provider.lookup_change_request(
            ChangeRequestSearch(case.precreate, "precreate"),
            call_context(ctx, case.precreate),
        )
        if scenario is ChangeRequestCase.PREEXISTING:
            check(
                precreate == observed,
                "lookup preserves the request and its actual opener",
            )
            case.check_counts(0, 1)
            return
        check(
            precreate == ChangeRequestResult("absent"),
            "precreate finds complete absence",
        )
        case.check_counts(0, 0)

        creation = await provider.create_change_request(
            case.dispatch, call_context(ctx, case.dispatch.call)
        )
        confirmed = scenario is ChangeRequestCase.CREATE
        dispatch = (
            replace(observed, outcome="created", acknowledged_create=True)
            if confirmed
            else ChangeRequestResult("unresolved")
        )
        check(creation == dispatch, "unexpected create acknowledgment or exact facts")
        case.check_counts(1, 1)

        known = fact.identity.number if confirmed else None
        recovered = await provider.lookup_change_request(
            ChangeRequestSearch(case.recovery, "historical", known),
            call_context(ctx, case.recovery),
        )
        check(recovered == observed, "historical read preserves the exact request")
        case.check_counts(1, 1)
