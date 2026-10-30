import pytest


class ConformanceFixtureMissing(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"override the {name} fixture with the provider under test")


@pytest.fixture
def kora_authentication_provider() -> object:
    raise ConformanceFixtureMissing("kora_authentication_provider")


@pytest.fixture
def kora_authentication_inputs() -> object:
    raise ConformanceFixtureMissing("kora_authentication_inputs")


@pytest.fixture
def kora_runtime_provider() -> object:
    raise ConformanceFixtureMissing("kora_runtime_provider")


@pytest.fixture
def kora_runtime_exercise() -> object:
    raise ConformanceFixtureMissing("kora_runtime_exercise")


@pytest.fixture
def kora_harness_provider() -> object:
    raise ConformanceFixtureMissing("kora_harness_provider")


@pytest.fixture
def kora_harness_exercise() -> object:
    raise ConformanceFixtureMissing("kora_harness_exercise")


@pytest.fixture
def kora_vcs_provider() -> object:
    raise ConformanceFixtureMissing("kora_vcs_provider")


@pytest.fixture
def kora_vcs_exercise() -> object:
    raise ConformanceFixtureMissing("kora_vcs_exercise")


@pytest.fixture
def kora_vcs_remote() -> object:
    raise ConformanceFixtureMissing("kora_vcs_remote")


@pytest.fixture
def kora_vcs_executor() -> object:
    raise ConformanceFixtureMissing("kora_vcs_executor")


@pytest.fixture
def kora_vcs_context() -> object:
    raise ConformanceFixtureMissing("kora_vcs_context")


@pytest.fixture
def kora_vcs_binding() -> object:
    raise ConformanceFixtureMissing("kora_vcs_binding")


@pytest.fixture
def kora_policy_provider() -> object:
    raise ConformanceFixtureMissing("kora_policy_provider")


@pytest.fixture
def kora_policy_exercise() -> object:
    raise ConformanceFixtureMissing("kora_policy_exercise")


@pytest.fixture
def kora_secrets_provider() -> object:
    raise ConformanceFixtureMissing("kora_secrets_provider")


@pytest.fixture
def kora_secrets_exercise() -> object:
    raise ConformanceFixtureMissing("kora_secrets_exercise")


@pytest.fixture
def kora_secrets_resolvable() -> object:
    raise ConformanceFixtureMissing("kora_secrets_resolvable")


@pytest.fixture
def kora_secrets_missing_reference() -> object:
    raise ConformanceFixtureMissing("kora_secrets_missing_reference")


@pytest.fixture
def kora_secrets_forbidden_reference() -> object:
    raise ConformanceFixtureMissing("kora_secrets_forbidden_reference")


@pytest.fixture
def kora_secrets_listed_prefix() -> object:
    raise ConformanceFixtureMissing("kora_secrets_listed_prefix")


@pytest.fixture
def kora_artifact_storage_factory() -> object:
    raise ConformanceFixtureMissing("kora_artifact_storage_factory")


@pytest.fixture
def kora_artifact_storage_exercise() -> object:
    raise ConformanceFixtureMissing("kora_artifact_storage_exercise")


@pytest.fixture
def kora_policy_calls() -> object:
    raise ConformanceFixtureMissing("kora_policy_calls")


@pytest.fixture
def kora_artifact_type_provider() -> object:
    raise ConformanceFixtureMissing("kora_artifact_type_provider")


@pytest.fixture
def kora_artifact_type_inputs() -> object:
    raise ConformanceFixtureMissing("kora_artifact_type_inputs")


@pytest.fixture
def kora_artifact_type_configuration() -> object:
    raise ConformanceFixtureMissing("kora_artifact_type_configuration")
