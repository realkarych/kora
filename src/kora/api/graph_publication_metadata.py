import re
from typing import Never, Self

from kora.api.graph_publication_values import (
    DEPLOYED_BY_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
)
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.types import NAME_LENGTH, JsonObject, canonical_json
from kora.ir.text import utf8_length
from kora.protocols.authentication import Principal

_OWNER: re.Pattern[str] = re.compile(r"[!-~]{1,256}")
_ISSUER = object()


class PreparedGraphPublicationMetadata:
    __slots__ = ("dialect", "_resolved_digests_json", "_deployed_by")
    dialect: Dialect
    _resolved_digests_json: str
    _deployed_by: str

    def __new__(
        cls, issuer: object, dialect: Dialect, resolved_digests_json: str, owner: str
    ) -> Self:
        if issuer is not _ISSUER:
            del issuer, dialect, resolved_digests_json, owner
            raise TypeError("graph publication metadata requires the checked producer")
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "dialect", dialect)
        object.__setattr__(prepared, "_resolved_digests_json", resolved_digests_json)
        object.__setattr__(prepared, "_deployed_by", owner)
        return prepared

    def __setattr__(self, name: str, value: object) -> Never:
        del self, name, value
        raise AttributeError("graph publication metadata is immutable")

    def __delattr__(self, name: str) -> Never:
        del self, name
        raise AttributeError("graph publication metadata is immutable")

    def __getstate__(self) -> Never:
        del self
        raise TypeError("graph publication metadata requires explicit access")

    def parameters(self) -> dict[str, str]:
        return {
            RESOLVED_DIGESTS_PARAMETER: self._resolved_digests_json,
            DEPLOYED_BY_PARAMETER: self._deployed_by,
        }


def _publication_owner(principal: Principal, dialect: Dialect) -> str:
    if type(principal) is not Principal or type(dialect) is not Dialect:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    owner: object = getattr(principal, "owner", None)
    kind: object = getattr(principal, "kind", None)
    runtime: object = getattr(principal, "runtime", None)
    if (
        type(kind) is not str
        or kind != "user"
        or runtime is not None
        or type(owner) is not str
        or _OWNER.fullmatch(owner) is None
        or (dialect is Dialect.POSTGRESQL and len(owner) > NAME_LENGTH)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return owner


def _canonical_resolved_digests(
    entries: tuple[tuple[str, str], ...], allowance: int
) -> str:
    if type(entries) is not tuple:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    remaining = allowance - (6 * len(entries) + 1 if entries else 2)
    if remaining < 0:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    document: JsonObject = {}
    for entry in entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        name, digest = entry
        if type(name) is not str or type(digest) is not str:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        if len(name) + len(digest) > remaining:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        name_width = utf8_length(name)
        digest_width = utf8_length(digest)
        if (
            name_width is None
            or digest_width is None
            or "\x00" in name
            or "\x00" in digest
            or name in document
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        remaining -= name_width + digest_width
        if remaining < 0:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        document[name] = digest

    encoded = canonical_json(document)
    width = utf8_length(encoded)
    if width is None:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if width > allowance:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    return encoded


def prepare_graph_publication_metadata(
    dialect: Dialect,
    principal: Principal,
    resolved_digests: tuple[tuple[str, str], ...],
    *,
    max_metadata_bytes: int,
) -> PreparedGraphPublicationMetadata:
    owner: str | None = None
    encoded: str | None = None
    try:
        if type(max_metadata_bytes) is not int or max_metadata_bytes <= 0:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        owner = _publication_owner(principal, dialect)
        encoded = _canonical_resolved_digests(
            resolved_digests, max_metadata_bytes - len(owner)
        )
        return PreparedGraphPublicationMetadata(_ISSUER, dialect, encoded, owner)
    except GraphValueRejected as rejected:
        reason = rejected.reason
    finally:
        del dialect, principal, resolved_digests, max_metadata_bytes, owner, encoded
    raise_safe(GraphValueRejected(reason))
