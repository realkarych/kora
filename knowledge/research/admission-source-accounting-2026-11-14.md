# Initial admission source accounting

Scope: the native source boundary under #1542. This record separates database input admission from returned native buffers and later detached processing. The [scheduler contract](../specs/scheduler.md#initial-admission-source-readers-1542) governs eligibility and ownership. None of these reads creates an attempt, reserves execution capacity or makes a final admission decision.

## Shared graph acquisition

`engine.graph_snapshot.acquire_graph_snapshot` borrows the caller's coherent connection. It preserves the existing Claim reader's order: validate graph read admission, read the supported SQLite encoding when applicable, project the admitted graph fields, then acquire the bounded resource-owner inventory. Its immutable result contains the graph pair and three digests, native resource bytes and resource owners. It creates no transaction, task, thread or cache. The existing Claim composition owns its deadline, statement guard, snapshot settlement and diagnostics.

For a future scheduler composition, the run-pinned execution version and the independently selected live cap version can differ. Its aggregate admission must reserve both before checkout; same-version sharing may reduce actual retention but cannot increase the allowance. This extraction does not by itself implement that scheduler owner or extend Claim's walking-skeleton PostgreSQL migration-lease exception.

## Candidate owner projection

`read_admission_candidate` performs at most two exact point reads: node ID to run ID/path/status, then run ID to namespace/graph/version IDs. Invalid request identity or row allowances issue no statement. A refused node prevents the run read. Every query result closes before the next query, and the helper verifies that the original active transaction still owns the observation before returning.

SQLite uses the existing canonical-key partial indexes in `main`. Before evaluating initial-node/run predicates and projecting variable-width values, each projection admits the complete stored row. The allowance includes storage widths of every column plus `9 * (column_count + 1)` bytes for the maximum record header. Native null, integer and real values charge 0, 8 and 8 bytes; other storage classes use `octet_length`. PostgreSQL projects schema-bounded scalar fields without reading node or run JSON. Its unchanged schema is part of the trusted database boundary.

Each projected text value is separately limited to four bytes per allowed character before transfer. SQLite additionally requires text storage. The maximum projected payloads are:

| Point read | Projected values | Maximum text bytes before application validation |
|---|---|---:|
| Node | Run ID, node path, initial status | `4 * (26 + 2048 + 7) = 8324` |
| Run | Namespace ID, graph ID, pinned version ID | `4 * 3 * 26 = 312` |

Application validation requires canonical identifiers, a nonempty path of at most 2,048 characters without NUL, and one of the three initial states. The node ID is the already validated caller value. The returned immutable object retains only those scalar values and their fixed structure, not the row/result or source JSON. Aggregate composition must account for both temporary rows and the returned structure; these text figures are not process heap or RSS bounds.

## Price selection

`read_admission_price_id` performs one latest-effective namespace lookup and, only when no such row exists, one latest-effective global lookup. Both retain the version-1 partial-index predicate and `LIMIT 1`. Projection admits at most `4 * 26 = 104` bytes for the selected identity before canonical validation. A present row with an invalid identity returns unavailable without a global lookup. Selection does not read a price map or infer that it is valid; the separate native price acquisition refuses unavailable or oversized selected data without an older-price fallback.

Native query plans are diagnostics. SQLite cases verify the expected compact locator and price indexes. PostgreSQL may choose a sequential scan for small fixtures; neither an observed plan nor a latest-row result establishes a universal MVCC tuple-visit, execution-time or RSS bound.

## Verification and remaining ownership

The point-reader cases run on real SQLite and PostgreSQL. They exercise accepted and refused states, absent/invalid identities, explicit row allowances, source-free PostgreSQL scalar reads, Unicode paths, whole-revision price precedence and invalid selected data. SQLite-native cases additionally cover UTF-8, UTF-16le and UTF-16be and refuse oversized, binary and NUL-containing paths. These owning-boundary fixtures do not manufacture graph read-admission witnesses.

The complete #1542 acceptance still requires actual graph publication, manual-run and configuration producers, one coherent snapshot, two graph identities, early wait refusal, per-source/aggregate admission, detached checking after release, safe diagnostics and actual closure on failure, deadline, cancellation and shutdown. #1446 owns the complete detached processing allowance, current accepted inputs, preparation and the original locked writer; #1448 owns loop composition and cause classification. These obligations are not established by the shared graph extraction or scalar-reader tests alone.
