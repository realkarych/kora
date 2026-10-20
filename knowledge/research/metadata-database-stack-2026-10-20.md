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

Transitive additions to `uv.lock` (17 distributions in all, from 23 to 40 packages): `greenlet` 3.5.6 (MIT and PSF-2.0, no attestation) through the `asyncio` extra; `Mako` 1.4.3 (MIT, no attestation) and `MarkupSafe` 3.0.4 (BSD-3-Clause) through Alembic; `docker` 7.2.0 (Apache-2.0, no attestation), `requests` 2.34.2, `urllib3` 2.8.0, `charset-normalizer` 3.5.2, `idna` 3.20, `certifi` 2026.10.19, `wrapt` 2.5.1 (BSD-2-Clause), `python-dotenv` 1.2.4 (BSD-3-Clause) and `typing-extensions` 4.16.0 through `testcontainers`. Every package is hash-locked; `uv` verifies hashes, not attestations, so provenance for the four unattested packages rests on their PyPI accounts.

Wheel availability on the CI lanes, from PyPI metadata on 2026-10-20: `asyncpg`, `sqlalchemy` and `greenlet` publish `cp311` and `cp315` wheels for `manylinux` x86_64 and macOS arm64 (`greenlet` as `universal2`); `MarkupSafe` and `wrapt` publish `cp315` wheels (`wrapt` also a pure wheel); `alembic`, `aiosqlite`, `testcontainers`, `Mako`, `docker`, `requests`, `urllib3`, `python-dotenv` and the rest are pure. `pyproject.toml` lists `asyncpg`, `greenlet`, `sqlalchemy`, `markupsafe` and `wrapt` in `[tool.uv] no-build-package`, and CI and the development instructions install with `uv sync --no-build`, so a future interpreter or platform without a wheel fails at setup instead of compiling on the runner; this extends the wheel-only rule the CEL row already states to every native dependency. `asyncpg` ships no `py.typed`; `kora.db` reaches it only through the SQLAlchemy URL, never by import.

## Database behaviour that shaped the implementation

- `sqlite3` connections hold one transaction state, so a read beside a writer on the same connection either fails to begin or joins the writer's transaction. The connection layer therefore owns one writer connection (a static pool of one, `isolation_level=None`, `BEGIN IMMEDIATE` from the `begin` event) under an `asyncio` lock, and a small pool of read-only connections (`PRAGMA query_only=ON`) for read functions; WAL mode gives readers a consistent snapshot while the writer holds an open transaction, which `test_sqlite_reads_proceed_while_the_writer_is_held` proves.
- SQLite matches a partial index only when the query repeats the index predicate literally. SQLAlchemy renders a bare boolean column on SQLite as `needs_scheduling = 1`, so the partial index on `run (id)` is declared `WHERE needs_scheduling = 1` on SQLite and `WHERE needs_scheduling` on PostgreSQL, and the sweep compares with `== true()`; `test_marked_run_sweep_wraps_behind_the_cursor` asserts the index in the query plan on both databases.
- SQLite cannot reflect expression-based indexes, so Alembic's autogenerate skips the unique index over `(coalesce(namespace_id, ''), name)` of `artifact_type` with a warning; the schema comparison test filters that one warning and the index is created explicitly in migration 0001.
- `jsonb` reorders keys and refuses `\u0000`; the JSON decorator refuses `\u0000` on both databases, stores canonical text on SQLite, maps Python `None` to SQL NULL on both, and tests compare parsed structures. Digests are computed over canonical JSON before storage.
- `Numeric` on SQLite loses exactness for `Decimal`; the amount decorator stores a fixed-scale decimal string on SQLite and `NUMERIC(24, 10)` on PostgreSQL.
- `Boolean(create_constraint=True)` emits a check constraint that only applies to SQLite, so the migration would differ by dialect; the tables use plain `Boolean` columns.
- SQLAlchemy wraps a bind-processing error in `StatementError` whose message contains the statement; the content policy keeps exception messages out of records, which `test_sql_parameters_and_statements_never_appear_in_diagnostics` checks with the `sqlalchemy` loggers admitted at DEBUG.
- Alembic logs through `alembic.runtime.migration` at INFO and prints nothing on `upgrade`; the environment never calls `fileConfig`, and the engine is created without `echo`.

## Container image and test fixture

| Item | Value | Checked |
|---|---|---|
| PostgreSQL image | `postgres:16.15@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d` (Docker Hub, multi-arch, published 2026-10-07) | 2026-10-20 |
| Why 16 | the supported floor of [NFR-11.1](../../docs/requirements/non-functional.md#11-compatibility); one major suffices for the first slice | — |
| Reaper | `testcontainers/ryuk:0.8.1` by tag, mounting the Docker socket read-write, started by default; disabled through `TESTCONTAINERS_RYUK_DISABLED=true` in the CI lanes and the development instructions, the session fixture stops and removes the container | 2026-10-20 |
| Credentials | user `kora`, a password generated per session with `secrets.token_urlsafe`, never a literal in the sources | — |
| Readiness | the library's own `psql` probe inside the container, then a host-side `SELECT 1` through `asyncpg` with a 60 s deadline polled every 100 ms | — |
| Isolation | one container per session; the template database is migrated once; each test gets `CREATE DATABASE … TEMPLATE kora_template`, dropped with `WITH (FORCE)` in teardown; database names are validated identifiers quoted explicitly | — |
| Port | a random host port published by Docker on the host interfaces for the session; `testcontainers` 4.15.0 types the host side of a port binding as a port, not an address, so the fixture does not bind to the loopback interface | 2026-10-20 |

The container listens on every host interface for the duration of a run; the random password bounds the exposure. OSV-Scanner scans `uv.lock` only; neither the PostgreSQL image nor the reaper image is scanned, and the pin by digest is the control. The fixture connects as the container superuser, so the INSERT-only role grant on the event table, which belongs to deployment, is not exercised by tests.

## Measurements

`tests/measurements/test_metadata_database_lifetime.py` declares its workloads and gates before running and writes every sample to the artifact `metadata-database-lifetime-measurement`. Workloads per selected database: 20 migrate-from-empty cycles with a no-op re-run each; 200 connect-and-dispose cycles; 2,000 commit and 2,000 rollback cycles, 200 cancellations of a statement in flight and, on SQLite, 200 cancellations while waiting for the writer; one sweep over 1,000 marked runs beside 1,000 unmarked ones in pages of 100; 200 run finish checks on a run with 10 top-level and 1,000 body instances; the event page over 5,000 events; and 100,000 identifiers. Gates: migration re-run is a no-op and leaves no temporary file; pool checkouts balanced, writer free, no open transaction and no temporary file after every ending path; heap growth over the second half of each cycle set within 64 KiB and under 1 MiB in all; threads, descriptors and server connections back at baseline; every marked run visited once per sweep; one statement per finish check; every event row read. Durations are recorded, not gated. The step budget is 120 s.

Laptop figures (Apple Silicon, macOS, Python 3.11.17, SQLite 3.53.1), one run on 2026-10-20 before the pull request's CI run:

| Workload | Count | Figure |
|---|---|---|
| migrate from empty | 20 | 93 ms per migration (87 to 107 ms) |
| connect and dispose | 200 | 16.6 ms per cycle |
| transaction commit | 2,000 | 1.11 ms per cycle, 2,002 checkouts |
| transaction rollback | 2,000 | 1.09 ms per cycle |
| marked-run sweep | 11 pages | 12 statements, 1,000 runs visited, 0.9 ms per page |
| run finish check | 200 | 0.54 ms per call, one statement each |
| event timeline page | 51 pages | 9.9 ms per page, 5,000 rows |
| identifier generation | 100,000 | 1.6 µs per identifier |

The whole step took 11.5 s on SQLite. PostgreSQL figures and the hosted-runner figures are added from the pull request's artifact once CI has run on the final revision.

## Limits of this record

Attestations and wheel lists were read from PyPI on the check date and change with every release. The container-image digest pins one build; moving to a later patch is a change to one constant and this table. The measurement figures are from one laptop run and are not budgets; the gates derive from the contracts of [scheduler.md section 12](../specs/scheduler.md#12-execution-backend-boundary-nfr-55) and the lifecycle method of the performance profile.
