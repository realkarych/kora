from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, TextBudget
from kora.ir.agent_session_view import VIEW_BYTES as VIEW_BYTES
from kora.ir.agent_session_view import (
    AgentSessionViewUnavailable,
    read_agent_session_view,
)
from kora.ir.json import JsonObject


def agent_session_checker(budget: TextBudget) -> JsonObject:
    budget.reserve(VIEW_BYTES)
    try:
        view = read_agent_session_view()
    except AgentSessionViewUnavailable:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None
    return {
        "kind": "published_view",
        "name": "AgentSession",
        "policy": view.policy,
        "schema": view.schema,
    }
