import pytest


class ConformanceFixtureMissing(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"override the {name} fixture with the provider under test")


def missing_fixture(name: str) -> object:
    def missing() -> object:
        raise ConformanceFixtureMissing(name)

    return pytest.fixture(missing, name=name)


kora_runtime_provider: object = missing_fixture("kora_runtime_provider")
kora_runtime_exercise: object = missing_fixture("kora_runtime_exercise")
kora_harness_provider: object = missing_fixture("kora_harness_provider")
kora_harness_exercise: object = missing_fixture("kora_harness_exercise")
kora_vcs_provider: object = missing_fixture("kora_vcs_provider")
kora_vcs_exercise: object = missing_fixture("kora_vcs_exercise")
kora_vcs_remote: object = missing_fixture("kora_vcs_remote")
kora_vcs_executor: object = missing_fixture("kora_vcs_executor")
kora_vcs_context: object = missing_fixture("kora_vcs_context")
kora_vcs_binding: object = missing_fixture("kora_vcs_binding")
kora_policy_provider: object = missing_fixture("kora_policy_provider")
kora_policy_exercise: object = missing_fixture("kora_policy_exercise")
kora_secrets_provider: object = missing_fixture("kora_secrets_provider")
kora_secrets_exercise: object = missing_fixture("kora_secrets_exercise")
kora_secrets_resolvable: object = missing_fixture("kora_secrets_resolvable")
kora_secrets_missing_reference: object = missing_fixture(
    "kora_secrets_missing_reference"
)
kora_secrets_forbidden_reference: object = missing_fixture(
    "kora_secrets_forbidden_reference"
)
kora_secrets_listed_prefix: object = missing_fixture("kora_secrets_listed_prefix")
kora_artifact_storage_factory: object = missing_fixture("kora_artifact_storage_factory")
kora_artifact_storage_exercise: object = missing_fixture(
    "kora_artifact_storage_exercise"
)
kora_policy_calls: object = missing_fixture("kora_policy_calls")
