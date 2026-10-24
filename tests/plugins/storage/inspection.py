from kora.plugins.storage.local import LocalArtifactStorage
from kora.plugins.storage.state import LocalState, integer, row_values


class InspectedStorage(LocalArtifactStorage):
    def control_state(self) -> LocalState:
        return self._state

    def epoch(self) -> int:
        return self._state.current_epoch()

    def resource_counts(self) -> dict[str, int]:
        queries = {
            "stage_rows": "SELECT COUNT(*) FROM stages",
            "delete_rows": "SELECT COUNT(*) FROM deletions",
            "receipts": "SELECT COUNT(*) FROM receipts",
            "content_rows": "SELECT COUNT(*) FROM contents",
            "reserved_bytes": "SELECT COALESCE(SUM(reserved), 0) FROM stages",
        }
        counts: dict[str, int] = {}
        with self._state.transaction(synchronize=False) as connection:
            for name, query in queries.items():
                cursor = connection.execute(query)
                try:
                    row = row_values(cursor.fetchone())
                finally:
                    cursor.close()
                if row is None:
                    raise AssertionError("private control inventory is missing")
                counts[name] = integer(row[0])
        counts.update(
            owned_jobs=len(self._jobs),
            native_jobs=len(self._native),
            readers=len(self._readers.streams),
            connections=self._state.connections,
        )
        return counts

    def assert_closed(self) -> None:
        if (
            self._jobs
            or self._native
            or self._read_jobs
            or self._readers.streams
            or self._readers.handles
            or self._state.connections
            or self._data_count
            or self._control_count
            or not self._root.closed
        ):
            raise AssertionError("actual provider resources remain after shutdown")
