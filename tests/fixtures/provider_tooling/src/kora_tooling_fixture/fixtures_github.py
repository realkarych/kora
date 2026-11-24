from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from pydantic import StrictBool, StrictInt, StrictStr

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.github import (
    ChangeRequestCase,
    ChangeRequestConformanceInputs,
    ChangeRequestCounts,
    ChangeRequestExercise,
)
from kora.ir.json import JsonObject, parse_json_object
from kora.observability import current_diagnostics
from kora.plugins.vcs.git.github_model import GitHubConfiguration
from kora.protocols.boundary import BoundaryModel
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.secret_value import SecretScope
from kora_tooling_fixture.configured_vcs import Adapter, configured_paths
from kora_tooling_fixture.github_exercises import exercise_for


class FixtureInputs(BoundaryModel):
    head: StrictStr
    base: StrictStr
    evidence: StrictStr
    omit_exercises: StrictBool = False


class RecordedCounts(BoundaryModel):
    posts: StrictInt
    pull_requests: StrictInt


def recorded_counts(path: Path) -> ChangeRequestCounts:
    recorded = RecordedCounts.model_validate_json(path.read_bytes())
    return ChangeRequestCounts(recorded.posts, recorded.pull_requests)


class GitHubAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__("normal")
        self.code_host: GitHubConfiguration
        self.inputs: FixtureInputs
        self.scopes: list[SecretScope] = []

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        self.inputs = FixtureInputs.model_validate(fixture_data)
        code_host = configured_paths(config).change_requests
        assert code_host is not None
        self.code_host = code_host
        await super().prepare(config, fixture_data, lifetime)
        if not self.inputs.omit_exercises:
            self.exercises = replace(
                self.exercises,
                change_requests=ChangeRequestConformanceInputs(
                    prepare=self.prepare_case
                ),
            )
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        parsed = parse_json_object(configured_paths(config).model_dump(mode="json"))
        remapped = {**config, "remotes": parsed["remotes"]}
        return ConstructorArguments((remapped,))

    def prepare_case(
        self, case: ChangeRequestCase, context: ProtocolContext
    ) -> ChangeRequestExercise:
        receipt = Path(self.inputs.evidence) / f"{case.value}.json"
        with ExitStack() as partial_preparation:

            def new_scope() -> SecretScope:
                scope = SecretScope(current_diagnostics())
                self.scopes.append(scope)
                partial_preparation.callback(scope.release)
                return scope

            exercise = exercise_for(
                context,
                self.code_host,
                self.inputs.head,
                self.inputs.base,
                case.value,
                partial(recorded_counts, receipt),
                new_scope,
            )
            partial_preparation.pop_all()
        return exercise

    async def cleanup(self, instance: object | None) -> None:
        released = all(
            scope.released and scope.registered_count() == 0 for scope in self.scopes
        )
        try:
            for scope in self.scopes:
                scope.release()
        finally:
            await super().cleanup(instance)
        assert released


@pytest.fixture
def kora_conformance_adapter() -> GitHubAdapter:
    return GitHubAdapter()
