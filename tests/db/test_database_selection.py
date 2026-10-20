import pytest

from tests.support.databases import DATABASES_VARIABLE, DatabaseKind, selected_databases


def test_database_selection_is_explicit() -> None:
    both = (DatabaseKind.SQLITE, DatabaseKind.POSTGRESQL)

    assert selected_databases({}) == both
    assert selected_databases({DATABASES_VARIABLE: "sqlite"}) == (DatabaseKind.SQLITE,)
    assert selected_databases({DATABASES_VARIABLE: "postgresql"}) == (
        DatabaseKind.POSTGRESQL,
    )
    assert selected_databases({DATABASES_VARIABLE: " postgresql , sqlite "}) == (
        DatabaseKind.POSTGRESQL,
        DatabaseKind.SQLITE,
    )
    assert selected_databases({DATABASES_VARIABLE: "sqlite,sqlite"}) == (
        DatabaseKind.SQLITE,
    )
    for rejected in ("", " , ", "mysql", "SQLite", "sqlite;postgresql"):
        with pytest.raises(pytest.UsageError):
            selected_databases({DATABASES_VARIABLE: rejected})
