from enum import StrEnum


class DependencyOutcome(StrEnum):
    SATISFIED = "satisfied"
    WAITING = "waiting"
    SKIPPED = "skipped"
    UPSTREAM_FAILED = "upstream_failed"
    REFUSED = "refused"


RULES = (
    "all_success",
    "all_done",
    "one_success",
    "one_failed",
    "none_failed",
    "none_failed_min_one_success",
    "all_skipped",
)
UNFINISHED = ("pending", "ready", "queued", "running", "waiting")
FAILURES = ("failed", "upstream_failed", "cancelled")
FINISHED = ("succeeded", "skipped", *FAILURES)


def decide_dependencies(rule: str, states: tuple[str, ...]) -> DependencyOutcome:
    if rule not in RULES or any(
        state not in (*UNFINISHED, *FINISHED) for state in states
    ):
        return DependencyOutcome.REFUSED
    if any(state in UNFINISHED for state in states):
        return DependencyOutcome.WAITING
    if not states:
        return DependencyOutcome.SATISFIED
    succeeded = states.count("succeeded")
    failed = sum(state in FAILURES for state in states)
    skipped = states.count("skipped")
    satisfied: bool
    match rule:
        case "all_success":
            satisfied = succeeded == len(states)
        case "all_done":
            satisfied = True
        case "one_success":
            satisfied = succeeded > 0
        case "one_failed":
            satisfied = failed > 0
        case "none_failed":
            satisfied = failed == 0
        case "none_failed_min_one_success":
            satisfied = failed == 0 and succeeded > 0
        case "all_skipped":
            satisfied = skipped == len(states)
        case _:
            return DependencyOutcome.REFUSED
    if satisfied:
        return DependencyOutcome.SATISFIED
    if failed and rule not in ("one_failed", "all_skipped"):
        return DependencyOutcome.UPSTREAM_FAILED
    return DependencyOutcome.SKIPPED
