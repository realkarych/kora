from typing import Annotated, Literal

from pydantic import MISSING, Field, StrictInt, StringConstraints, ValidationError

from kora.ir.config import NodeLimits
from kora.ir.json import JsonObject, JsonObjectField
from kora.plugins.harness.codex.launch_configuration import MODEL_NAME
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import KoraProtocolError
from kora.runner.calls import TRACKING_CEILING

CodexModel = Annotated[str, StringConstraints(pattern=rf"\A(?:{MODEL_NAME.pattern})\z")]


class CodexSessionOptions(BoundaryModel):
    model: CodexModel | None = None
    system_prompt_append: str | None = None
    permission_mode: Literal["plan", "accept_edits", "dont_ask", "bypass"] = "dont_ask"
    max_events: Annotated[StrictInt, Field(ge=1)]
    output_schema: JsonObjectField | None = None


def session_options(config: JsonObject, limits: NodeLimits) -> CodexSessionOptions:
    ignored = {
        "harness",
        "prompt",
        "workspace",
        "tool_policy",
        "env",
        "secret_env",
        "runtime",
        "vcs",
    }
    supported = {"model", "system_prompt_append", "permission_mode", "output_schema"}
    if (
        limits.budget_usd is not MISSING
        or config.get("trust_repo_config", False) is not False
        or config.keys() - ignored - supported - {"trust_repo_config"}
    ):
        raise configuration_refused()
    try:
        return CodexSessionOptions.model_validate(
            {key: value for key, value in config.items() if key in supported}
            | {
                "max_events": TRACKING_CEILING
                if limits.max_events is MISSING
                else limits.max_events
            }
        )
    except ValidationError:
        raise configuration_refused() from None


def configuration_refused() -> KoraProtocolError:
    return KoraProtocolError(
        "config_error", "the Codex session configuration is unsupported", False, {}
    )
