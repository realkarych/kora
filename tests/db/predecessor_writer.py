import asyncio
import os
import sys
from pathlib import Path

import kora
from kora.db.connection import DATABASE_URL_VARIABLE, Database, parse_database_url
from kora.db.migrations import current_revision
from kora.engine.run_promotion import PromotionOutcome

PRODUCER_REVISIONS: dict[str, str] = {
    "promotion": "0029",
    "admission": "0027",
    "capacity": "0025",
    "deadline": "0024",
}


async def run(producer: str) -> str:
    from tests.engine.admission_wait_support import admission_case
    from tests.engine.expiry_support import execution_case
    from tests.engine.promotion_support import promotion_case

    database = Database(parse_database_url(os.environ[DATABASE_URL_VARIABLE]))
    try:
        async with database.read_transaction() as connection:
            assert (
                await connection.run_sync(current_revision)
                == PRODUCER_REVISIONS[producer]
            )
        if producer == "promotion":
            case = await promotion_case(database)
            assert await case.promote(database) is PromotionOutcome.APPLIED
            return case.run_id
        if producer == "admission":
            return (await admission_case(database)).binding.node_id
        return (await execution_case(database)).attempt_id
    finally:
        await database.close()


def main() -> None:
    source, dependencies, producer = sys.argv[1:]
    package = kora.__file__
    assert package is not None
    assert Path(package).resolve().is_relative_to(Path(sys.prefix).resolve())
    sys.path.extend((source, dependencies))
    sys.stdout.write(asyncio.run(run(producer)))


if __name__ == "__main__":
    main()
