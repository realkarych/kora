from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from kora.client.errors import ClientError
from kora.client.transport import TOKEN, direct_origin

URL_VARIABLE = "KORA_URL"
URL_OPTION = "--url"
CLI_CREDENTIAL_VARIABLE = "KORA_TOKEN"
MCP_CREDENTIAL_VARIABLE = "KORA_MCP_TOKEN"


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

    def __repr__(self) -> str:
        return f"ClientSettings(url={self.url!r})"


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
    return ClientSettings(origin, token)
