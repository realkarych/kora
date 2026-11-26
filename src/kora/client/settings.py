from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kora.client.documents import NAMESPACE
from kora.client.errors import ClientError, Failure
from kora.client.transport import TOKEN, KoraClient, direct_origin
from kora.observability import ProcessDiagnostics

URL_VARIABLE = "KORA_URL"
URL_OPTION = "--url"
CA_BUNDLE_VARIABLE = "KORA_CA_BUNDLE"
CLI_CREDENTIAL_VARIABLE = "KORA_TOKEN"
MCP_CREDENTIAL_VARIABLE = "KORA_MCP_TOKEN"
NAMESPACE_VARIABLE = "KORA_NAMESPACE"
NAMESPACE_OPTION = "--namespace"


class ConfigurationProblem(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"


class ClientConfigurationError(ValueError):
    def __init__(self, variable: str, problem: ConfigurationProblem) -> None:
        super().__init__(f"{variable}: {problem.value}")
        self.variable = variable
        self.problem = problem


@dataclass(frozen=True, slots=True)
class ClientSettings:
    url: str
    token: str
    ca_bundle: Path | None = None

    def __repr__(self) -> str:
        return f"ClientSettings(url={self.url!r})"

    def client(self, diagnostics: ProcessDiagnostics) -> KoraClient:
        try:
            return KoraClient(
                self.url, self.token, ca_bundle=self.ca_bundle, diagnostics=diagnostics
            )
        except ClientError as error:
            if self.ca_bundle is None or error.reason is not Failure.TLS:
                raise
            raise ClientConfigurationError(
                CA_BUNDLE_VARIABLE, ConfigurationProblem.INVALID
            ) from None


def read_client_settings(
    environment: Mapping[str, str], credential_variable: str, url: str | None = None
) -> ClientSettings:
    source = URL_VARIABLE if url is None else URL_OPTION
    origin = environment.get(URL_VARIABLE, "") if url is None else url
    if not origin:
        raise ClientConfigurationError(source, ConfigurationProblem.MISSING)
    try:
        direct_origin(origin)
    except ClientError:
        raise ClientConfigurationError(source, ConfigurationProblem.INVALID) from None
    token = environment.get(credential_variable, "")
    if not token:
        raise ClientConfigurationError(
            credential_variable, ConfigurationProblem.MISSING
        )
    if not TOKEN.fullmatch(token):
        raise ClientConfigurationError(
            credential_variable, ConfigurationProblem.INVALID
        )
    bundle = environment.get(CA_BUNDLE_VARIABLE)
    if bundle == "":
        raise ClientConfigurationError(CA_BUNDLE_VARIABLE, ConfigurationProblem.INVALID)
    return ClientSettings(origin, token, None if bundle is None else Path(bundle))


def read_namespace(environment: Mapping[str, str], option: str | None) -> str:
    source = NAMESPACE_VARIABLE if option is None else NAMESPACE_OPTION
    value = environment.get(NAMESPACE_VARIABLE, "") if option is None else option
    if not value:
        raise ClientConfigurationError(source, ConfigurationProblem.MISSING)
    if NAMESPACE.fullmatch(value) is None:
        raise ClientConfigurationError(source, ConfigurationProblem.INVALID)
    return value
