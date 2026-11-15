import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

FIXTURE = Path(__file__).parents[1] / "fixtures" / "schema_policy_v1_history.json"
FIXTURE_DIGEST = "a7bc6d198e0e24a2375c1a9f9570e52552e9eba5a1019a1db6c542b5dddd456b"


class HistoricalLeaf(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: str
    ir: str
    type_contracts: list[object]
    path: list[str]
    contract: str
    digest: str


class HistoricalWhole(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    ir: str
    contract: str
    ir_digest: str
    execution_contract_digest: str
    deployment_digest: str


class HistoricalOutputs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source_revision: str
    leaves: list[HistoricalLeaf]
    whole: HistoricalWhole


def historical_outputs() -> HistoricalOutputs:
    encoded = FIXTURE.read_bytes()
    assert hashlib.sha256(encoded).hexdigest() == FIXTURE_DIGEST
    result = HistoricalOutputs.model_validate_json(encoded)
    assert result.source_revision == "1d41a3837c7a9e50c044f81f272b7839336864fe"
    return result
