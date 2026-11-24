import asyncio
import json
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.github import (
    ChangeRequestCounts,
    ChangeRequestExercise,
    call_context,
)
from kora.observability import Level, LoggingSettings
from kora.plugins.vcs.git import GitVcs, create_provider
from kora.plugins.vcs.git.github_model import GitHubConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.secret_value import SecretScope
from kora.protocols.vcs import (
    ChangeRequestIdentity,
    ChangeRequestResult,
    ChangeRequestSearch,
)
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.github_exercises import (
    exercise_for,
)
from tests.support.configured_conformance import vcs_configuration
from tests.support.github_host import (
    CREDENTIAL_MARKER,
    HOST_TEXT_MARKER,
    NUMBER,
    ChangeRequestCase,
    GitHubState,
    github_host,
    host_configuration,
    published_git,
)
from tests.support.https_fixture import create_authority


@dataclass(frozen=True)
class Exercise:
    provider: GitVcs
    context: ProtocolContext
    case: ChangeRequestExercise
    server: GitHubState
    capture: Capture

    async def lookup(self, *, historical: bool = False) -> ChangeRequestResult:
        call = self.case.recovery if historical else self.case.precreate
        mode = "historical" if historical else "precreate"
        return await self.provider.lookup_change_request(
            ChangeRequestSearch(call, mode), call_context(self.context, call)
        )

    async def dispatch(self) -> ChangeRequestResult:
        request = self.case.dispatch
        return await self.provider.create_change_request(
            request, call_context(self.context, request.call)
        )


@contextmanager
def exercise(root: Path, scenario: ChangeRequestCase) -> Generator[Exercise]:
    tls = root / "tls"
    tls.mkdir(mode=0o700)
    authority = create_authority(tls)
    published = published_git(root)
    state = GitHubState(published)
    settings = LoggingSettings(component_levels={CONFORMANCE_COMPONENT: Level.DEBUG})
    with (
        github_host(state, tls / "server.pem") as host,
        captured(
            CONFORMANCE_COMPONENT, settings, "test-github-refusal", utc_now
        ) as log,
        ExitStack() as scopes,
    ):
        binding = host_configuration(host.origin, authority.bundle)
        configuration = vcs_configuration(root)
        remotes = configuration["remotes"]
        assert isinstance(remotes, dict)
        configuration["remotes"] = {"local": remotes["local"]}
        configuration["change_requests"] = binding
        provider = create_provider(configuration)
        context = context_for(log)

        def counts() -> ChangeRequestCounts:
            return ChangeRequestCounts(*state.cases[scenario].counts())

        def scope() -> SecretScope:
            registered = SecretScope(log.diagnostics)
            scopes.callback(registered.release)
            return registered

        case = exercise_for(
            context,
            GitHubConfiguration.model_validate(binding),
            published.head,
            published.base,
            scenario.value,
            counts,
            scope,
        )
        yield Exercise(provider, context, case, state, log)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "outcome", "count"),
    [
        (ChangeRequestCase.MULTIPLE, "conflict", 2),
        (ChangeRequestCase.MALFORMED, "unresolved", 0),
        (ChangeRequestCase.INCOMPLETE, "unresolved", 1),
        (ChangeRequestCase.OTHER_REPOSITORY, "unresolved", 0),
    ],
)
async def test_github_precreate_refuses_search_without_authorizing_a_post(
    tmp_path: Path, scenario: ChangeRequestCase, outcome: str, count: int
) -> None:
    with exercise(tmp_path, scenario) as test:
        repository_id = test.case.expected.identity.repository_id
        expected_ids = tuple(
            ChangeRequestIdentity(repository_id, NUMBER + index)
            for index in range(count)
        )

        observed = await test.lookup()

        assert observed.outcome == outcome
        assert observed.fact is None
        assert not observed.acknowledged_create
        assert observed.identities == expected_ids
        assert test.case.counts() == ChangeRequestCounts(0, count)
        assert test.server.posts == 0
        assert test.server.cases[scenario].list_reads == int(
            scenario is not ChangeRequestCase.OTHER_REPOSITORY
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [ChangeRequestCase.MOVED, ChangeRequestCase.OTHER_OPENER, ChangeRequestCase.ECHO],
)
async def test_github_create_preserves_effect_when_enrichment_is_refused(
    tmp_path: Path, scenario: ChangeRequestCase
) -> None:
    with exercise(tmp_path, scenario) as test:
        assert await test.lookup() == ChangeRequestResult("absent")

        observed = await test.dispatch()

        assert observed == ChangeRequestResult(
            "conflict", (test.case.expected.identity,), acknowledged_create=True
        )
        assert test.case.counts() == ChangeRequestCounts(1, 1)
        assert test.server.posts == 1
        if scenario is ChangeRequestCase.MOVED:
            assert test.server.published.head == test.server.published.revision(
                test.case.precreate.intent.published.branch
            )
            assert test.server.cases[scenario].records[0].head == (
                test.server.published.alternate
            )
        records = test.capture.finish()
        assert [
            (record.get("operation"), record.get("outcome"))
            for record in records
            if record.get("event") == "vcs.git.change_request"
        ] == [("lookup", "absent"), ("create", "conflict")]
        assert CREDENTIAL_MARKER not in (repr(observed) + json.dumps(records))
        assert HOST_TEXT_MARKER not in json.dumps(records)


@pytest.mark.asyncio
async def test_github_generic_create_refusal_stays_unresolved(
    tmp_path: Path,
) -> None:
    with exercise(tmp_path, ChangeRequestCase.REFUSED) as test:
        assert await test.lookup() == ChangeRequestResult("absent")

        created = await test.dispatch()
        observed = await test.lookup(historical=True)

        assert created == observed == ChangeRequestResult("unresolved")
        assert test.case.counts() == ChangeRequestCounts(1, 0)
        assert test.server.posts == 1


@pytest.mark.asyncio
async def test_github_same_app_competing_effect_carries_observation_only(
    tmp_path: Path,
) -> None:
    with exercise(tmp_path, ChangeRequestCase.COMPETING_WRITER) as test:
        assert await test.lookup() == ChangeRequestResult("absent")

        lost, _ = await asyncio.gather(
            test.dispatch(),
            asyncio.to_thread(test.server.create_from_competing_dispatch),
        )
        recovered = await test.lookup(historical=True)

        assert lost == ChangeRequestResult("unresolved")
        assert recovered == ChangeRequestResult(
            "observed", (test.case.expected.identity,), test.case.expected
        )
        assert test.case.counts() == ChangeRequestCounts(1, 1)
        assert test.server.posts == test.server.competing_posts == 1
