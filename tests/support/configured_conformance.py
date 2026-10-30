import os
from collections.abc import Generator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.ir.json import JsonObject
from tests.support.provider_tooling import (
    InstalledTooling,
    build_fixture_wheel,
    copy_core_wheel,
    install_tooling,
    prepare_wheelhouse,
)


def vcs_configuration(root: Path) -> JsonObject:
    principal: JsonObject = {
        "provider_instance": "fixture-git",
        "stable_id": "fixture-publisher",
        "display_name": "Fixture publisher",
    }
    return {
        "remotes": {
            "local": {
                "path": str(root / "remote.git"),
                "binding": "fixture-binding",
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
            "gone": {
                "path": str(root / "gone.git"),
                "binding": "gone-binding",
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
        },
        "identity": {"name": "Fixture", "email": "fixture@example.invalid"},
        "tool_path": list(os.environ["PATH"].split(":")),
    }


@pytest.fixture(scope="module")
def installed_conformance(core_wheel: Path) -> Generator[InstalledTooling]:
    with TemporaryDirectory(prefix="kora-installed-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        wheelhouse = root / "dependencies"
        wheelhouse.mkdir()
        prepare_wheelhouse(wheelhouse, conformance=True, postgres=True)
        copy_core_wheel(core_wheel, wheelhouse)
        fixture = build_fixture_wheel(
            root,
            {
                "secrets.configured": (
                    "kora_tooling_fixture.configured_secrets:configured"
                ),
                "secrets.blocked": "kora_tooling_fixture.blocked_metadata:blocked",
                "policy.unrelated": (
                    "kora_tooling_fixture.noisy_import:provider_metadata"
                ),
                "policy.configured": (
                    "kora_tooling_fixture.configured_policy:configured"
                ),
                "runtime.configured": (
                    "kora_tooling_fixture.configured_runtime:configured"
                ),
                "harness.configured": (
                    "kora_tooling_fixture.configured_harness:configured"
                ),
                "vcs.configured": "kora_tooling_fixture.configured_vcs:configured",
                "artifact_storage.configured": (
                    "kora_tooling_fixture.configured_storage:configured"
                ),
                "artifact_type.configured": (
                    "kora_tooling_fixture.configured_artifact_type:configured"
                ),
            },
            {"socket": str(root)},
        )
        yield install_tooling(
            root, wheelhouse, fixture, conformance=True, postgres=True
        )
