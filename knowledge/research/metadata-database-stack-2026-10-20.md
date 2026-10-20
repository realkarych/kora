# Metadata database stack

Checked: 2026-10-20, for [issue #153](https://github.com/realkarych/kora/issues/153), against `master` at `01871c3`. This record carries the volatile facts behind the "Metadata DB access", "Testing", "Python build toolchain" and "Continuous integration" rows of the [stack table](../architecture/detailed.md#stack-per-component): exact pins, release dates, wheel availability, provenance, licences, the container image, and what the scanners do and do not see. The decisions themselves are in [ADR-0013](../../docs/decisions/0013-implementation-stack.md) and the plan approved on the issue.

## Runtime and development dependencies

| Package | Version | Released | Role | Licence | PyPI attestation |
|---|---|---|---|---|---|
| `sqlalchemy[asyncio]` | 2.1.5 | 2026-10-16 | Core table objects, the async engine and the dialects of `kora.db` | MIT | yes (`sqlalchemy/sqlalchemy`) |
| `alembic` | 1.20.0 | 2026-09-11 | migrations through `kora db upgrade`, the environment configured in code | MIT | none |
| `aiosqlite` | 0.22.1 | 2025-12-23 | the SQLite driver of standalone | MIT | yes (`omnilib/aiosqlite`) |
| `asyncpg` | 0.32.0 | 2026-10-06 | the PostgreSQL driver, in the `kora[postgres]` extra | Apache-2.0 | yes (`MagicStack/asyncpg`) |
| `testcontainers` | 4.15.0 | 2026-07-24 | the PostgreSQL container of the test fixture, in the `dev` group | Apache-2.0 | yes (`testcontainers/testcontainers-python`) |

SQLAlchemy 2.1 is taken as the current line of the ADR-0013 decision "SQLAlchemy 2.0": the Core and async engine interfaces that `kora.db` uses are unchanged between the lines, 2.1.5 ships `cp315` wheels, and the only behavioural difference met during implementation is that `greenlet` is no longer a base dependency, which the `asyncio` extra restores. The previous line, 2.0.55 (released 2026-10-16), remains the fallback with the same code.

Transitive additions to `uv.lock` (17 distributions in all, from 23 to 40 packages): `greenlet` 3.5.6 (MIT and PSF-2.0, no attestation) through the `asyncio` extra; `Mako` 1.4.3 (MIT, no attestation) and `MarkupSafe` 3.0.4 (BSD-3-Clause) through Alembic; `docker` 7.2.0 (Apache-2.0, no attestation), `requests` 2.34.2, `urllib3` 2.8.0, `charset-normalizer` 3.5.2, `idna` 3.20, `certifi` 2026.10.19, `wrapt` 2.5.1 (BSD-2-Clause), `python-dotenv` 1.2.4 (BSD-3-Clause) and, under a `sys_platform == 'win32'` marker that never applies on supported platforms, `pywin32` 312 (PSF-2.0, no attestation) through `testcontainers`; `requests` is Apache-2.0, `urllib3` and `charset-normalizer` MIT, `idna` BSD-3-Clause and `certifi` MPL-2.0, the only weak-copyleft licence in the lock, reached only through the development group (`testcontainers`, `docker`, `requests`). `typing-extensions` was already locked before this change. Every package is hash-locked; `uv` verifies hashes, not attestations, so provenance for the four unattested packages rests on their PyPI accounts.

Wheel availability on the CI lanes, from PyPI metadata on 2026-10-20: `asyncpg`, `sqlalchemy` and `greenlet` publish `cp311` and `cp315` wheels for `manylinux` x86_64 and macOS arm64 (`greenlet` as `universal2`); `MarkupSafe` and `wrapt` publish `cp315` wheels (`wrapt` also a pure wheel); `charset-normalizer` publishes compiled wheels with a pure fallback; `alembic`, `aiosqlite`, `testcontainers`, `Mako`, `docker`, `requests`, `urllib3`, `python-dotenv` and the rest are pure. `pyproject.toml` lists `asyncpg`, `greenlet`, `sqlalchemy`, `markupsafe` and `wrapt` in `[tool.uv] no-build-package`, and CI and the development instructions install with `uv sync --no-build`, so a future interpreter or platform without a wheel fails at setup instead of compiling on the runner; this extends the wheel-only rule the CEL row already states to every native dependency. `asyncpg` ships no `py.typed`; `kora.db` reaches it only through the SQLAlchemy URL, never by import.

## Database behaviour that shaped the implementation

- `sqlite3` connections hold one transaction state, so a read beside a writer on the same connection either fails to begin or joins the writer's transaction. The connection layer therefore owns one writer connection (a static pool of one, `isolation_level=None`, `BEGIN IMMEDIATE` from the `begin` event) under an `asyncio` lock, and a small pool of read-only connections (`PRAGMA query_only=ON`) for read functions; in WAL mode a reader sees the committed state while the writer holds an uncommitted transaction, which `test_sqlite_reads_proceed_while_the_writer_is_held` proves; the readers issue no `BEGIN`, so a read transaction sees per-statement snapshots, as PostgreSQL's read-committed default does, and multi-statement consistency belongs inside the writer transaction.
- SQLite matches a partial index only when the query repeats the index predicate literally. SQLAlchemy renders a bare boolean column on SQLite as `needs_scheduling = 1`, so the partial index on `run (id)` is declared `WHERE needs_scheduling = 1` on SQLite and `WHERE needs_scheduling` on PostgreSQL, and the sweep compares with `== true()`; `test_marked_run_sweep_wraps_behind_the_cursor` asserts the index in the query plan on both databases.
- SQLite cannot reflect expression-based indexes, so Alembic's autogenerate skips the unique index over `(coalesce(namespace_id, ''), name)` of `artifact_type` with a warning; the schema comparison test filters that one warning and the index is created explicitly in migration 0001.
- `jsonb` reorders keys and refuses `\u0000`; the JSON decorator refuses `\u0000` on both databases, stores canonical text on SQLite, maps Python `None` to SQL NULL on both, and tests compare parsed structures. Digests are computed over canonical JSON before storage.
- `Numeric` on SQLite loses exactness for `Decimal`; the amount decorator stores a fixed-scale decimal string on SQLite and `NUMERIC(24, 10)` on PostgreSQL.
- `Boolean(create_constraint=True)` emits a `CHECK (x IN (0, 1))` on SQLite only, where booleans are integers, and nothing on PostgreSQL, whose boolean type refuses other values natively; Alembic's autogenerate renders that constraint twice (once through the column type, once as a table constraint), so the generated migration keeps only the column form. `test_boolean_columns_refuse_other_values` proves the refusal on both databases.
- The partial index `ix_attempt_lease_until` covers the statuses that hold a lease (`claimed`, `submitted`, `running`, `finishing`); a `created` attempt is non-terminal but has no lease yet, so it is left out of the lease-expiry scan.
- SQLAlchemy treats `asyncio.CancelledError` raised during a statement as an exit exception and invalidates the connection. On the single SQLite writer that leaves the old `aiosqlite` connection finishing its statement with an open transaction on its own thread while the pool opens a new connection, whose `BEGIN IMMEDIATE` then fails on the lock. A `handle_error` listener on the writer engine keeps the connection for a cancellation, so the rollback queues behind the running statement on the same connection; the PostgreSQL pool keeps SQLAlchemy's default, because `asyncpg` cancels the query server-side and a replaced pooled connection costs nothing. `Database.run` and `Database.read` execute a whole unit under a shield and re-raise the cancellation only after the unit ended, which is the transaction unit of scheduler.md section 12; the context managers exist for tests and for callers that handle cancellation themselves.
- SQLAlchemy wraps a bind-processing error in `StatementError` whose message contains the statement; the content policy keeps exception messages out of records, which `test_sql_parameters_and_statements_never_appear_in_diagnostics` checks with the `sqlalchemy` loggers admitted at DEBUG.
- Alembic logs through `alembic.runtime.migration` at INFO and prints nothing on `upgrade`; the environment never calls `fileConfig`, and the engine is created without `echo`.

## Closing past the deadline

Checked on 2026-10-20 for [issue #158](https://github.com/realkarych/kora/issues/158) against `master` at 409a763, with SQLAlchemy 2.1.5, aiosqlite 0.22.1 and asyncpg 0.32.0.

- `StaticPool.dispose()` (`sqlalchemy/pool/impl.py`) closes its single connection whether or not it is checked out. The `QueuePool` family closes only the connections idle in the pool when it is disposed.
- The aiosqlite adapter (`AsyncAdapt_aiosqlite_connection.commit` and `rollback` in `sqlalchemy/dialects/sqlite/aiosqlite.py`) does nothing once the aiosqlite connection is closed. A unit whose SQLite writer connection was closed underneath it therefore returned as committed while SQLite had rolled the transaction back. On 409a763 a writer unit held past `close(deadline_seconds=0.2)` returned normally on both databases, and after reopening the database its row was absent on SQLite and present on PostgreSQL.
- `Engine.dispose(close=False)` drops the pool without touching it and gives the engine a new pool, carrying the pool event listeners over. A connection checked in to a dropped pool afterwards stays open until garbage collection.
- aiosqlite runs one non-daemon worker thread per connection. Its finalizer issues a `ResourceWarning` before stopping that thread, so under the test suite's `filterwarnings = error` the warning raises inside the finalizer and the thread is never stopped.

Release owners after this change:

| Resource | Released by |
|---|---|
| the connection a unit checked out (SQLite writer, SQLite reader, PostgreSQL) | the unit, which returns it to the live pool on every path, open unless SQLAlchemy invalidated it (a cancelled PostgreSQL statement) |
| the pools and the connections idle in them | the engine disposal, run once: by `close` when no unit is in flight once it stops waiting, otherwise by the last unit to leave |
| the closing task | started by the first call of `close`, which refuses new units at once; shared by every caller through a shield; a cancelled caller does not stop it, a caller that is not cancelled receives the cancellation when the closing task itself is cancelled, and a closing task cancelled while it waits, as at event-loop shutdown, still sets the closed state and disposes the engines when no unit is in flight, otherwise the last unit does; a closing task cancelled before its first step, when `close` was called in the last iteration before the loop stopped, sets the closed state from its completion callback, so the last unit still disposes, while with no unit in flight nothing disposes the engines, as before this change |
| the `finish_releasing` tasks of a checkout, a begin, a commit, a rollback or a close | the unit, which waits for each through cancellations |

After a close that timed out, the idle pooled connections, and on SQLite their aiosqlite worker threads, stay open until the last unit leaves. When `asyncio.run` shuts the loop down it cancels the remaining tasks, so a unit that can be cancelled ends and releases them. A unit that never ends keeps them open, and the non-daemon worker threads then keep the interpreter from exiting. Before this change `close` closed the idle connections at the deadline and closed the held SQLite writer connection underneath its unit.

A planning review reproduced a leak of an earlier design that disposed the SQLite writer pool without closing it whenever the writer lock was held: the lock is held without a checkout between acquisition and checkout and between checkin and release, and a hand-off at a zero deadline left one aiosqlite connection, its thread and the WAL companion files open in 8 of 210 interleavings. The last-unit-out rule left none.

Laptop figures of the close workloads (Apple Silicon, macOS, Python 3.11.17, PostgreSQL 16.15 in the pinned container), one run per database on 2026-10-21, all 94 gates passing and the whole step 51.6 s; the median and 95th percentile are per cycle, including opening the connections and disposing the engines, but not constructing the `Database`:

| Workload | SQLite | PostgreSQL |
|---|---|---|
| `close_past_deadline`, 50 cycles | 20.1 ms, 21.0 ms | 51.4 ms, 68.5 ms |
| `close_during_handoff`, 50 cycles | 4.0 ms, 4.3 ms | 13.6 ms, 15.9 ms |

A run on 409a763 and on the first revision of the change, under the same conditions, gave comparable figures for the unchanged workloads: connect and dispose 18.3 and 17.9 ms on SQLite, 35.8 and 36.5 ms on PostgreSQL; commit 1.17 and 1.10 ms on SQLite, 2.42 and 2.62 ms on PostgreSQL. The whole step took 46.1 s and 51.3 s.

On the hosted runner (`ubuntu-24.04`, four CPUs, Python 3.11.17), CI run [39492495346](https://github.com/realkarych/kora/actions/runs/39492495346) on revision 16981b2, before the read transaction was released last and before the scenarios stopped at their first failing cycle, passed all 90 gates of that revision; the step took 59.0 s of the 120 s budget, with the container fixture at 1.2 s outside it. The close workloads took 21.2 ms and 9.1 ms per cycle (median; 95th percentile 21.7 and 12.1 ms) on SQLite and 32.9 ms and 14.1 ms (36.0 and 16.7 ms) on PostgreSQL, 3.9 s of measured cycles in all, against 59.8 s for the whole step before this change in run 39452331149. CI run [39498421531](https://github.com/realkarych/kora/actions/runs/39498421531) on revision f5be516 passed all 94 gates in a step of 85.2 s; the close workloads took 25.4 ms and 10.6 ms per cycle (median) on SQLite and 46.0 ms and 21.5 ms on PostgreSQL, and unrelated workloads were slower by the same factor of 1.3 to 1.5 (migration 229 ms against 167 ms in run 39492495346, the event page 21.0 ms against 14.7 ms), which places the difference in the runner. On the laptop, the close workloads take about 1.4 s on SQLite and 3.5 s on PostgreSQL of wall-clock time, warm-up, thread waits and database creation included.

## Container image and test fixture

| Item | Value | Checked |
|---|---|---|
| PostgreSQL image | `postgres:16.15@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d` (Docker Hub, multi-arch, published 2026-10-07) | 2026-10-20 |
| Why 16 | the supported floor of [NFR-11.1](../../docs/requirements/non-functional.md#11-compatibility); one major suffices for the first slice | — |
| Reaper | `testcontainers/ryuk:0.8.1` by tag, mounting the Docker socket read-write, started by default; the fixture disables it through the library configuration when `TESTCONTAINERS_RYUK_DISABLED` is unset, the CI lanes set the variable, an explicit `false` re-enables it; the session fixture starts the container inside the block that stops and removes it, so a readiness failure leaves no container | 2026-10-20 |
| Credentials | user `kora`, a password generated per session with `secrets.token_urlsafe`, never a literal in the sources | — |
| Readiness | the library's `psql` probe is disabled, because its timeout error would carry the password on its command line; the fixture polls a host-side `SELECT 1` through `asyncpg` with a 60 s deadline every 100 ms and fails with a content-free error | — |
| Isolation | one container per session; the template database is migrated once; each test gets `CREATE DATABASE … TEMPLATE kora_template`, dropped with `WITH (FORCE)` in teardown; database names are validated identifiers quoted explicitly | — |
| Port | a random host port published by Docker on the host interfaces for the session; `testcontainers` 4.15.0 types the host side of a port binding as a port, not an address, so the fixture does not bind to the loopback interface | 2026-10-20 |

The container listens on every host interface for the duration of a run; the random password bounds the exposure. OSV-Scanner scans `uv.lock` only; neither the PostgreSQL image nor the reaper image is scanned, and the pin by digest is the control. The fixture connects as the container superuser, so the INSERT-only role grant on the event table, which belongs to deployment, is not exercised by tests.

## Measurements

`tests/measurements/test_metadata_database_lifetime.py` declares its workloads and gates before running and writes every sample to the artifact `metadata-database-lifetime-measurement`. Workloads per selected database: 20 migrate-from-empty cycles with a no-op re-run each; 200 connect-and-dispose cycles; two close-past-the-deadline scenarios of one warm-up and 50 measured cycles each, on a fresh `Database` per cycle (`close_past_deadline`, with a two-connection pool: a held writer, a held read transaction and a writer queued behind them, closed with a 10 ms deadline, the writers released before the read transaction so that a unit holding a checkout is the last to leave; `close_during_handoff`, with a one-connection pool: a close with a zero deadline started from the writer pool's `reset` event while a committed writer checks its connection in, with a second writer waiting behind it). `close_past_deadline` is the workload that detects a disposal that leaves a connection open: substituting the earlier lock-held disposal rule or the disposal at the deadline makes its release gates fail. Each scenario stops at its first cycle whose gates fail, and a cycle that does not finish within 10 s and ends when cancelled fails the scenario's `completed` gate, so a leak is recorded as named failing gates within the step budget instead of exhausting server connections; a hang inside a shielded release (a commit, rollback, connection close or disposal that never returns) is not bounded by the scenario timeout, because the release waits through cancellation, and runs to the job timeout; with the disposal at the deadline substituted, the whole step took 54.9 s and failed the release, write and heap gates of both databases. `close_during_handoff` is one fixed schedule in which the writer behind holds no checkout when the close runs, so it checks the commit boundary and the write outcome, not the disposal rule; 2,000 commit and 2,000 rollback cycles; 200 cancellations of a statement in flight, synchronised on the statement's execution; on SQLite 200 cancellations while waiting for the writer, on PostgreSQL 50 cancellations while waiting for a one-connection pool with a 50 ms checkout timeout; one sweep over 1,000 marked runs beside 1,000 unmarked ones in pages of 100; 200 run finish checks on a run with 10 top-level and 1,000 body instances; the event page over 5,000 events; and 100,000 identifiers. Gates: migration re-run is a no-op and leaves no temporary file; in every close cycle, checked before any garbage collection, every held or queued unit raised `DatabaseClosedError`, the pool `connect` events equal the `close` and `close_detached` events, no connection was closed while a checkout was outstanding and none is outstanding afterwards, the thread count returns to its value before the cycle within 10 s and no SQLite companion file remains; after the cycles, the database holds no row of a unit that was cut short and one row per cycle of the writer committed during the hand-off; every scenario completes; pool checkouts balanced, writer free, no open transaction and no temporary file after every ending path; the cancelled statements were observed running; invalidations as the dialect predicts (none on SQLite, one per in-flight cancellation on PostgreSQL); heap growth over the second half of each cycle set within 64 KiB and under 1 MiB in all; threads, descriptors and server connections back at baselines measured outside the event loop; every marked run visited once per sweep; one statement per finish check; every event row read. Each transaction path records its per-cycle minimum, median, 95th percentile and maximum and the peak traced memory. Durations are recorded, not gated. The step budget is 120 s, with the container fixture timed separately.

Laptop figures (Apple Silicon, macOS, Python 3.11.17, SQLite 3.53.1, PostgreSQL 16.15 in the pinned container on Docker Desktop), one run per database on 2026-10-20 before the pull request's CI run:

| Workload | Count | SQLite | PostgreSQL |
|---|---|---|---|
| migrate from empty | 20 | 95 ms per migration (87 to 107 ms) | 215 ms per migration (204 to 231 ms) |
| connect and dispose | 200 | 17.5 ms per cycle | 36.3 ms per cycle |
| transaction commit | 2,000 | 1.27 ms per cycle, 2,002 checkouts | 1.85 ms per cycle, 2,002 checkouts |
| transaction rollback | 2,000 | 1.14 ms per cycle | 2.27 ms per cycle |
| statement cancelled in flight, synchronised on the running statement | 200 | the cancelled caller waits for the statement and the queued rollback, no invalidation | the driver cancels the query server-side and SQLAlchemy replaces the pooled connection, one invalidation per cycle |
| cancelled while waiting for a one-connection pool | 50 | not applicable | the shielded checkout waits for the pool timeout, then the caller sees its cancellation |
| cancelled while waiting for the writer | 200 | 0.84 ms per cycle | not applicable |
| marked-run sweep | 11 pages | 12 statements, 1,000 runs visited, 1.0 ms per page | 12 statements, 1.2 ms per page |
| run finish check | 200 | 0.56 ms per call, one statement each | 0.70 ms per call, one statement each |
| event timeline page | 51 pages | 10.3 ms per page, 5,000 rows | 4.3 ms per page, 5,000 rows |
| identifier generation | 100,000 | 1.6 µs per identifier | same process |

Threads, descriptors and server connections returned to their baselines; the heap grew by less than 25 KiB over the first half of any cycle set and did not grow over the second half. The transaction cycles record their per-cycle minimum, median, 95th percentile and maximum and the peak traced memory, and the figures are taken with `tracemalloc` active, which inflates allocation-heavy paths. The container start, readiness and template migration are timed separately from the step. The whole step took 12.4 s on SQLite and 24.2 s on PostgreSQL.

Hosted-runner figures (`ubuntu-24.04`, four CPUs, Python 3.11.17), from the artifact of CI run [39452331149](https://github.com/realkarych/kora/actions/runs/39452331149) on revision b6667b6, all 66 gates passing, step 59.8 s with the container fixture at 1.3 s outside it:

| Workload | SQLite | PostgreSQL |
|---|---|---|
| migrate from empty | 165 ms per migration | 256 ms per migration |
| connect and dispose | 46.8 ms per cycle | 66.5 ms per cycle |
| transaction commit (median, 95th percentile) | 1.50 ms, 1.60 ms | 1.58 ms, 1.94 ms |
| transaction rollback (median, 95th percentile) | 1.51 ms, 1.58 ms | 1.48 ms, 1.61 ms |
| statement cancelled in flight (median) | 20.4 ms, no invalidation | 8.9 ms, one invalidation per cycle |
| cancelled while waiting (median) | 0.75 ms for the writer lock | 51.0 ms for the one-connection pool at a 50 ms timeout |
| marked-run sweep | 1.4 ms per page | 1.3 ms per page |
| run finish check | 0.89 ms per call | 0.94 ms per call |
| event timeline page | 13.1 ms per page | 5.5 ms per page |
| identifier generation | 2 µs per identifier | same process |

The logging measurement of the same lane took 71.6 s; the lane as a whole stayed under five minutes and the four workflows of the run finished within about four and a half minutes of wall-clock.

## Limits of this record

Attestations and wheel lists were read from PyPI on the check date and change with every release. The container-image digest pins one build; moving to a later patch is a change to one constant and this table. The measurement figures are from one laptop run and are not budgets; the gates derive from the contracts of [scheduler.md section 12](../specs/scheduler.md#12-execution-backend-boundary-nfr-55) and the lifecycle method of the performance profile.
