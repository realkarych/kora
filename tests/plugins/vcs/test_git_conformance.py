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
from tests.plugins.vcs.git_exercise import git_exercise
from tests.plugins.vcs.git_support import World, make_world

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
def git_world(tmp_path: Path, kora_capture: Capture) -> World:
    return make_world(tmp_path, kora_capture)


@pytest.fixture
def kora_vcs_provider(git_world: World) -> Vcs:
    provider: Vcs = git_world.provider
    return provider


@pytest.fixture
def kora_vcs_exercise() -> object:
    return git_exercise


@pytest.fixture
def kora_vcs_remote(git_world: World) -> VcsRemote:
    return git_world.remote_fixture()


@pytest.fixture
def kora_vcs_executor(git_world: World) -> VcsBench:
    return git_world.bench()


@pytest.fixture
def kora_vcs_context(git_world: World) -> ProtocolContext:
    return git_world.context()


@pytest.fixture
def kora_vcs_binding(git_world: World) -> VcsBinding:
    return git_world.binding()
