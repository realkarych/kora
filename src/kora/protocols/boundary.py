from pydantic import BaseModel, ConfigDict


class BoundaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
