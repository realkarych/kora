import gc
import tracemalloc

OWN_LINES: tuple[tracemalloc.Filter, ...] = (
    tracemalloc.Filter(False, tracemalloc.__file__),
)


def snapshot() -> tracemalloc.Snapshot:
    gc.collect()
    return tracemalloc.take_snapshot().filter_traces(OWN_LINES)


def grown_lines(
    before: tracemalloc.Snapshot, after: tracemalloc.Snapshot
) -> dict[str, tracemalloc.StatisticDiff]:
    return {
        str(stat.traceback): stat
        for stat in after.compare_to(before, "lineno")
        if stat.size_diff > 0
    }


def recurring_lines(
    before: tracemalloc.Snapshot,
    middle: tracemalloc.Snapshot,
    after: tracemalloc.Snapshot,
    cycles: int,
) -> list[tracemalloc.StatisticDiff]:
    first = grown_lines(before, middle)
    second = grown_lines(middle, after)
    return [
        stat
        for line, stat in grown_lines(before, after).items()
        if stat.count_diff >= cycles // 2 or (line in first and line in second)
    ]


def recurring_growth(
    before: tracemalloc.Snapshot,
    middle: tracemalloc.Snapshot,
    after: tracemalloc.Snapshot,
    cycles: int,
) -> int:
    return sum(
        stat.size_diff for stat in recurring_lines(before, middle, after, cycles)
    )


def top_growth(lines: list[tracemalloc.StatisticDiff], count: int) -> list[str]:
    ranked = sorted(lines, key=lambda stat: -stat.size_diff)
    return [
        f"{stat.traceback} size {stat.size_diff} count {stat.count_diff}"
        for stat in ranked[:count]
    ]
