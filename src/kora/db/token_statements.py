from datetime import UTC, datetime
from typing import Final

from sqlalchemy import bindparam, or_, update
from sqlalchemy.sql.dml import Update

from kora.db.tables import api_token

REVOKED: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def revoke_token_statement() -> Update:
    expires_at = api_token.c.expires_at
    revoked = bindparam("revoked", type_=expires_at.type)
    return (
        update(api_token)
        .where(
            api_token.c.id == bindparam("token_id"),
            or_(expires_at.is_(None), expires_at != revoked),
        )
        .values(expires_at=revoked)
    )
