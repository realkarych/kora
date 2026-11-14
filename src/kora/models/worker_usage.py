from typing import Self

from pydantic import Field, field_validator, model_validator

from kora.ir.cost import Money, UsageCoverage, UsageRow, bounded_sequence, refusal
from kora.models.errors import WireRecord
from kora.protocols.identity_records import Count, Identifier


class UsageReport(WireRecord):
    lease_epoch: Count
    dispatcher_id: Identifier
    rows: tuple[UsageRow, ...] = Field(max_length=64)
    coverage: UsageCoverage

    @field_validator("rows", mode="before")
    @classmethod
    def bound_rows(cls, value: object) -> object:
        return bounded_sequence(value, 64)

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        if len({row.model for row in self.rows}) != len(self.rows):
            raise refusal()
        return self


class AcceptedUsageRow(UsageRow):
    usd_estimate: Money | None


class AcceptedUsageReport(WireRecord):
    rows: tuple[AcceptedUsageRow, ...] = Field(max_length=64)
    coverage: UsageCoverage

    @field_validator("rows", mode="before")
    @classmethod
    def bound_rows(cls, value: object) -> object:
        return bounded_sequence(value, 64)
