import sys
from multiprocessing.connection import Connection
from pathlib import Path

from kora.plugins.storage.files import RootAnchor
from kora.plugins.storage.state import LocalState
from kora.protocols.storage_models import StorageLimits


def interrupt_transaction(root: Path, connection: Connection) -> None:
    try:
        with RootAnchor.open(root, StorageLimits(version=1)) as anchor:
            state = LocalState(anchor)
            with state.transaction(write=True) as database:
                database.execute("UPDATE root SET epoch=epoch+1 WHERE singleton=1")
                database.execute(
                    "CREATE TABLE interrupted_control(value TEXT NOT NULL)"
                )
                for _ in range(1024):
                    database.execute(
                        "INSERT INTO interrupted_control VALUES(?)", ("x" * 1024,)
                    )
                journal = root / "control.sqlite-journal"
                with journal.open("rb") as stream:
                    header = stream.read(8)
                if len(header) != 8 or header == b"\0" * 8:
                    raise AssertionError("the interrupted control journal is not hot")
                connection.send_bytes(b"hot_journal")
                if not connection.poll(15):
                    raise TimeoutError("the interrupted transaction was not terminated")
                connection.recv_bytes(16)
    finally:
        connection.close()


if __name__ == "__main__":
    interrupt_transaction(Path(sys.argv[1]), Connection(int(sys.argv[2])))
