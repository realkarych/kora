from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import upgrade_database
from tests.db.seed import seed_predecessor


async def upgraded_predecessor(settings: DatabaseSettings) -> Database:
    seeded = Database(settings)
    try:
        async with seeded.transaction() as connection:
            await connection.run_sync(seed_predecessor)
    finally:
        await seeded.close()
    await upgrade_database(settings)
    return Database(settings)
