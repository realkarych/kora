from operator import itemgetter
from typing import Never

from kora.api.cel_pattern_validation import CheckedPatternSite
from kora.cel import CheckedExpression, ResultKind
from kora.db.graph_value_json import TextBudget, quoted, render_object
from kora.engine.graph_type_references import array_text
from kora.ir.json import JsonObject, JsonValue

FACTS_PREFIX = '{"interpretation":"kora.cel-expression-facts.v1","fields":['
FACTS_SUFFIX = "]}"


class GraphCelFacts:
    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text: str = text

    @property
    def text(self) -> str:
        return self._text

    def __getstate__(self) -> Never:
        raise TypeError("graph CEL facts require explicit access")


def local_reads(checked: CheckedExpression, budget: TextBudget) -> JsonObject:
    reads: JsonObject = {}
    names = sorted(checked.projections, key=lambda name: quoted(name, budget))
    for name in names:
        paths = checked.projections[name]
        budget.reserve(len(paths.whole) + len(paths.presence))
        reads[name] = {"whole": list(paths.whole), "presence": list(paths.presence)}
    return reads


def fact_entry(site: CheckedPatternSite, budget: TextBudget) -> tuple[str, str]:
    budget.reserve(len(site.path))
    path: list[JsonValue] = list(site.path)
    key = array_text(path, budget)
    estimate = site.checked.estimate
    result = (
        ResultKind.STRING
        if site.path[-1] in ("reason", "else_reason")
        else ResultKind.BOOL
    )
    entry: JsonObject = {
        "path": path,
        "origin": "authored" if site.authored else "approval_default",
        "result": result.value,
        "rendering": site.checked.rendering,
        "cost": {
            "steps": estimate.steps,
            "memory": estimate.memory,
            "compile_steps": estimate.compile_steps,
        },
        "reads": local_reads(site.checked, budget),
    }
    return key, render_object(entry, budget, reject_aliases=True)


def stage_graph_cel_facts(
    sites: tuple[CheckedPatternSite, ...], max_bytes: int
) -> GraphCelFacts:
    budget = TextBudget(max_bytes)
    budget.reserve(len(FACTS_PREFIX) + len(FACTS_SUFFIX))
    entries: list[tuple[str, str]] = []
    for site in sites:
        budget.reserve(int(bool(entries)))
        entries.append(fact_entry(site, budget))
    entries.sort(key=itemgetter(0))
    return GraphCelFacts(
        FACTS_PREFIX + ",".join(entry for _, entry in entries) + FACTS_SUFFIX
    )
