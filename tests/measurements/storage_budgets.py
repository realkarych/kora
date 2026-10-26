from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import Field, StrictFloat, StrictInt, model_validator

from kora.ir.json import JsonObject
from kora.protocols.common import BoundaryModel
from tests.measurements.storage_evidence_common import EvidenceFailure, read_json
from tests.measurements.storage_lifetime_manifest import WORKLOADS, Workload

Nonnegative = Annotated[StrictInt, Field(ge=0)]
Positive = Annotated[StrictInt, Field(gt=0)]
Seconds = Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
BUDGET_PATH: Final[Path] = Path(__file__).with_name("storage_budgets.json")
DIAGNOSTIC_GROWTH_FIELDS: Final[tuple[str, ...]] = (
    "python_post_gc_bytes",
    "tracer_bytes",
    "rss_bytes",
    "cumulative_maxrss_bytes",
    "phase_peak_rss_bytes",
)
BLOCKING_GROWTH_FIELDS: Final[tuple[str, ...]] = (
    "database_bytes",
    "journal_bytes",
    "unique_bytes",
    "allocated_bytes",
)


class GrowthBudget(BoundaryModel):
    absolute: Nonnegative
    warmed_growth: Nonnegative
    late_endpoint_growth: Nonnegative
    late_excursion_growth: Nonnegative


class TimingBudget(BoundaryModel):
    legacy_p95_seconds: Seconds
    maximum_cycle_seconds: Seconds | None


class PlatformBudget(BoundaryModel):
    python_peak_bytes: Positive
    python_post_gc_bytes: GrowthBudget
    tracer_bytes: GrowthBudget
    rss_bytes: GrowthBudget
    cumulative_maxrss_bytes: GrowthBudget
    phase_peak_rss_bytes: GrowthBudget
    database_bytes: GrowthBudget
    journal_bytes: GrowthBudget
    unique_bytes: GrowthBudget
    allocated_bytes: GrowthBudget
    phase_unique_overhead_bytes: Nonnegative
    phase_allocated_overhead_bytes: Nonnegative
    inventory_query_steps: Positive
    inventory_page_seconds: Seconds
    workloads: dict[str, TimingBudget]

    @model_validator(mode="after")
    def exact_workloads(self) -> Self:
        if set(self.workloads) != {workload.name for workload in WORKLOADS}:
            raise ValueError("budget workload coverage is not exact")
        for workload in WORKLOADS:
            maximum = self.workloads[workload.name].maximum_cycle_seconds
            if (maximum is not None) != (workload.cycles == 3):
                raise ValueError("maximum-cycle budget applicability differs")
        return self


class StorageBudgets(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    platforms: dict[str, PlatformBudget]

    @model_validator(mode="after")
    def exact_platforms(self) -> Self:
        if set(self.platforms) != {"linux", "darwin"}:
            raise ValueError("budget platform coverage is not exact")
        return self


def read_budgets(path: Path = BUDGET_PATH) -> StorageBudgets:
    return StorageBudgets.model_validate(read_json(path, 32768))


def require_budget(value: int | float, maximum: int | float, metric: str) -> None:
    if value > maximum:
        raise EvidenceFailure(f"storage budget exceeded: {metric}: {value} > {maximum}")


def validate_growth(
    values: tuple[tuple[int, int], ...],
    baseline: int,
    budget: GrowthBudget,
    metric: str,
) -> None:
    for name, observed, maximum in growth_comparisons(values, baseline, budget, metric):
        require_budget(observed, maximum, name)


def growth_comparisons(
    values: tuple[tuple[int, int], ...],
    baseline: int,
    budget: GrowthBudget,
    metric: str,
) -> tuple[tuple[str, int, int], ...]:
    comparisons = (
        (metric, max(value for _, value in values), budget.absolute),
        (
            metric + ".warmed_growth",
            max(value - baseline for _, value in values),
            budget.warmed_growth,
        ),
    )
    if values[-1][0] != 100:
        return comparisons
    middle = next(value for cycle, value in values if cycle == 50)
    return (
        *comparisons,
        (
            metric + ".late_endpoint_growth",
            values[-1][1] - middle,
            budget.late_endpoint_growth,
        ),
        (
            metric + ".late_excursion_growth",
            max(value - middle for cycle, value in values if cycle > 50),
            budget.late_excursion_growth,
        ),
    )


def diagnostic_observation(
    metric: str, observed: int | float, reference: int | float
) -> JsonObject:
    return {
        "metric": metric,
        "observed": observed,
        "reference": reference,
        "exceeds_reference": observed > reference,
    }


def sampled_payload_bytes(workload: Workload) -> int:
    size = workload.size
    if workload.ending in {"source_error", "cancelled"}:
        return size
    if workload.ending == "raw" and size == 65536:
        return 3 * size
    if workload.ending == "prepared":
        return size + (size + 1 if size < 268435456 else size - 1)
    return 2 * size
