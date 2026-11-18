from collections.abc import Mapping
from typing import cast

from kora.ir.schema_keywords import NUMBER_KEYWORDS
from kora.ir.standard_schema_policy import SchemaAccounting, SchemaDomain, require
from kora.ir.standard_schema_scan import SourceScan, preparation_score
from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_values import Bounds, Statistics, numeric_score


def equality_score(left: Bounds, right: Bounds, cap: int) -> int:
    arithmetic = (
        numeric_score(max(left.bits, right.bits))
        if left.numeric or right.numeric
        else 0
    )
    return min(
        cap,
        (left.nodes + right.nodes + 1) ** 2 * (left.key_bytes + right.key_bytes + 1)
        + left.encoded
        + right.encoded
        + arithmetic,
    )


def local_score(
    sources: Sources,
    index: int,
    value: Statistics,
    source: Statistics,
    domain: SchemaDomain,
) -> int:
    site = sources.sites[index]
    schema = site.schema
    if type(schema) is bool:
        return 1
    cap = sources.limits["max_validation_score"] + 1
    work = 1 + len(schema)
    for keyword, argument in schema.items():
        if keyword in NUMBER_KEYWORDS:
            operand = (
                SourceScan(sources.limits, domain)
                .measure(argument, complete_source=False)
                .tree.bits
            )
            bits = (
                value.tree.bits
                if keyword
                in {
                    "multipleOf",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                }
                else max(value.tree.encoded, value.tree.nodes).bit_length()
            )
            work += numeric_score(max(bits, operand))
        elif keyword in {
            "properties",
            "required",
            "dependentRequired",
            "dependentSchemas",
            "additionalProperties",
            "propertyNames",
        }:
            work += (value.members + source.members + 1) ** 2 * (
                max(value.tree.key_bytes, source.tree.key_bytes) + 1
            )
        elif keyword in {"items", "contains", "prefixItems"}:
            work += value.array + 1
        elif keyword in {"allOf", "anyOf", "oneOf"}:
            work += len(cast("list[object]", argument)) + 1
        elif keyword in {"const", "enum"}:
            literals = (
                [argument] if keyword == "const" else cast("list[object]", argument)
            )
            maximum = Bounds()
            for literal in literals:
                maximum.maximum(
                    SourceScan(sources.limits, domain)
                    .measure(literal, complete_source=False)
                    .tree
                )
            work += len(literals) * equality_score(value.tree, maximum, cap)
        elif keyword == "uniqueItems" and argument is True:
            if site.reachable:
                require(
                    value.array <= sources.limits["max_unique_items"], capacity=True
                )
            comparison = (
                equality_score(value.element, value.element, cap) if value.array else 0
            )
            work += (value.array + 1) ** 2 * comparison + value.array + 1
        elif keyword == "type":
            work += (len(argument) if isinstance(argument, list) else 1) + 1
        work = min(cap, work)
    return work


def call_certificate(
    sources: Sources, value: Statistics, source: Statistics, domain: SchemaDomain
) -> tuple[int, int, int]:
    for root in sources.roots():
        pending = [] if sources.sites[root].reachable else [root]
        sources.sites[root].reachable = True
        while pending:
            current = pending.pop()
            for target, _ in sources.sites[current].edges:
                if not sources.sites[target].reachable:
                    sources.sites[target].reachable = True
                    pending.append(target)
    parents: list[list[tuple[int, int]]] = [[] for _ in sources.sites]
    weights = {"once": 1, "array": value.array, "object": value.members}
    for current, site in enumerate(sources.sites):
        site.calls, site.depth = 1, 1
        site.remaining = len(site.edges)
        site.work = local_score(sources, current, value, source, domain)
        for target, kind in site.edges:
            parents[target].append((current, weights[kind]))
    ready = [index for index, site in enumerate(sources.sites) if not site.remaining]
    processed = 0
    while ready:
        current = ready.pop()
        child = sources.sites[current]
        processed += 1
        for parent, weight in parents[current]:
            site = sources.sites[parent]
            site.calls = min(
                sources.limits["max_expanded_calls"] + 1,
                site.calls + weight * child.calls,
            )
            site.work = min(
                sources.limits["max_validation_score"] + 1,
                site.work + weight * child.work,
            )
            site.depth = min(
                sources.limits["max_call_depth"] + 1, max(site.depth, child.depth + 1)
            )
            site.remaining -= 1
            if not site.remaining:
                ready.append(parent)
    require(processed == len(sources.sites))
    calls = work = depth = 0
    for root in sources.roots():
        site = sources.sites[root]
        calls = min(sources.limits["max_expanded_calls"] + 1, calls + site.calls)
        work = min(sources.limits["max_validation_score"] + 1, work + site.work)
        depth = max(depth, site.depth)
    return calls, work, depth


def admit(
    sources: Sources, value: Statistics, source: Statistics, domain: SchemaDomain
) -> SchemaAccounting:
    limits: Mapping[str, int] = sources.limits
    calls, work, depth = call_certificate(sources, value, source, domain)
    require(
        calls <= limits["max_expanded_calls"] and depth <= limits["max_call_depth"],
        capacity=True,
    )
    score = (
        work
        + preparation_score(source, limits["max_validation_score"])
        + value.tree.encoded
        + source.tree.encoded
        + value.tree.nodes
        + source.tree.nodes
        + value.conversions
        + source.conversions
    )
    require(score <= limits["max_validation_score"], capacity=True)
    extra_repr = sum(
        max(0, 20 - len(cast("dict[str, str]", sources.sites[index].schema)[keyword]))
        * 12
        for index, keyword in sources.references
    )
    schema_repr = source.representations + extra_repr
    require(
        max(value.representations, schema_repr) <= limits["max_repr_characters"],
        capacity=True,
    )
    entries = (
        2 * value.tree.nodes
        + 4 * source.tree.nodes
        + 12 * len(sources.sites)
        + 8 * len(sources.references)
        + 4 * len(sources.documents)
        + 256 * (depth + value.depth + source.depth + 1)
        + 4 * depth * depth
        + depth
        * (2 * value.array + 4 * value.members + 2 * source.array + 4 * source.members)
    )
    numeric_bytes = (
        8 * depth * ((2 * max(value.tree.bits, source.tree.bits) + 9) // 8 + 32)
    )
    variable = (
        8 * (value.representations + schema_repr + 256)
        + value.tree.encoded
        + source.tree.encoded
        + value.owned_bytes
        + source.owned_bytes
        + 80 * len(sources.sites)
        + 80 * len(sources.references)
        + numeric_bytes
    )
    require(
        entries <= limits["max_auxiliary_entries"]
        and variable <= limits["max_variable_bytes"],
        capacity=True,
    )
    return SchemaAccounting(
        value.tree.nodes,
        source.tree.nodes,
        len(sources.sites),
        len(sources.references),
        calls,
        score,
        variable,
        entries,
    )
