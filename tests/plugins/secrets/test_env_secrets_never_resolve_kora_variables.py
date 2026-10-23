import asyncio

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import NAMESPACE_ID, kora_capture
from kora.plugins.secrets.env import EnvSecretsConfiguration, EnvSecretsProvider
from tests.plugins.secrets.env_support import resolve_code

__all__ = ["kora_capture"]

CONTROL: dict[str, str] = {
    "KORA_TOKEN": "control-plane-token-0042",
    "KORA_MCP_TOKEN": "control-plane-mcp-0043",
}


def permissive_provider(prefix: str) -> EnvSecretsProvider:
    configuration = EnvSecretsConfiguration.model_construct(
        namespace_prefixes={NAMESPACE_ID: (prefix,)}, excluded_names=()
    )
    return EnvSecretsProvider(configuration, CONTROL)


def test_an_unvalidated_family_prefix_still_reads_nothing(
    kora_capture: Capture,
) -> None:
    configuration = EnvSecretsConfiguration.model_construct(
        namespace_prefixes={NAMESPACE_ID: ("A",)}, excluded_names=()
    )
    subject = EnvSecretsProvider(configuration, {"AWS_SECRET_ACCESS_KEY": "cloud-0001"})

    code = resolve_code(subject, kora_capture, "secret://env/AWS_SECRET_ACCESS_KEY")
    listed = asyncio.run(subject.list_names("A"))

    assert code == "secret_reference_not_allowed"
    assert listed == []


@pytest.mark.parametrize("prefix", ["K", "KORA_", ""])
@pytest.mark.parametrize("name", ["KORA_TOKEN", "KORA_MCP_TOKEN", "KORA_ABSENT"])
def test_env_secrets_never_resolve_kora_variables(
    kora_capture: Capture, prefix: str, name: str
) -> None:
    subject = permissive_provider(prefix)

    code = resolve_code(subject, kora_capture, f"secret://env/{name}")
    listed = asyncio.run(subject.list_names(prefix))

    assert code == "secret_reference_not_allowed"
    assert [item for item in listed if item.startswith("KORA_")] == []
