from alembic.config import Config
from alembic.script import ScriptDirectory

from kora.db.custody_schema import ISOLATED_FRONTIER_REVISION
from kora.db.migrations.environment import MIGRATIONS_DIRECTORY
from tests.api.guarded_sqlite_marker_child import isolated_head


def test_private_frontier_graph_does_not_change_installed_head() -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
    assert (
        ScriptDirectory.from_config(config).get_current_head()
        == "run_event_metadata_0034"
    )
    assert isolated_head() == ISOLATED_FRONTIER_REVISION
