# aiosqlite worker thread after a failed SQLite connect

Date: 2026-11-07. Implementing issue: [#918](https://github.com/realkarych/kora/issues/918). Governing contract: [scheduler section 10](../specs/scheduler.md#10-standalone-sqlite-versus-postgres), the SQLite "connection" row. Versions checked: aiosqlite 0.22.1, the latest release on this date, and SQLAlchemy 2.1.5.

## Failures

| Run | Lane | Head | Result |
|---|---|---|---|
| [42357463000](https://github.com/realkarych/kora/actions/runs/42357463000/job/128359325877) | Ubuntu 24.04, Python 3.15.0, measurement cohort | `cf3d59d5` | 1228 passed, 576 deselected, 1 error |
| [42366812306](https://github.com/realkarych/kora/actions/runs/42366812306/job/128386512369) | Ubuntu 24.04, Python 3.11.17, measurement cohort | `35cbc46b` | 1228 passed, 576 deselected, 1 error |

In both runs, the error is a `PytestUnhandledThreadExceptionWarning` reported at the setup of `test_real_database_connection_unavailability_is_not_bad_credentials[postgresql]`, right after the `[sqlite]` case of the same test passed. In `measurements.log`, a `_connection_worker_thread` raised `RuntimeError: Event loop is closed` at `aiosqlite/core.py:66`, where the worker calls `call_soon_threadsafe(set_result, ...)`. While it handled that error, the worker raised again at `core.py:75` (`set_exception`). Neither pull request touched the database layer.

## Cause

The thread is started by a failed `aiosqlite.connect`:

1. `Connection.__await__` starts the worker thread, and `_connect` queues the `sqlite3.connect` call on it.
2. When that call raises, `_connect` calls `stop()` and re-raises. `stop()` queues a close step with a future, and `_connect` never awaits that future.
3. The worker then runs the close step on its own. The step succeeds, so the worker reports its result with `call_soon_threadsafe`. If the loop has closed by then, that call raises at `core.py:66`, and reporting that error raises again at `core.py:75`.

The `core.py:66` frame shows that a queued step succeeded. A failed open cannot succeed, so the step was this close step. The `[sqlite]` case makes the database path a directory, so its `authenticate` (reader pool) and `revoke` (writer pool) calls each fail once to connect. Each failure leaves one such worker.

Two other explanations were excluded:

- **A pooled connection.** `Database` opens no connection when it is constructed, because both pools connect lazily.
- **`Database.close()`.** For a connection that did open, `aiosqlite.Connection.close()` awaits its stop future.

The defect is reported upstream as [omnilib/aiosqlite#394](https://github.com/omnilib/aiosqlite/issues/394), open on this date, and upstream main has the same `_connect`.

## Reproduction

All probes ran on arm64 macOS against `master` at `38d18e72`, with real aiosqlite, SQLAlchemy and `kora.db.connection` and no test doubles. Every probe counts live threads whose name ends in `(_connection_worker_thread)`. A failure is an SQLite path replaced by a directory.

| Probe | Python 3.11.17 | Python 3.15.0 |
|---|---|---|
| `await aiosqlite.connect(directory)` in a fresh loop: a worker is alive when the call raises | 2000 of 2000 | 2000 of 2000 |
| The same, with the loop closed right after the call: a worker raised `RuntimeError: Event loop is closed` | 1 of 2000 | 1 of 2000 |
| `Database.reader_engine.connect()` and `writer_engine.connect()`: a worker is alive when the call raises | 1000 of 1000 | 1000 of 1000 |
| The test's own path through `Database.read`, `Database.run` and `Database.close()`: a worker is alive after `close()` returns | 0 of 2000 | 0 of 2000 |
| The same path, with a `threading.setprofile` hook holding each worker's close step until the loop had closed | 2 workers alive after `close()`, then 2 errors with the `core.py:66` and `core.py:75` chain | Not run: the hook crashes Python 3.15.0 when greenlet is loaded |

The defect is therefore not specific to Python 3.15. Through `Database`, a worker normally finishes within the few loop turns before `close()` returns, so a CI failure needs a worker that is delayed long enough for the test's loop to close first. A loaded runner fits that pattern, but neither run shows why the worker was late.

Two neighbouring paths were checked and leave no worker:

- **A file that is not a database.** It opens, then fails in the `connect` event pragmas. SQLAlchemy 2.1.5 closes that connection gracefully, which awaits the worker.
- **The other SQLite tests.** Before this change, no other test in the repository made an SQLite file fail to open. Closed-database tests raise `DatabaseClosedError` before connecting.

### Probe method

Each probe is a standalone script run with the project environment, `.venv/bin/python probe.py N`, where `N` is the number of iterations. Each iteration creates a fresh event loop with `asyncio.new_event_loop()`, runs one coroutine with `run_until_complete`, and closes the loop immediately. A worker counts when its thread name ends in `(_connection_worker_thread)` and it is alive. Thread errors are collected through `threading.excepthook`. After the last iteration, each script waits up to 5 seconds for workers to exit before it reports.

- **Bare connect.** The coroutine awaits `aiosqlite.connect(directory)`, catches `sqlite3.OperationalError`, and counts workers at that moment.
- **Engine connect.** The coroutine builds `Database(parse_database_url("sqlite+aiosqlite:///<tmp>/unavailable"))`, replaces the file with a directory, then awaits `reader_engine.connect()` and `writer_engine.connect()`. It counts workers when each raises, and again after `Database.close()`:

```python
async def one(root: Path) -> tuple[int, int]:
    database = Database(parse_database_url(f"sqlite+aiosqlite:///{root / 'unavailable'}"))
    (root / "unavailable").unlink()
    (root / "unavailable").mkdir()
    seen = []
    for engine in (database.reader_engine, database.writer_engine):
        try:
            await engine.connect()
        except OperationalError:
            seen.append(workers())
    await database.close()
    return min(seen), workers()
```

- **Test path.** The same setup, but through `database.read` and `database.run` with `SELECT 1`, followed by `Database.close()`.
- **Held close step.** The test path, with `threading.setprofile` installed before the loop starts. Its hook blocks inside any `close_and_stop` frame on a `threading.Event`. The event is set after `loop.close()`. The collected tracebacks end in this chain:

```text
aiosqlite/core.py:66  future.get_loop().call_soon_threadsafe(set_result, future, result)
asyncio/base_events.py  _check_closed -> RuntimeError: Event loop is closed
During handling of the above exception, another exception occurred:
aiosqlite/core.py:75  future.get_loop().call_soon_threadsafe(set_exception, future, e)
asyncio/base_events.py  _check_closed -> RuntimeError: Event loop is closed
```

- **Not a database.** The file is overwritten with non-SQLite bytes after `Database` creates it. `read` and `run` then raise `DatabaseError: file is not a database`. The probe counts workers after `Database.close()`, after `loop.close()`, and after `gc.collect()`.

### Results after the fix

These are the same probes against the fix, together with probes from a resource-lifetime review of the change:

| Probe | Python 3.11.17 | Python 3.15.0 |
|---|---|---|
| Engine connect: a worker is alive when the call raises, or after `close()` | 0 of 1000 | 0 of 1000 |
| Held close step | No worker started, no thread error | Not run |
| 1000 failed opens through the SQLite connector: open descriptors before and after | 7 and 7, no worker | 7 and 7, no worker |
| 300 `asyncio.run` trials ending with `read` and `run` in flight, so the loop's shutdown cancels them | 600 of 600 cancelled, no hang, no thread error, no worker left | Not run |
| The connect wrap step cancelled 200 times, which only shutdown can reach, because `Database._unit` shields the connect | No worker alive after a 5 s join, no thread error; the opened `sqlite3` connection is left to garbage collection in 200 of 200 | Same |
| Executor growth over 3000 cycles of opens, run on 3.11, on 3.15, and on 3.11 under load from 24 busy processes on 12 CPUs (9000 cycles in all) | At most one executor worker. Only the first cycle added it. None left after `asyncio.run` | Same |

The cancelled wrap step behaves as on the code before the fix. aiosqlite clears its connection reference before `stop()` runs, so it does not close the open file, and it does not await the stop future. Before the fix, the same window could also leave a worker alive.

## Fix and its evidence

`kora.db.connection` passes the SQLite engines an `async_creator`. The creator opens the file with `sqlite3.connect` through `asyncio.to_thread`, using the absolute path and today's arguments. Only after a successful open does it wrap the connection in an `aiosqlite.Connection` subclass whose thread is a daemon. A failed open therefore starts no worker.

With the fix, the probes above report no live worker and no thread error on either Python version. `tests/db/test_driver_threads.py` holds the regression tests. On the code before the fix, its `[sqlite]` failure case fails at the check made when `engine.connect()` raises, because the worker exists at that moment. The check after `Database.close()` passes on both versions, as the table shows.

## Reading rule for thread counts

Default-executor workers started by opens stay until the event loop shuts its executor down. Two lifetime measurements count threads once per cycle inside one event loop: the metadata close scenarios through `threads_returned_to`, and the pricing release cycles through `threading.active_count()`. They pass because an earlier step on the same loop has already started those workers: migrations in the metadata measurement, and warm-ups in the pricing measurement. If those steps are reordered, extra threads in the first cycle are these executor workers, one for each open that overlaps another, and not leaked connections.

## Known remaining path

A forced terminate in SQLAlchemy calls aiosqlite `stop()` without awaiting it, which is the same upstream defect. A graceful terminate does await it. A direct `Database.read_transaction()` cancelled twice reached the forced path, and the fix does not change that path. Remove the creator once an aiosqlite release awaits `stop()` in `_connect`, and repeat the probes above when that happens.
