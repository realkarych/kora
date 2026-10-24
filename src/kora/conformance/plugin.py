import pytest


class ConformanceFixtureMissing(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"override the {name} fixture with the provider under test")


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
def kora_policy_calls() -> object:
    raise ConformanceFixtureMissing("kora_policy_calls")
