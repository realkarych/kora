from typing import Annotated, Final

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
)

from kora.ir.config import environment_schema, require_integer
from kora.ir.json import JsonObject
from kora.ir.scalars import SecretRef
from kora.plugins.harness.quota import MAX_RELEASE_BATCH as MAX_RELEASE_BATCH
from kora.plugins.harness.quota import MAX_RELEASE_INTERVAL as MAX_RELEASE_INTERVAL
from kora.plugins.harness.quota import HarnessQuota, ModelPatterns, QuotaIdentifier
from kora.protocols.boundary import BoundaryModel
from kora.protocols.metadata import parse_config_schema

MAX_RETRIES: Final[int] = 10


def default_groups() -> dict[str, tuple[str, ...]]:
    return {
        "account": ("*",),
        "opus": ("claude-opus-*",),
        "sonnet": ("claude-sonnet-*",),
    }


def default_vendor_groups() -> dict[str, str]:
    return {
        "five_hour": "account",
        "seven_day": "account",
        "seven_day_opus": "opus",
        "seven_day_sonnet": "sonnet",
    }


class ClaudeCredentials(BoundaryModel):
    ANTHROPIC_API_KEY: SecretRef


class ClaudeQuota(HarnessQuota):
    groups: Annotated[
        dict[QuotaIdentifier, ModelPatterns],
        Field(json_schema_extra=environment_schema),
    ] = Field(default_factory=default_groups)
    vendor_groups: Annotated[
        dict[QuotaIdentifier, QuotaIdentifier],
        Field(json_schema_extra=environment_schema),
    ] = Field(default_factory=default_vendor_groups)


class ClaudeDeployment(BoundaryModel):
    credentials: ClaudeCredentials
    quota: ClaudeQuota = Field(default_factory=ClaudeQuota)
    cli_telemetry: StrictBool = False
    max_retries: Annotated[
        StrictInt, Field(ge=0, le=MAX_RETRIES), BeforeValidator(require_integer)
    ] = 2


CONFIG_SCHEMA: Final[JsonObject] = parse_config_schema(
    ClaudeDeployment.model_json_schema()
)
