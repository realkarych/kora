import json
from dataclasses import replace
from pathlib import Path

import pytest

from kora.cli.conformance import CommandResult
from kora.conformance.bridge import ConformanceSelection
from kora.plugins.vcs.git.provider import provider_metadata
from kora.protocols.lifetime import OriginalOutcome
from tests.cli.test_conformance import (
    assert_closed,
    assert_undisclosed,
    conformance_arguments,
    private_home,
)
from tests.support.configured_conformance import (
    installed_conformance as installed_conformance,
)
from tests.support.configured_conformance import vcs_configuration
from tests.support.github_host import (
    ChangeRequestCase,
    GitHubState,
    github_host,
    host_configuration,
    published_git,
)
from tests.support.https_fixture import create_authority
from tests.support.provider_tooling import InstalledTooling


def test_change_request_conformance_is_tied_to_the_selected_git_profile() -> None:
    configuration = vcs_configuration(Path("vcs"))
    configuration["change_requests"] = host_configuration(
        "https://127.0.0.1:4443", Path("/fixture/ca.pem")
    )
    wire = json.dumps(configuration).encode()
    metadata = provider_metadata()

    git = ConformanceSelection(metadata, wire)
    other = ConformanceSelection(replace(metadata, name="configured"), wire)

    assert git.change_requests
    assert not other.change_requests


@pytest.mark.parametrize("omit_exercises", [False, True])
def test_installed_git_provider_runs_required_change_request_conformance(
    installed_conformance: InstalledTooling, tmp_path: Path, omit_exercises: bool
) -> None:
    home = private_home(tmp_path / "home")
    tls = private_home(tmp_path / "tls")
    authority = create_authority(tls)
    published = published_git(private_home(tmp_path / "published"))
    evidence = private_home(tmp_path / "evidence")
    state = GitHubState(published, evidence)
    checkout = Path("git-change-requests")
    configuration = vcs_configuration(checkout)
    config = tmp_path / "config.json"
    data = tmp_path / "fixture.json"
    data.write_text(
        json.dumps(
            {
                "head": published.head,
                "base": published.base,
                "evidence": str(evidence),
                "omit_exercises": omit_exercises,
            }
        )
    )
    with github_host(state, tls / "server.pem") as host:
        configuration["change_requests"] = host_configuration(
            host.origin, authority.bundle
        )
        config.write_text(json.dumps(configuration))

        process = installed_conformance.run(
            *conformance_arguments("vcs", "git", config, "fixtures_github", data),
            environment={
                "HOME": str(home),
                "KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG",
            },
        )

    report = CommandResult.model_validate_json(process.stdout)
    assert process.code == int(omit_exercises), (process.stdout, process.stderr)
    assert report.outcome is (
        OriginalOutcome.FAILED if omit_exercises else OriginalOutcome.COMPLETE
    )
    assert report.expected == 14
    assert report.completed == (0 if omit_exercises else report.expected)
    assert_closed(report)
    assert state.posts == (0 if omit_exercises else 2)
    expected_counts = {
        ChangeRequestCase.CREATE: (1, 1),
        ChangeRequestCase.PREEXISTING: (0, 1),
        ChangeRequestCase.RESPONSE_LOSS: (1, 1),
    }
    assert {case: exercise.counts() for case, exercise in state.cases.items()} == (
        {} if omit_exercises else expected_counts
    )
    assert [
        (record["protocol"], record["provider"])
        for record in process.records()
        if record["event"] == "protocols.loading.provider_loaded"
    ] == [("vcs", "git")]
    assert not (installed_conformance.outside / checkout).exists()
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        process,
        b"private-github-installation-token",
        b"private-github-response-content",
        b"private-github-presentation-title",
        b"private-github-presentation-body",
        str(tls).encode(),
    )
