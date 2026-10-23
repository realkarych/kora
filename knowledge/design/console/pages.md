# Console pages, element by element

Status: design detail behind [docs/console.md](../../../docs/console.md), written 2026-10-16, revised after the design review of 2026-10-17. Every element names the API route that serves it ([api-and-agent-surface.md](../../specs/api-and-agent-surface.md) section 2); an element that needed a route change points at [ADR-0015](../../../docs/decisions/0015-web-console-design.md). States, colours and words are in [states.md](states.md); typefaces, scales, colour values and node geometry in [tokens.md](tokens.md); the pilot scenarios in [pilot-scenarios.md](pilot-scenarios.md).

## Bounded read composition

[Bounded read projections](../../specs/bounded-read-projections.md) governs every affected element below. Histories keep one displayed page and one replacement, releasing old bodies and retaining only a bounded refetch anchor. Exact count/sample/continuation markers remain visible; search is scoped server filtering or labelled current-page-only. Runs request 50 rows and logs retain the existing 1,000-event window.

A selected graph scope paints only after its pinned topology and complete compact state observation arrive. Scope expansion uses declared topology paths, not runtime element indexes. At most eight scope snapshots and one global replacement request are retained. Events mark scopes dirty; starts coalesce at most once per second per scope, and the five-second periodic due check recovers missed events. Fair round-robin selection prevents a hot scope from starving others. A total 15-second dispatch/header/body/validation deadline aborts and settles the owned fetch/reader/task, discards its replacement, marks that scope stale and then releases the slot for the next due scope. Hung headers/bodies and repeated timeouts never accumulate a queue. Uncertain cleanup remains owned/reported. An over-limit expansion, including address/Back restoration, asks for a scope to be collapsed. Expanded scopes are separate labelled observations.

Each result binds namespace, identity, object and navigation generation; cancellation also rejects late answers. Keep an old canvas labelled with its old run and remove its actions while replacement loads. Rights loss clears protected data and stops its streams/timers before authorized refetch. Replacement/unmount releases readers, subscriptions, query handles, timers, detached nodes and object URLs. Rendering requires complete size/digest-verified bytes, at most 4 MiB, depth 64 and 65,536 containers/scalars/property names; larger valid originals retain explicit byte/raw access with a rendering-limit state. Prefixes are inert and never JSON, links or complete approval evidence. Exact JSON numbers remain exact. No incomplete form/bound-digest map enables approval.

## Frame

| element | content | route |
|---|---|---|
| top bar | the kora mark at the left edge, in the stroke of the node-kind icons, linking to the console root; the namespace switcher (the token's namespace list; the last chosen namespace remembered in the browser; the first namespace on first entry); the sections Graphs · Targets · Approvals · Cost · System; the user and the theme switch; the document title "page · namespace · kora" | `GET /namespaces/{ns}` for each namespace of the token |
| page | full width below the bar; no side rail | |

## Addresses

Every page has a browser address with the namespace in its path; the console root redirects to the last chosen namespace or, on first entry, to the token's first namespace. What a person would point a colleague at lives in the address; transient filters do not. These are the console's own addresses, not API routes; every page remains a composition of the API routes named in the tables below. Browser Back returns to the previous address with its selection.

| page | address | carried in the address |
|---|---|---|
| Graphs | `/{ns}/graphs` | the search text as `?q=` |
| graph page | `/{ns}/graphs/{graph}` for the newest run; `/{ns}/graphs/{graph}/runs/{run}` for a selected run | `?node={node_path}` for the open node panel (the path percent-encoded); each repeated `expand=` value is one percent-encoded JSON array containing the complete declared topology scope path, using the same IDs/path meaning as API `scope`. Reused subgraph bodies retain their distinct parent paths; runtime element indexes are excluded. Restore against the selected pinned version and apply the eight-scope expansion-limit state, including Back restoration. The timeline filter stays out. |
| review page | `/{ns}/approvals/{apr}` | |
| Approvals | `/{ns}/approvals` | `?status=` for the history block |
| Targets | `/{ns}/targets`; `/{ns}/targets/{key}` | `?active=all` to include idle targets |
| Cost | `/{ns}/cost` | |
| System | `/{ns}/system` | |
| logs | `/{ns}/runs/{run}/nodes/{node_path}/attempts/{att}/logs` | |
| attempt comparison | `/{ns}/runs/{run}/nodes/{node_path}/attempts/{att}/compare` | `?against={att}` |

## Graphs (the entry)

One row per graph of the namespace, the columns in the order of the table; the rows are ordered with the graphs that have a run waiting for a person first, then the graphs with a failed run, then the rest by name; a search field above the list filters by name; a namespace with one graph opens its page directly.

| element | content | route |
|---|---|---|
| traffic light | first column; over the graph's runs in `running` or `failed`: runs whose only live work waits for a person; runs in `failed`; runs running otherwise; zeros hidden; each segment links to the graph page with the newest run in that state selected | the graphs list summary (ADR-0015) |
| name and description | `graph.name`; under it the one-line `description` of the latest active version's document, truncated to the row, the full text on hover | `GET /namespaces/{ns}/graphs` |
| node count | declared top-level nodes of the latest active version; a `fan_out` or `subgraph` node counts once | the graphs list summary (ADR-0015) |
| tokens today | the sum of `usage_record` tokens over the graph's attempts started today in the namespace's timezone; the USD estimate beside it unless the billing class is `subscription` | the graphs list summary (ADR-0015) |
| search | a text field filtering the rows by graph name | client-side over the list |
| click | the graph page | |

Not in the row: triggers and pause state, the last run, the version and its deploy date.

## Graph page

Layout at a window of at least 1280 px wide (the console's minimum; narrower windows follow the responsive rules):

```text
+- top bar 48 px: kora mark · namespace · Graphs Targets Approvals Cost System · user ----------------+
+- graph header 40 px: sql_optimization · v7 · [Run] ----------------------------------------------------+
+- run line 40 px: etl.orders_agg · waiting for you · 2 d · schedule · 184k tokens $3.10 · v7 · rerun of … +
+----------+-------------------------------------------------------+----------------------------------+
| runs     | canvas: the rest of the width                         | node panel 400 px: opens on a    |
| 320 px;  |                                                       | node click, pushes the canvas    |
| collapses|                                                       | (never covers it), closes with   |
| to 48 px |                                                       | Esc or a click on the canvas     |
| of marks |                                                       |                                  |
+----------+-------------------------------------------------------+----------------------------------+
| timeline: 32 px collapsed, 240 px expanded                                                            |
+--------------------------------------------------------------------------------------------------------+
```

The collapsed runs list is a column of 48 px rows, each the run's state mark; the panel and the timeline keep their sizes across runs; the canvas re-fits its viewport when the panel opens and keeps the layout of the nodes.

Navigation between pages:

```text
Graphs --row--> graph page --node panel--> review page (human-input node)
                    |              |------> logs
                    |              +------> attempt comparison
                    +--run line target--> target page
Targets --row--> target page --now block--> review page
                      +--history row--> graph page (that run selected)
Approvals --waiting row--> review page --link--> graph page (the node open)
Cost --by graph--> graph page
System → affected approval, run or node detail from integration degradation and recovery backlog
```

| element | content | route |
|---|---|---|
| runs list (left) | newest first, 50 rows per request; bounded state summary, target, timing and actor; a complete strip only when the API supplies a complete group of at most 64; otherwise exact counts and a selected-run link. Earlier pages replace the displayed page while preserving a bounded refetch anchor. The default/state-filter selection remains. | `GET /namespaces/{ns}/runs?graph=`; selected graph state comes from `GET …/runs/{run}/view?scope=root` |
| graph header | always present: `graph.name`, the latest active version number, the Run button; read-only execution-run usage/cap and cap-source version from the API | `GET …/graphs/{graph}` |
| run line | under the graph header, only when a run is selected: target, state word and elapsed time, trigger and actor, tokens and USD so far, the run's execution version, `rerun_of` and its reruns; separate outstanding/execution-reserved/target-hold facts, with observed time and scope detail under disclosure | `GET …/runs/{run}`, `GET …/runs?rerun_of=`, `GET …/runs/{run}/cost` |
| graph | selected pinned topology, or latest active topology in gray with no run; existing ELK layout and animation. Paint each scope only with its complete state observation; expansion waits for that scope. Events invalidate and coalesced/periodic API reads refresh state. A refused scope offers the paged node list and never paints a partial canvas. | `GET …/graphs/{graph}/versions/{gv}`, `GET …/runs/{run}/view?scope=`, `GET …/runs/{run}/events?follow=true` |
| node, zoomed out | the kind icon in one corner and the state glyph ([states.md](states.md)) in the opposite corner; the rectangle's colour is the state | |
| node, zoomed in | icon, name, time-by-state line and attempt count; current wait/recovery distinctions from the complete selected scope | `GET …/runs/{run}/view?scope=`; details from the selected node |
| parallel instances | aggregate colour and exact current-state counts from the complete selected scope; a full ordered strip only for a group of at most 64. Larger groups show counts and paged details, never the first page as all instances. | `GET …/runs/{run}/view?scope=`; `GET …/runs/{run}/nodes?declared_node=` |
| fan-out node | collapsed counts/complete strip when at most 64; expanded body once per declared scope, showing current subgraph-attempt state only. Counts include all current elements. Searchable element details are paged independently of canvas state. | compact `…/view?scope=` and paged `…/nodes` with declared/parent/element filters |
| node panel | paged fan-out elements, current state and safe reason metadata; explicitly selected saved error/wait fields; paged current/historical attempts, dispatchers, closure evidence and artifacts with owner-scoped content links. Recovery keeps domain/version, observation/provenance, retained scopes and a fully bound operator command. Current attempt details load when selected. Human-input/retry buttons retain their existing placement. | `GET …/nodes/{node_path}`, `…/attempts[/{att}]`, `…/dispatchers[/{epoch}]`, `…/observations`, `GET …/artifacts`; selected `content=` and source/summary reads |
| timeline (bottom) | collapsed: "last event · when"; expanded: the run's events newest first with a filter by node; a clicked event highlights its node | `GET …/runs/{run}/events?follow=true` |
| Run button | in the graph header, reading "Run v<n>" with the latest active version; a form from the graph's `params` JSON Schema (titles, descriptions, required marks, defaults; the supported subset is string, number, integer, boolean, enum, arrays of those and objects one level deep; a field outside the subset is a JSON editor validated against its schema) whose title names the version and, when the selected run's version differs, states in one line "the selected run used v<m>; this run uses v<n>, the latest active version"; submits with a fresh idempotency key on the latest active version; the new run appears first in the list and is selected | `POST /namespaces/{ns}/graphs/{graph}/runs` |
| retry dialog | selected strategy, exact observed reset/recollection counts and paged affected members; deadline/rerun guidance and execution/publication barriers. Preview is advisory; a refusal shows its error/fixed scope without invented zero counts or disabling an otherwise authorized retry. Submission recomputes current effects. | `GET …/nodes/{node_path}/retry-preview`, existing retry POST |
| cancel | fixed run/node scope, exact observed counts and paged current affected attempts; retained holds/reservations and configured grace. A failed preview shows the error and fixed scope; current authorized cancellation remains available. | run/node `GET …/cancel-preview` and existing cancel POST |

Target-source results (ADR-0019): the node panel labels each tentative artifact `staged`, with its normal access/availability, and does not show it as accepted success. A target identity conflict uses the existing failed/configuration-error state and shows bound/proposed keys, source artifact and recovery guidance. Scheduler-only sources expose the same node failure without an attempt row. The panel can show the fully bound existing `kora rerun <run>` command; there is no new console action. Retry preserves the key, while rerun discovers independently and cannot guarantee a requested target without graph-defined selection.

## Review page

Opens from a human-input node and from Approvals; one approval request.

| element | content | route |
|---|---|---|
| header | bound commit/target/deadline from metadata; branch/revision/change-request links only from an explicitly selected complete authorized bound VcsRef source, independent of saved-summary availability | `GET …/approvals/{apr}` and that exact scoped source descriptor; current wait metadata |
| facts: metrics | baseline → candidate → gain, with existing provenance words; only from a completely verified renderable context value | explicitly selected context artifact source, `ReviewBundle.facts` |
| facts: gates | gate name, exit code and verified/unprotected result under the saved rendering policy | completely verified selected context, `ReviewBundle.facts` |
| facts: cost | `run_cost` per model with the weakest completeness | the request view |
| facts: change | changed paths/statistics from selected complete context; bound revision and change-request links only from the explicitly selected complete bound VcsRef source; no prefix link | selected context and exact bound VcsRef source descriptors |
| narrative | summary/rationale labelled written by the agent, inert and in serif; absent/expired/forbidden/render-limit states remain distinct | completely verified selected context, `ReviewBundle.narrative` |
| form | rendered only after complete selected form and bound-digest map verification, from the request's JSON Schema with the same subset as the Run form; the decision field and the submit button, which names the bound commit, in a bar pinned to the bottom of the window; the other fields at the end of the page; without the `approve` flag the bar says beside the button, before any press, that the token lacks the approve right and how it is granted (FR-12.4), and the button, still pressable, repeats it when pressed | `POST …/approvals/{apr}/respond` with `commit_sha` and `digests` as shown |
| stale response | on `API006` the page reloads the request | |
| invalidated request | a banner with `bound_commit_sha` and `observed_head_sha`, the statement that nothing can be approved on this evidence, no form, available/permitted facts and narrative kept as history, the retry button for the producing node (named by the API's `API002` hint) with `fresh_with_feedback`, a link to that node | `GET …/approvals/{apr}`, `POST …/nodes/{node_path}/retry` |
| observation and delivery | integration identity/purpose, last successful observation/time, active deadline, structured error and next retry; desired open/closed delivery, pending/unknown closure even after terminal state; no new action | existing approval/node read projections |
| approved awaiting verification | no decision form; recorded approver and immutable revision, blue current gate with verification/backoff reason; only current node success completes the gate | existing request and node views |
| approved then moved | both commits, the request kept `approved`, the node failed with `candidate_moved` | the request view and the node view |

## Targets

| element | content | route |
|---|---|---|
| list row | key, latest holder status/bounded state summary, accepted request identity, exact holder/deferred/pending counts with incomplete samples and history refs; active by default, switch to all. No VcsRef body or implicit graph-scope fetch for every row. | `GET /namespaces/{ns}/targets?active=true` |
| target page: now | exact holder/deferred counts and bounded samples, including terminal retained holds; paged holder/deferred runs with hold time, status and review references | target metadata; `GET …/runs?target=&holding=true` and `…&target_deferred=true` |
| target page: accepted | latest applied approved request, approver/time/commit; accepted/merged/verified remain separately sourced. Explicitly select that card's bound VcsRef source for links. Show cost only when the API response names the same accepted approval; otherwise clear/refetch. Display the named version-1 cohort, as-of time, exact/null/known-subtotal/completeness semantics and nonadditivity across approvals. | target metadata; scoped source read; `GET …/targets/{key}?view=cost&accepted={apr}` |
| target page: history | paged permanently bound runs, newest first, including failed/rejected runs; immutable origin survives expiry. No console sum of a page as whole-search cost. | `GET …/runs?target=` and `GET …/approvals?target=` |

## Approvals

| element | content | route |
|---|---|---|
| waiting | rows: target, graph, how long it waits and the deadline, the bound commit, `run_cost`; click opens the review page | `GET /namespaces/{ns}/approvals?status=pending`; the deadline from the node's wait (ADR-0015) |
| history | rows: target, decision and approver, time, commit; `invalidated`, `expired` and `failed` with their reason (for `failed`, the CEL error kind and field); pending/unknown channel delivery stays visible on approved/rejected/terminal records | `GET /namespaces/{ns}/approvals?status=` (ADR-0015) |

## Cost

| element | content | route |
|---|---|---|
| today | the namespace's spend today against `daily_budget_usd` as a bar; "admission blocked" when the daily view says so | `GET /namespaces/{ns}/cost/daily` |
| by graph | the graphs with their tokens today, linking to the graph pages | the graphs list summary (ADR-0015) |

Daily history and monthly totals are after MVP.

## System

Read-only in MVP; one health line on top from `GET /readyz`.

| block | content | route |
|---|---|---|
| Workers | namespace-visible worker identity/domain/version/capacity/liveness; separate exact held/recovery counts and incomplete samples with concrete attempt refs. Healthy held work and historical/superseded recovery are distinct predicates. | paged `GET /workers`; scoped runs with `worker`/`worker_attempts=held\|recovery`, then matching per-run attempt page |
| Schedules | per graph: trigger kind, spec, `next_fire_at`, paused; last accepted/skipped/rejected occurrence, reason, quota snapshot and time; sensor observation/degradation, last success and next retry, including same-reference credential repair | `GET …/graphs/{graph}/triggers` |
| Presets and artifact types | the lists; a body on click | `GET /namespaces/{ns}/presets`, `GET /namespaces/{ns}/artifact-types/{name}` |
| Integrations | namespace-visible observation in-flight/limit/degradation, last success/next retry and pending/unknown delivery count/age with links to approval/node detail; external outage separate from core readiness | `GET /namespaces/{ns}` integration-health projection, existing node/approval/trigger reads |
| Policy | namespace policy read-only, including outstanding accepted-run usage/limit (default 1000), observed time and explicit over-limit state after lowering | `GET /namespaces/{ns}` |

## Attempt comparison

Opens from the node panel; two attempts of one node, the counterpart as the route defaults it.

| section | left and right | route |
|---|---|---|
| outcome | status/error class and intended/observed strategy from diff metadata; retained feedback, author and association only from each exact attempt's explicitly selected saved request, with complete-byte/digest, current access and structured-render checks; unavailable/forbidden/over-limit content remains explicit | diff metadata and each owner-pinned `GET …/attempts/{att}?content=request`; never implicit feedback text in the default diff |
| outputs | paged immutable output pairs and digest/availability; selected bounded complete StructuredOutput patch, or explicit computation refusal with original reads. VcsRef branches/links require each complete authorized source. | diff metadata, `…/diff/entries?kind=outputs`, selected `…/diff/patch?scope=output&output=` and original payload reads |
| cost and events | usage per model with completeness; event counts | the same |
| configuration and versions | digest/availability/actual observation metadata; explicit saved input manifest preserving nesting/order/repetitions, saved policy and config selections. Complete structured rendering uses its budget; larger manifests show a limit and complete-read reference. Config comparison counts whole saved requests and may refuse without hiding originals. | diff metadata/entries/selected patch and owner-pinned attempt content reads |

Identical available rows are muted. Missing/expired/not-recorded/unknown values are labelled explicitly, never treated as equal empty values. Diff reads immutable attempt records and stored artifact projections, never current pointers or replacement plugins (ADR-0018). Prompt/feedback and equivalent config/diff projections require namespace `transcripts` access; a forbidden value is distinct from an expired one. A retry requiring unavailable inputs displays the API's structured conflict and source IDs, without adding a console action.

## Logs

Opens from the node panel; one attempt.

| element | content | route |
|---|---|---|
| transcript | the normalised events in order: `turn` and agent messages as text in the serif typeface; `tool_call` and `tool_result` as one collapsible row (tool, arguments in brief, duration, outcome) expanding to the full arguments and result, truncation markers kept; `permission_denied` and `usage` as rows of their own colour; `result` and `error` at the bottom; a page holds 1,000 events and its boundary is a visible line "earlier events" that replaces the retained window with the previous page while preserving the reader's bounded navigation anchor | `GET …/attempts/{att}/logs?follow=true`, paged at 1,000 events |
| live tail | follows while the attempt runs; pauses when the reader scrolls up; a "follow" control resumes | |
| raw transcript | a download link | `GET …/attempts/{att}/logs?raw=true` |
| no access | without the `transcripts` capability the page states that access is missing | `API005` |

## Empty states

Every place that can be empty says what is missing and offers the one way to fill it: a copyable command where the way is the CLI, a button or a link where it is the console. The sentences are the console's English; `{ns}` and `{q}` stand for the namespace and the search text.

| place | sentence | action |
|---|---|---|
| Graphs, namespace without graphs | No graphs in {ns} yet. | the command `kora deploy <graph.ir.yaml>` to copy, with the namespace flag of the CLI |
| Graphs, search without a match | No graph named like "{q}". | clear the search |
| graph page, runs list of a graph without runs | No runs yet. | the Run button in the graph header |
| graph page, node panel of a node not started | Not started. | none |
| graph page, artifacts of an attempt without artifacts | No artifacts yet. | none |
| graph page, timeline before the first event | No events yet. | none |
| Approvals, nothing waiting | Nothing waits for you. | the history block below |
| Approvals, no history | No responses yet. | none |
| Targets, no active target | No active targets. | the switch to all targets |
| Targets, no target at all | No targets yet; a graph that declares a target creates one on its first run. | the link to Graphs |
| target page, no accepted change | No accepted change yet. | none |
| Cost, no spend today | No spend today. | the daily limit stays shown |
| System, no worker | No workers have registered. | none; starting a worker is the platform team's documented step |
| System, no schedule | No schedules. | none |
| logs of an attempt not started | The attempt has not started. | none |

## Loading

Nothing is drawn half-ready. The frame renders at once; each region of a page holds a gray placeholder of its own size until its data arrive, so the page never jumps. For a selected run, the graph is drawn only when its pinned version's IR and run state are both present, in its final colours and with no transition. After the runs query confirms zero runs, draw the latest active version's topology in gray without waiting for a nonexistent run; the colour transition applies to state changes after the first paint only. When a person selects another run, the canvas keeps the current run until the next one is ready, then swaps. A region whose data take longer than one second shows the quiet word "loading"; nothing spins.

## After an action

Every action answers in the place where it was taken: no toasts, no modal dialogs for results or errors. While a request is in flight its control is disabled and shows the verb in progress ("approving…", "starting…", "retrying…", "cancelling…"); one request at a time per control; each new logical mutation gets one fresh idempotency key. A repeated click or transport retry of that same action reuses the same key and request body; a different body is a new action. Recovery of an unresolved action across reload or reauthentication remains an [open question](../../architecture/detailed.md#open-implementation-questions).

| action | on success | on refusal |
|---|---|---|
| approve or reject | the pinned bar becomes one line, "approved by you at <time> · commit <sha>" (or "rejected …", or, when the response status is `failed`, "response received, but `approve_when` could not be evaluated" with the error kind and field, never shown as approved), the form goes, the facts and the narrative stay as history, and the line links the node that continues; the page stays | the structured error under the bar with its code and message; `API006` (already answered, or the request moved) reloads the request and shows its current state |
| run | accepted: select the new pending run and its pinned version; skipped: show "not started · graph overlap" with no invented run | validation errors under each field; full quota keeps the form filled and shows API002/run_backlog_full with scope, used/limit and retry guidance; no run was accepted |
| retry | the dialog closes; the node panel stays open and shows the new attempt as current in `created` after recovery settles, or recovery/capacity/target blockers and next recheck while admission waits; the nodes that clear-downstream reset turn gray | the error under the retry button; full outstanding quota states that nothing was cleared and shows used/limit; `deadline_expired` states that nothing was cleared and shows the expired scope/deadline with the existing rerun command guidance; a node that is no longer failed reloads the node view |
| cancel | the run line (or the node's second line) reads "cancelling" until the API reports the terminal state, then `cancelled` with retained scopes/recovery reason when physical closure remains unknown; the panel stays open | the error under the cancel control; a run or node already finished reloads the view |

A local client failure is shown under the control with its fixed code/message and API-delivery outcome from [API conventions](../../specs/api-and-agent-surface.md#client-and-local-validation-failures), without an invented server request ID. A lost response with outcome `unknown` says that the action's result is unknown; it is not a domain refusal or proof that nothing happened. Preserve that unresolved logical action's key/body while the page remains active, read the authoritative run/node/approval state or recover its accepted receipt through the operation's existing contract, and never resend automatically. An explicit retry of that same logical action retains its key/body and current authorization; a deliberate new action gets a new key only after its uncertain predecessor is resolved. A retry operation's uncertain result is read before repeating it. A proven `not_sent` result describes API delivery only. The control can expose inspection/recovery without presenting an uncertain mutation as a safe fresh start. Cross-reload/reauthentication persistence remains the open question above; returning from login refetches state and does not automatically replay a mutation.

## Live data

Every page with a live stream carries a quiet freshness word at the right end of its header line (the run line on the graph page): `live` while valid stream records arrive; `reconnecting` after a local connection loss while an allowed read reconnects and refetches the run before resubscribing; `stale · updated <n> min ago` when that recovery has failed for longer than a minute. A nonretryable terminal stops the subscription and marks it stale immediately. API state remains authoritative; transport loss never changes a run/node/approval's domain status or proves an action failed. When new runs are inserted outside the visible part of the runs list, a mark "n new runs" appears at the top and scrolls there on click; scroll position never moves on its own.

Dispatch a schema-validated server `stream_error` by its API code even though the stream's HTTP status was 200. API009 follows the same login transition as the ordinary API009/401 response: stop subscriptions/reconnect timers, clear the old credential's protected query/view data, remember the page address without credentials, and after `kora open` refetch with the new identity before returning there. API005 ends the affected stream and shows its forbidden state in place; discard protected cached/displayed content and do not enter an automatic reconnect loop. Permitted metadata may be shown again only through a currently authorized read. A new explicit read or credential/namespace change re-enters API authorization; no client decision grants rights. A permission denial is distinct from retained-content expiry/truncation and ordinary network loss.

Other server terminals preserve their code/message and exchange request ID. API010 and unknown nonretryable server failures stop and show the structured failure, without classifying it as a network drop. API011 read recovery follows its explicit retryability/current-authority contract. Local CLT errors remain client-origin failures with no fabricated server ID; connection/deadline/stream loss may use existing read reconnect behavior, while invalid framing/schema stops and shows the local protocol failure. Reconnection never automatically retries a mutation. Earlier complete records remain valid observations where still permitted, and a partial input tail is discarded. Tolerated additive/unknown data remains data and is never trusted markup or a control frame.

Navigation, namespace/identity change, terminal failure, cancellation and unmount release the view's streams, subscriptions, reconnect timers and query handles. No old-identity protected cache or detached reconnect loop survives a login transition. The current shell has no implemented API journeys; the console implementation issue assumes these legs of `test_follow_stream_revalidates_authority` and `test_client_distinguishes_transport_and_api_failures` before activation, alongside both complete pilots and the existing functional/browser release checks.

## Partial data

Lists load in pages and say so at their boundary ("earlier runs", "earlier events") rather than scrolling without end. Wherever a cost is shown, an amount whose completeness is below full carries the word `partial` after it, with the completeness the cost view reports on hover; a sum over several runs (the target page's cost of the search) is `partial` when any of its parts is.

## Time

Durations and ages shorter than a day are relative ("48 min", "2 d" from a day on); timestamps are absolute in the browser's timezone, with the zone named once in the user menu, and the full timestamp on hover of any relative time. "Today" in the cost figures is the namespace's day as the API computes it; the Cost page names that timezone beside its heading.

## Responsive

Two tiers. From 1280 px the graph page is laid out as drawn above. From 768 to 1279 px the runs list starts collapsed to its 48 px column of marks and the node panel becomes a sheet over the right edge of the canvas (Esc or a click outside closes it), so that the canvas keeps at least 600 px. The review page, the Graphs, Targets, Approvals, Cost and System pages, the target page, the logs and the attempt comparison are one column and work at every width down to a phone: below 768 px the top bar folds the sections into a menu behind the kora mark, every tap target is at least 44 px, and the attempt comparison stacks its two columns. Below 768 px the graph page draws no canvas: it shows the graph header, the runs list with the state strips, and the line "open on a wider window to see the graph"; a graph view for phones is deferred.

## Keyboard and screen readers

Every page works from the keyboard. On the graph page the canvas is one tab stop; inside it the arrow keys move between nodes in layout order (left to right, then top to bottom), Enter opens the node's panel, Esc closes the panel and returns the focus to the node, and the strip's segments are reached with the arrow keys inside a node. Each node is a button whose accessible name reads "<name>, <state word>, <second line>"; a strip's accessible name reads its counts per state; the runs list is a listbox whose rows read the target, the state word and the age. Focus and selection must remain distinguishable, including when both are on the same node; their geometry and colours are owned by [tokens.md](tokens.md#node-geometry-and-zoom). The node panel's element list is the textual equivalent of a strip.
