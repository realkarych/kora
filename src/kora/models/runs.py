from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from kora.models.errors import WireRecord
from kora.models.graphs import OpaqueObject

RUN_PREFIX = "run_"
GraphVersionReference = Annotated[
    StrictStr, Field(pattern=r"^gv_[0-9A-HJKMNP-TV-Z]{26}$")
]
RunReference = Annotated[StrictStr, Field(pattern=r"^run_[0-9A-HJKMNP-TV-Z]{26}$")]


class TriggerRequest(WireRecord):
    params: OpaqueObject
    graph_version: GraphVersionReference | None = None
    allow_unenforced_secrets: StrictBool = False


class StartOutcomeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)


class AcceptedRun(StartOutcomeRecord):
    outcome: Literal["accepted"]
    run_id: RunReference
    graph_version_id: GraphVersionReference
    status: Literal["pending"]


class SkippedRun(StartOutcomeRecord):
    outcome: Literal["skipped"]
    reason: Literal["overlap"]
    run_id: None


StartOutcome = Annotated[AcceptedRun | SkippedRun, Field(discriminator="outcome")]
