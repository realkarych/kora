# Namespace graph catalogue

The project owner approved this contract's [implementation plan](https://github.com/realkarych/kora/issues/2012#issuecomment-7239444394) under [#2012](https://github.com/realkarych/kora/issues/2012#issuecomment-7239530667). Runtime activation is pending. This contract defines one operation and its consumers. The [run list](run-list.md) and [saved graph-version read](graph-version-read.md) keep their own contracts.

Governing decisions are [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md), [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md), [ADR-0011](../../docs/decisions/0011-namespaces-and-personal-tokens.md) and [ADR-0015](../../docs/decisions/0015-web-console-design.md). [API conventions](api-and-agent-surface.md#1-conventions), [bounded reads](bounded-read-projections.md), [usage coverage](data-model.md#7-usage-records-and-completeness-fr-91) and [durable execution state](data-model.md#13-run-acceptance-and-execution-capacity-adr-0020) govern the corresponding boundaries.

## 1. Request and authority

`GET /api/v1/namespaces/{ns}/graphs` uses the ordinary personal bearer boundary. Check current token validity, personal principal kind and grant for the explicit namespace before looking up namespace existence, interpreting query selectors or reading list sources. An invalid credential, including a cookie-only request, produces `API009`. A valid service principal or personal credential without the namespace grant produces `API005`, even if the namespace does not exist. Resolve the authorized namespace or return `API001`. Ordinary namespace access permits authored graph metadata and the aggregates below. It grants no transcript access, operator, execution or approval authority.

Check graph/version, run/graph, run/version and all subordinate owner links against that namespace before projecting their values. A required relationship to a foreign or inconsistent owner or an invalid required stored representation produces a fixed `API010`, without reflecting a foreign identity or content. Perform no global existence or aggregate lookup on behalf of the request. Recheck authority on every page. A cursor is only a selector.

Accept only the following query members, each at most once. Decode each value separately once. Unknown, invalid, empty or repeated values produce `API003` without reflecting their values.

| Member | Meaning |
|---|---|
| `q` | 1–64 ASCII letters, digits or underscores. Fold ASCII uppercase to lowercase, then match it as a literal substring of the canonical graph name. An underscore is literal. Omission matches all names. |
| `limit` | Canonical decimal integer from 1 through 100, default 100. Signs, whitespace, fractional notation and redundant leading zeroes are invalid. |
| `cursor` | The version 1 selector in section 4. |

## 2. Response and run categories

HTTP 200 returns exactly `{items,next_cursor,observed_at}` as compact JSON. `next_cursor` is a string or null. `observed_at` is the API's UTC observation time after the read snapshot is established. All rows, including accounting dates and classification, use that snapshot and time. Both success and refusal use `Cache-Control: no-store`.

Each item has exactly these members:

| Member | Source and representation |
|---|---|
| `graph` | Canonical namespace-scoped graph name from the graph registry. |
| `active_version` | Null if there is no active version. Otherwise exactly `{graph_version_id,description,top_level_node_count}`, using the highest numbered active version and its public `gv_` ID. The description is the saved authored string, with null for an absent description and the empty string for a recorded empty description. Return complete saved text in the successful item, using lossless escaped JSON where needed. Count the declared top-level entries exactly. A fan-out or subgraph wrapper counts once. |
| `runs` | Exactly `{needs_human,failed,running}`. Each member is exactly `{count,latest_run_id}`, with an exact nonnegative count and a nullable public `run_` ID. |
| `today` | The recorded usage and coverage in section 3. |

The ordinary list reads bounded metadata. Admitting a description or declared count must not materialize or transfer unrelated graph documents, contracts or payloads. If implementation needs an immutable version-scoped scalar representation from publication, qualify that representation and its history backfill before enabling the operation. Resolve the representation from the selected version only. No later registry lookup, current execution profile, or another version can replace its description or count. Use ordinary projection refusal if a valid complete item cannot fit, including when its description alone is too large.

Counts span all versions of the scoped graph. Classify each saved `failed` run as `failed` until its lifecycle changes. Classify each saved `running` run as `needs_human` only when there is a nonempty current frontier of actionable work and all that work is waiting on human decisions. Other saved `running` runs belong to `running`. Other lifecycle states are outside the counts. A retained hold on a terminal run is a separate fact and does not add a member to these counts. The latest ID in each category is the greatest canonical public run ID in that category, using the accepted run-list order. It is null for an empty category.

A current frontier is established from the pinned execution structure, current body iterations and live descendants. Each actionable leaf must be `waiting` with its own scoped current open `approval` wait, the request bound to that same wait and node, a saved pending request status, and a decision deadline that has not arrived. A pending downstream node that cannot act until one of those approvals resolves is dormant. Wrapper bookkeeping with only those waiting descendants does not add independent work. Eligibility of other pending work uses its current activation and predecessors. A ready or independently eligible pending node, queued or operating execution, automatic wait, approved request awaiting verification, or due expiry is other work. Non-terminal physical attempts and live, unknown or recovery-required attempts under [the existing run-wide hold predicate](run-list.md#admission-and-filter-predicates) also prevent a human-only classification, including when a current pointer has moved to another attempt. An unproven human-only frontier belongs to `running`. A malformed required saved fact is a refusal. A top-level wait count or an older event never establishes that all current work waits for a person.

For example, a running wrapper whose only actionable body leaf has a current pending approval can count as `needs_human` even when a downstream command is dormant. The same run belongs to `running` once there is a queued sibling, expired decision window or unresolved old physical attempt. An approved VCS request still under head verification belongs to `running`. A failed subgraph attempt does not make a saved `running` run count as `failed`.

## 3. Day and accounting coverage

Read the effective [trusted namespace policy](data-model.md#trusted-admission-initialization-issues-1446-and-1484) in the page snapshot. Its optional `timezone` is an IANA accounting timezone. A valid policy with the field absent uses `UTC`. Provisioning rejects an invalid or unavailable supplied zone before writing the policy. A malformed present stored value or unavailable zone produces a fixed safe failure. Use the same accounting day for this catalogue, namespace daily cost and future daily-budget admission. Activation of the latter two operations remains separate.

The accounting date is `observed_at` converted to the policy zone's Gregorian local date. An attempt belongs to it exactly when the attempt's saved first-execution `started_at` converted using those same rules has that date. This definition handles 23- and 25-hour days, transitions at local midnight and historical changes that repeat or skip dates. A native lookup may use a conservative indexed UTC range if it then applies exact date membership and charges extra examined domain rows to the shared budget. Do not substitute a fixed 24-hour duration, a process or database session timezone, or the browser's presentation timezone.

Usage includes all selected attempts, including failed, retried, superseded and fan-out attempts. Count persisted coverage even when a covered agent attempt has no model rows. A healthy live agent can have partial or unavailable evidence. A never-granted cancellation or command-only execution does not manufacture a model-usage gap. Apply the [recorded usage and predecessor rules](data-model.md#74-immutable-approval-cost) to the selected day independently from a run's current version or status. A predecessor whose required timing or accounting facts cannot be interpreted under those rules is an explicit refusal. Select by the execution start, not the latest report arrival, so later accepted usage for a selected attempt is visible on subsequent reads of the same date. Use stored normalized observations, prices and coverage. Read no live counters, source payloads or current price map to reconstruct estimates.

The `today` record has exactly these members:

| Member | Meaning |
|---|---|
| `date`, `timezone` | ISO `YYYY-MM-DD` date and its effective IANA timezone. |
| `tokens` | Exact nonnegative JSON integer when all additive token dimensions and applicable coverage are complete, else null. Counts add uncached input, cache reads, cache creation and output. Reasoning is already part of output. |
| `known_tokens` | The exact subtotal of observed token counts, or null when there are covered attempts and no usable token count. This equals `tokens` for a complete value. |
| `completeness` | Weakest applicable normalized completeness, including covered attempts without usable reports. Missing token dimensions still make the total unavailable even when a reported subtotal exists. |
| `usd_estimate`, `known_usd_estimate`, `pricing_status` | Exact USD estimate, known subtotal and pricing status from the version 1 run-cost rules, applied to this day. Money uses canonical decimal strings or null. Subscription-only known usage has null USD and `not_billable`. Mixed priced and unpriced/subscription/missing evidence preserves its priced subtotal and coverage. |
| `missing_report_attempts`, `attribution_gap_attempts`, `observation_gap_attempts` | Exact coverage counts for the day in the existing run-cost meanings. Count an attempt once in each applicable category. Categories can overlap. |

A genuinely empty cost-producing ledger has complete zero tokens and estimated zero USD. If an eligible agent starts before the day boundary, a report it publishes during the following day still belongs to its start date. If eligible attempts start during both repeated clock hours on a fall-back day, their records both count for that date. A missing report yields unavailable or partial coverage. A known priced subtotal from another attempt is useful with its coverage, and does not establish the full cost. Use safe JSON integers through 9,007,199,254,740,991 and the [exact decimal monetary domain](data-model.md#74-immutable-approval-cost). A sum exceeding a representation budget refuses the page with `API002/read_projection_too_large` and the affected authorized graph. There is no rounded or truncated summary.

## 4. Ordering, snapshots and finite reads

Rank all matching scoped graphs before page selection. Priority 0 has any `needs_human` run. Priority 1 has any failed run and no `needs_human` run. Priority 2 covers the remainder. Within a priority sort by canonical graph name ascending in ASCII order. The name is unique within the namespace.

Use an unpadded base64url continuation of compact UTF-8 JSON, with exactly the members below. The existing structural [continuation checks](bounded-read-projections.md#2-continuations-and-observation-semantics) apply, including the 8 KiB encoded cap before decode, canonical encoding, duplicate-member refusal and exact scope binding.

- `version` is 1, `operation` is `list_graphs` and `order` is `priority_name_asc`.
- `namespace_id` binds the durable authorized namespace. `filters` contains exactly `q` and `limit`, using normalized query text or null and the effective integer limit.
- `ceiling` is the greatest canonical scoped graph identifier before filtering on the initial page. The scan never admits a graph with a later identifier. An empty namespace needs no continuation.
- `after` is exactly `{priority,graph}`, from the last emitted row. Continue strictly after that key in the current order.

Membership and rank are observed anew on every request. An emitted graph that moves ahead can reappear. A graph that moves behind the cursor can be missed. For example, after reading the last person-waiting graph, an ordinary graph may begin waiting and move behind the cursor. Refresh starts a new traversal. The identifier ceiling is no commit watermark. No live transaction or server-side cursor survives a request.

Use one public request and one finite read snapshot per page. Indexed batches admit catalogue candidates, nonmatches, sources, aggregate inputs and lookahead against one 100,000-domain-row budget and one five-second monotonic deadline starting before snapshot checkout. Budget exhaustion produces `API002/read_budget_exceeded` and no partial page. Bound native work by the remaining statement deadline. Native page/index visits are measured independently of admitted domain rows. Preserve exact application-side monetary addition on both databases. The implementation must qualify scoped index access against growing histories within selected graphs and histories in unrelated graphs and namespaces. A date index that filters only global today's attempts does not bound work to selected owners. Queries are issued for bounded batches, without a per-graph query loop or persisted mutable counters.

Count compact UTF-8 JSON bytes including the actual continuation. The 1 MiB cap may shorten an otherwise available page; resume strictly after the last emitted row. Return null only after establishing exhaustion under this page's observation. An unfit single item produces the standard exact-item refusal. Release read results and the snapshot before response backpressure, using the native deadline/cancellation settlement from [#1564](scheduler.md#10-standalone-sqlite-versus-postgres). Completion, refusal, cancellation and shutdown all settle the resources under their existing owner.

## 5. Shared consumers and Console activation

The operation ships with one shared Python client call, `kora graphs [--q Q] [--limit N] [--cursor C]`, stdio authoring `kora_reference` selector `{kind:"graphs",namespace,q?,limit?,cursor?}`, and generated OpenAPI and TypeScript. Forward selectors as separate values. Requests read one page; clients apply existing finite response bounds and surface structured server and local failures. CLI JSON and MCP return the API page and its nullable cursor and completeness facts. Human-readable output renders authored text inertly and distinguishes partial or unavailable amounts. No client aggregates, repaginates or assigns rank on its own. Existing graph detail and version modes inherit no new selector.

The later installed Graphs entry uses the [page/navigation and state contracts](../design/console/pages.md#graphs-the-entry). It calls the same scoped handler through a separately approved current-session ingress. The public bearer path remains a bearer path. Activate the entry only with working namespace bootstrap, graph addresses and run selection (including the case with no active version). The first complete, unfiltered observation of exactly one item with a null cursor authorizes the UX decision to replace the list address with that graph's address. Search results, continuation pages and stale or refused observations never establish single-graph state. The Console uses the exact category ID for a traffic-light link, shows unknown or partial cost distinctly from zero, and treats returned description text as text. Its bounded page replaces the prior page rather than accumulating an unbounded catalogue in the browser.

## 6. Delivery and evidence

The graph catalogue operation owner activates the API, Python, CLI, MCP and TypeScript together under a separately approved implementation issue. The metadata owner qualifies the native read plan and any required immutable projection, predecessor backfill and indexes before that route serves. The installed Graphs owner activates the browser journey after that operation and the graph/run destinations are available. This contract activates no runtime operation.

The API owns one bounded DEBUG completion/refusal event with safe operation, outcome/code, emitted rows/bytes and elapsed time. Use existing request correlation. Exclude query/cursor values, identities/credentials, descriptions, policy input, billing detail and database text. Event delivery cannot change release or read outcomes. No durable audit/domain event is added.

| Future test | Evidence at implementation |
|---|---|
| `test_graph_list_enforces_current_namespace_authority` | Real current personal token and explicit namespace. API009/005 precedence, no query or source access on authority refusal, cross-scope owners, revoked or narrowed scope on continuation and ordinary access without optional flags. |
| `test_graph_list_has_client_surface_parity` | One real shared-client/CLI/MCP call returns the same API page and error. Generated OpenAPI/TypeScript covers identical fields, nullable costs and selectors. |
| `test_graph_list_resolves_priority_and_latest_runs` | Global rank before page, body/approval/frontier cases, terminal holds and latest in each exact bucket, active version selection and recorded metadata without full source bodies. |
| `test_graph_list_accounts_for_namespace_day_and_coverage` | Midnight rollover, DST, earlier start with later report, missing report, normalization predecessors, reasoning counted once, exact Decimal, mixed or subscription usage, and unrelated namespace exclusion. |
| `test_graph_list_continuation_is_scope_and_filter_bound` | Literal normalized search, malformed/refused queries and cursor, scope/filter/limit mismatch, ceiling, priority movement, byte-shortened page and exact exhaustion. |
| `test_graph_list_bounds_native_work` | SQLite/PostgreSQL query plans and real work bounds at page sizes 1/50/100 with selected-graph old history and unrelated history, both row/deadline refusal and explicit unfit item. Demonstrate bounded source transport and no per-graph query. |
| `test_graph_list_cancellation_releases_snapshot` | Real native cleanup and bounded release on completion, refusal, cancellation and shutdown, without holding a snapshot for response delivery. |
| `test_installed_console_graph_catalogue_navigation` | Real installed HTTPS, empty/one/many, namespace-wide search and pagination, correctly scoped graph/run links, session rights loss, late response rejection and keyboard-visible state. |

Use SQLite and PostgreSQL and existing supported-platform coverage where the boundary is native. Use real components and deterministic synchronization. Record native query shape, admitted rows, database work and release evidence at activation. The design PR verifies the contract, linked docs and existing CI. The future tests in this table supply no passing runtime evidence for this PR.
