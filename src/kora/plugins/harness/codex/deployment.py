from typing import Final

from pydantic import Field

from kora.ir.json import JsonObject
from kora.ir.scalars import SecretRef
from kora.plugins.harness.quota import HarnessQuota
from kora.protocols.boundary import BoundaryModel
from kora.protocols.metadata import parse_config_schema


class CodexCredentials(BoundaryModel):
    CODEX_API_KEY: SecretRef


class CodexDeployment(BoundaryModel):
    credentials: CodexCredentials
    quota: HarnessQuota = Field(default_factory=HarnessQuota)


CONFIG_SCHEMA: Final[JsonObject] = parse_config_schema(
    CodexDeployment.model_json_schema()
)
