import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.identifiers import is_identifier
from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT, native_value_bound
from kora.db.tables import namespace
from kora.db.types import JsonObject, canonical_json
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.engine.start_capacity import InvalidCapacityPolicy, outstanding_limit
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.namespace_policy.prepared",
        COMPONENT,
        Level.DEBUG,
        "namespace policy prepared for commit",
        {"changed": FieldKind.BOOLEAN},
    )
)


class NamespacePolicyRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("the namespace policy initialization was refused")


def running_attempt_limit(policy: object) -> int | None:
    if type(policy) is not dict:
        raise NamespacePolicyRefused()
    fields = cast(dict[str, object], policy)
    concurrency = fields.get("concurrency", {})
    if type(concurrency) is not dict:
        raise NamespacePolicyRefused()
    limits = cast(dict[str, object], concurrency)
    if "max_running_attempts" not in limits:
        return None
    limit = limits["max_running_attempts"]
    if type(limit) is not int or limit <= 0:
        raise NamespacePolicyRefused()
    return limit


def checked_policy(value: object) -> JsonObject:
    try:
        native_value_bound(value, NAMESPACE_POLICY_NATIVE_LIMIT)
        running_attempt_limit(value)
        outstanding_limit(value)
        document = canonical_json(cast(JsonObject, value))
    except (ValueError, TypeError, RecursionError, UnicodeError, InvalidCapacityPolicy):
        raise_safe(NamespacePolicyRefused())
    result: JsonObject = json.loads(document)
    return result


@dataclass(frozen=True, slots=True)
class NamespacePolicyConfiguration:
    namespace_id: str
    revision: int
    document: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not is_identifier(self.namespace_id)
            or type(self.revision) is not int
            or not 0 < self.revision <= 2**31 - 1
        ):
            raise NamespacePolicyRefused()
        object.__setattr__(self, "document", canonical_json(self.value()))

    @classmethod
    def prepare(
        cls, namespace_id: str, revision: int, policy: object
    ) -> "NamespacePolicyConfiguration":
        return cls(namespace_id, revision, canonical_json(checked_policy(policy)))

    def value(self) -> JsonObject:
        try:
            value: object = json.loads(self.document)
        except (ValueError, RecursionError):
            raise_safe(NamespacePolicyRefused())
        return checked_policy(value)


def same_policy(left: object, right: object) -> bool:
    pending: list[tuple[object, object]] = [(left, right)]
    while pending:
        first, second = pending.pop()
        if type(first) in (int, float) and type(second) in (int, float):
            if Decimal(str(first)) != Decimal(str(second)):
                return False
        elif type(first) is not type(second):
            return False
        elif type(first) is dict:
            a = cast(dict[str, object], first)
            b = cast(dict[str, object], second)
            if a.keys() != b.keys():
                return False
            pending.extend((value, b[key]) for key, value in a.items())
        elif type(first) is list:
            x = cast(list[object], first)
            y = cast(list[object], second)
            if len(x) != len(y):
                return False
            pending.extend(zip(x, y, strict=True))
        elif first != second:
            return False
    return True


def namespace_policy_scopes(namespace_id: str) -> tuple[Scope, ...]:
    return (Scope(ScopeKind.GLOBAL), Scope(ScopeKind.NAMESPACE, (namespace_id,)))


async def write_namespace_policy(
    connection: AsyncConnection,
    locks: TransitionLocks,
    configuration: NamespacePolicyConfiguration,
    at: datetime,
) -> bool:
    locks.require_connection(connection)
    locks.require(None, namespace_policy_scopes(configuration.namespace_id))
    value = configuration.value()
    result = await connection.execute(
        select(namespace.c.policy_revision, namespace.c.policy).where(
            namespace.c.id == configuration.namespace_id
        )
    )
    row = result.first()
    if (
        row is None
        or type(row.policy_revision) is not int
        or not 0 < row.policy_revision <= 2**31 - 1
    ):
        raise NamespacePolicyRefused()
    revision: int = row.policy_revision
    if revision > configuration.revision or (
        revision == configuration.revision and not same_policy(row.policy, value)
    ):
        raise NamespacePolicyRefused()
    changed = revision < configuration.revision
    if changed:
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == configuration.namespace_id)
            .values(policy=value, policy_revision=configuration.revision, updated_at=at)
        )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, changed=changed)
    return changed
