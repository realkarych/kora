from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from kora.models.errors import WireRecord
from kora.protocols.history import GrantHistoryPosition
from kora.protocols.identity import Count, Identifier, Sha256


class WorkerHistoryQuery(WireRecord):
    lease_epoch: Count
    dispatcher_id: Identifier
    snapshot_version: Annotated[StrictInt, Field(ge=1, le=1)]
    snapshot_upper_epoch: Count | None = None
    snapshot_count: Count
    snapshot_digest: Sha256
    after_epoch: Count | None = None
    received_count: Count
    prefix_digest: Sha256

    @model_validator(mode="after")
    def positions_have_epochs(self) -> Self:
        if (self.snapshot_count == 0) != (self.snapshot_upper_epoch is None) or (
            (self.received_count == 0) != (self.after_epoch is None)
        ):
            raise PydanticCustomError("history_invalid", "the history query is invalid")
        return self

    def tip(self) -> GrantHistoryPosition:
        return GrantHistoryPosition(
            after_epoch=self.snapshot_upper_epoch,
            received_count=self.snapshot_count,
            prefix_digest=self.snapshot_digest,
        )

    def start(self) -> GrantHistoryPosition:
        return GrantHistoryPosition(
            after_epoch=self.after_epoch,
            received_count=self.received_count,
            prefix_digest=self.prefix_digest,
        )
