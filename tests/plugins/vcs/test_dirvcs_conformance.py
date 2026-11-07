from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import (
    VcsBench,
    VcsBinding,
    VcsRemote,
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
    test_vcs_api_credential_is_refused,
    test_vcs_checkout_captures_requested_base,
    test_vcs_credential_binding_mismatch_is_refused,
    test_vcs_head_sha,
    test_vcs_local_credential_carries_no_secret,
    test_vcs_private_files_stay_outside_candidate,
    test_vcs_repeated_finalize_returns_one_ref,
    test_vcs_sealed_transfer_fixes_inspected_revision,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import Vcs
from tests.plugins.vcs.dirvcs_exercise import dirvcs_exercise
from tests.plugins.vcs.dirvcs_support import DirWorld, make_dir_world

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_vcs_api_credential_is_refused",
    "test_vcs_checkout_captures_requested_base",
    "test_vcs_credential_binding_mismatch_is_refused",
    "test_vcs_head_sha",
    "test_vcs_local_credential_carries_no_secret",
    "test_vcs_private_files_stay_outside_candidate",
    "test_vcs_repeated_finalize_returns_one_ref",
    "test_vcs_sealed_transfer_fixes_inspected_revision",
]


@pytest.fixture
def dir_world(tmp_path: Path, kora_capture: Capture) -> DirWorld:
    return make_dir_world(tmp_path, kora_capture)


@pytest.fixture
def kora_vcs_provider(dir_world: DirWorld) -> Vcs:
    provider: Vcs = dir_world.provider
    return provider


@pytest.fixture
def kora_vcs_exercise() -> object:
    return dirvcs_exercise


@pytest.fixture
def kora_vcs_remote(dir_world: DirWorld) -> VcsRemote:
    return dir_world.remote_fixture()


@pytest.fixture
def kora_vcs_executor(dir_world: DirWorld) -> VcsBench:
    return dir_world.bench()


@pytest.fixture
def kora_vcs_context(dir_world: DirWorld) -> ProtocolContext:
    return dir_world.context()


@pytest.fixture
def kora_vcs_binding(dir_world: DirWorld) -> VcsBinding:
    return dir_world.binding()


def test_conformance_modules_run_every_case() -> None:
    from kora.conformance import vcs

    required = {
        name: value for name, value in vars(vcs).items() if name.startswith("test_")
    }

    present = {name: globals().get(name) for name in required}

    assert required
    assert present == required
