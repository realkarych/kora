from collections.abc import Awaitable, Mapping

from kora.conformance.capture import Capture
from kora.conformance.common import NAMESPACE_ID, context_for, raised_protocol_error
from kora.plugins.secrets.env import EnvSecretsConfiguration, EnvSecretsProvider

VALUE: str = 'env-secret-"quoted"-é-7c21'
ENVIRONMENT: dict[str, str] = {
    "CONF_TOKEN": VALUE,
    "CONF_SHORT": "short",
    "CONF_WIDE": "é" * 1500,
    "OTHER_TOKEN": "other-namespace-value-91",
    "KORA_TOKEN": "control-plane-token-0042",
    "WORKER_KEY": "worker-credential-5531",
}
CONFIGURATION: dict[str, object] = {
    "namespace_prefixes": {NAMESPACE_ID: ["CONF_"], "other": ["OTHER_"]},
    "excluded_names": ["WORKER_KEY"],
}


def provider(environment: Mapping[str, str] = ENVIRONMENT) -> EnvSecretsProvider:
    configuration = EnvSecretsConfiguration.model_validate(CONFIGURATION)
    return EnvSecretsProvider(configuration, environment)


def refusal_code(outcome: Awaitable[object]) -> str | None:
    error = raised_protocol_error(outcome)
    return None if error is None else error.code


def resolve_code(
    subject: EnvSecretsProvider, capture: Capture, reference: str
) -> str | None:
    return refusal_code(subject.resolve(reference, context_for(capture)))
