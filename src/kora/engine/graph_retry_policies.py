from collections.abc import Callable
from typing import TypeVar

from pydantic import MISSING

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    quoted,
    render_object,
)
from kora.engine.graph_types import read_document
from kora.ir.config import Retry
from kora.ir.json import JsonObject
from kora.ir.topology import GraphBody, Node, Subgraph

INTERPRETATION = "kora.disabled-retry-policies.v1"
INTERPRETATION_KEY = '{"interpretation":'
SECTION_PREFIX = INTERPRETATION_KEY + '"' + INTERPRETATION + '","nodes":'
NODE_PREFIX = '{"retry":'
NODE_RUNTIME = ',"provider_retry":{"max_wait":"0s"},"infrastructure_retry":'
T = TypeVar("T")


def ordinary_policy_text(retry: Retry | MISSING, budget: TextBudget) -> str:
    policy: JsonObject = {"max_attempts": 1}
    if retry is not MISSING:
        if retry.max_attempts is not MISSING and retry.max_attempts != 1:
            raise GraphValueRejected(GraphValueFailure.VERSION)
        if retry.initial_interval is not MISSING:
            policy["initial_interval"] = retry.initial_interval
        if retry.backoff_coefficient is not MISSING:
            policy["backoff_coefficient"] = retry.backoff_coefficient
        if retry.max_interval is not MISSING:
            policy["max_interval"] = retry.max_interval
        if retry.retry_on is not MISSING:
            policy["retry_on"] = [error.value for error in retry.retry_on]
        if retry.non_retryable is not MISSING:
            policy["non_retryable"] = [error.value for error in retry.non_retryable]
        if retry.predicate is not MISSING:
            policy["predicate"] = retry.predicate
        if retry.strategy is not MISSING:
            policy["strategy"] = retry.strategy
    return render_object(policy, budget)


def named_policy_map(
    records: dict[str, T], render: Callable[[T], str], budget: TextBudget
) -> str:
    budget.reserve(2)
    keys = [(quoted(name, budget), record) for name, record in records.items()]
    keys.sort(key=lambda entry: entry[0])
    entries: list[str] = []
    for name, record in keys:
        budget.reserve(len(name) + 1 + int(bool(entries)))
        entries.append(name + ":" + render(record))
    return "{" + ",".join(entries) + "}"


def node_policy_text(node: Node, infrastructure: str, budget: TextBudget) -> str:
    budget.reserve(len(NODE_PREFIX) + len(NODE_RUNTIME) + len(infrastructure) + 1)
    ordinary = ordinary_policy_text(node.retry, budget)
    return NODE_PREFIX + ordinary + NODE_RUNTIME + infrastructure + "}"


def node_policy_map(body: GraphBody, infrastructure: str, budget: TextBudget) -> str:
    return named_policy_map(
        body.nodes, lambda node: node_policy_text(node, infrastructure, budget), budget
    )


def body_policy_text(body: Subgraph, infrastructure: str, budget: TextBudget) -> str:
    budget.reserve(len('{"retry":,"nodes":}'))
    retry = ordinary_policy_text(body.retry, budget)
    nodes = node_policy_map(body, infrastructure, budget)
    return '{"retry":' + retry + ',"nodes":' + nodes + "}"


def derive_retry_policies(ir_text: str, budget: TextBudget) -> str:
    document = read_document(ir_text, budget)
    provider = document.runtime.provider_retry
    if (
        provider is not MISSING
        and provider.max_wait is not MISSING
        and any(character in "123456789" for character in provider.max_wait)
    ):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    budget.reserve(len(SECTION_PREFIX) + len(',"subgraphs":}'))
    infrastructure = ordinary_policy_text(document.runtime.infrastructure_retry, budget)
    nodes = node_policy_map(document.graph, infrastructure, budget)
    bodies = document.graph.subgraphs
    subgraphs = named_policy_map(
        {} if bodies is MISSING else bodies,
        lambda body: body_policy_text(body, infrastructure, budget),
        budget,
    )
    return SECTION_PREFIX + nodes + ',"subgraphs":' + subgraphs + "}"


def build_graph_retry_policies(ir_text: str, *, max_encoded_bytes: int) -> str:
    return derive_retry_policies(ir_text, TextBudget(max_encoded_bytes))


def read_graph_retry_policies(
    ir_text: str, value: object, *, max_encoded_bytes: int
) -> str:
    budget = TextBudget(max_encoded_bytes)
    saved = render_object(value, budget, reject_aliases=True)
    reader = ObjectReader(saved)
    reader.take(INTERPRETATION_KEY)
    if reader.string() != INTERPRETATION:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if saved != derive_retry_policies(ir_text, budget):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return saved
