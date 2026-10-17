# Console pages, element by element

Status: design detail behind [docs/console.md](../../../docs/console.md), written 2026-10-16, revised after the design review of 2026-10-17. Every element names the API route that serves it ([api-and-agent-surface.md](../../specs/api-and-agent-surface.md) section 2); an element that needed a route change points at [ADR-0015](../../../docs/decisions/0015-web-console-design.md). States, colours and words are in [states.md](states.md); typefaces, scales, colour values and node geometry in [tokens.md](tokens.md); the pilot scenarios in [pilot-scenarios.md](pilot-scenarios.md).

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
| graph page | `/{ns}/graphs/{graph}` for the newest run; `/{ns}/graphs/{graph}/runs/{run}` for a selected run | `?node={node_path}` for the open node panel (the path percent-encoded); `expand={node_id}` repeated for each expanded fan-out; the timeline filter stays out |
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
System: no outgoing links
```

| element | content | route |
|---|---|---|
| runs list (left) | the graph's runs newest first; per row a strip of the top-level node instances' states (colour and glyph), `target_key` (or the run's parameters in brief when the graph declares no target), `started_at` relative, the trigger kind and actor; the newest run selected by default, or the newest run in the state of the traffic-light segment that opened the page; 50 rows at a time, with "earlier runs" at the bottom loading the next 50 without moving the scroll | `GET /namespaces/{ns}/runs?graph=` with the top-level node states of each run |
| graph header | always present: `graph.name`, the latest active version number, the Run button; read-only execution-run usage/cap and cap-source version from the API | `GET …/graphs/{graph}` |
| run line | under the graph header, only when a run is selected: target, state word and elapsed time, trigger and actor, tokens and USD so far, the run's execution version, `rerun_of` and its reruns; separate outstanding/execution-reserved/target-hold facts, with observed time and scope detail under disclosure | `GET …/runs/{run}`, `GET …/runs?rerun_of=`, `GET …/runs/{run}/cost` |
| graph | the topology of the selected run's pinned version, or of the latest active version when the graph has no run (drawn in gray), laid out once from the IR, left to right, by the ELK layered algorithm through its JavaScript build, with fan-out and sub-graph frames as compound nodes; expanding or collapsing a frame re-runs the layout and animates the moves over 200 ms; a state change never re-runs it; nodes as same-size rectangles; edges from `inputs.from` and `depends_on` | `GET …/graphs/{graph}/versions/{gv}`, `GET …/runs/{run}` for instance states, `GET …/runs/{run}/events?follow=true` for live changes |
| node, zoomed out | the kind icon in one corner and the state glyph ([states.md](states.md)) in the opposite corner; the rectangle's colour is the state | |
| node, zoomed in | the icon in a corner; the node name; the time-by-state line ([states.md](states.md)); "attempt n" when the instance has more than one attempt | the node instances of the run view; the wait of a waiting node (ADR-0015) |
| parallel instances | one rectangle for a declared node materialised per fan-out element; colour by the worst instance state; a strip with one segment per element in index order, each segment in its state's colour with its glyph when at least 12 px wide, a hatch for failed and a dot for waiting for a person when narrower | the run view's node instances grouped by declared node id |
| fan-out node | collapsed: one rectangle with the strip and the summary of counts per state, "n elements: a succeeded · b failed · c running · d waiting", always shown as words whatever the aggregate colour; expanded in place: a frame with the body drawn once, each body node carrying the strip; only the current `subgraph_attempt` of each element is drawn; a strip holds up to 64 segments, above that it shows the counts only and the element list in the panel is searchable | the run view's node instances with `node_path` and `subgraph_attempt` |
| node panel | in order: for a fan-out or a body node, the list of elements first, each with its index, its label from the fan-out's items when there is one, its state word, its current attempt and its reason, where selecting an element highlights its segment in every body node and scopes the rest of the panel to it; then state and reason with the `error` text; recovery details for current and historical attempts (domain/version, last observation/time, all earlier dispatcher grants, publication outcome, retained scopes, evidence provenance and permitted actions) with a fully bound operator CLI command; attempts (current expanded: harness and model from the resolved config, `strategy`, `strategy_fallback`, `started_at`, `finished_at`, tokens and USD; earlier attempts collapsed to a line with status and error class; for a fan-out element the earlier sub-graph attempts with their `else_reason`); artifacts of the current attempt with type, digest and payload link; links to the logs page and the comparison page; a human-input node: the review button on top and the request's state; a failed node: the retry button | `GET …/nodes/{node_path}`, `…/attempts[/{att}]`, `GET …/artifacts/{art}[/payload]`, `GET …/runs/{run}/cost` |
| timeline (bottom) | collapsed: "last event · when"; expanded: the run's events newest first with a filter by node; a clicked event highlights its node | `GET …/runs/{run}/events?follow=true` |
| Run button | in the graph header, reading "Run v<n>" with the latest active version; a form from the graph's `params` JSON Schema (titles, descriptions, required marks, defaults; the supported subset is string, number, integer, boolean, enum, arrays of those and objects one level deep; a field outside the subset is a JSON editor validated against its schema) whose title names the version and, when the selected run's version differs, states in one line "the selected run used v<m>; this run uses v<n>, the latest active version"; submits with a fresh idempotency key on the latest active version; the new run appears first in the list and is selected | `POST /namespaces/{ns}/graphs/{graph}/runs` |
| retry dialog | "retry <node> as <strategy from the node's policy>"; the list of node instances that clear-downstream will reset (the downstream closure of the run view); any execution/publication barrier and the statement that replacement admission waits for closure; the strategy under a details disclosure, `resume` labelled as falling back on runtimes without `supports_resume` | `POST …/nodes/{node_path}/retry` |
| cancel | in the run header (the run) and in the node panel (the node); the confirmation names the attempts requested to stop and explains retained reservations/holds until closure; the grace period comes from configuration | `POST …/runs/{run}/cancel`, `…/nodes/{node_path}/cancel` |

Target-source results (ADR-0019): the node panel labels each tentative artifact `staged`, with its normal access/availability, and does not show it as accepted success. A target identity conflict uses the existing failed/configuration-error state and shows bound/proposed keys, source artifact and recovery guidance. Scheduler-only sources expose the same node failure without an attempt row. The panel can show the fully bound existing `kora rerun <run>` command; there is no new console action. Retry preserves the key, while rerun discovers independently and cannot guarantee a requested target without graph-defined selection.

## Review page

Opens from a human-input node and from Approvals; one approval request.

| element | content | route |
|---|---|---|
| header | the bound commit, the branch and the change-request link from the bundle's `VcsRef`, the run's `target_key`, the request's deadline | `GET …/approvals/{apr}`; the deadline from the node's wait (ADR-0015) |
| facts: metrics | one table baseline → candidate → gain; every value followed by its provenance word: "measured · <command node>" or "reported by the agent · <agent node>" | `ReviewBundle.facts` |
| facts: gates | one row per gate: name, exit code, "verified" against the protected paths in force or "unprotected" | `ReviewBundle.facts` |
| facts: cost | `run_cost` per model with the weakest completeness | the request view |
| facts: change | changed paths and diff statistics; two links named by revision: "diff of <bound commit>" to the code host's page for that commit (`commit_url`, or `compare_url` where the host compares the base to the commit) and "open change request" to the request's current head (`url`); beside them the words "the facts cover <bound commit>" | `ReviewBundle.facts`, the bound `VcsRef` (ADR-0015) |
| narrative | the agent's `summary` and `rationale`, labelled "written by the agent", set in the serif typeface | `ReviewBundle.narrative` |
| form | rendered from the request's JSON Schema with the same subset as the Run form; the decision field and the submit button, which names the bound commit, in a bar pinned to the bottom of the window; the other fields at the end of the page; without the `approve` flag the bar says beside the button, before any press, that the token lacks the approve right and how it is granted (FR-12.4), and the button, still pressable, repeats it when pressed | `POST …/approvals/{apr}/respond` with `commit_sha` and `digests` as shown |
| stale response | on `API006` the page reloads the request | |
| invalidated request | a banner with `bound_commit_sha` and `observed_head_sha`, the statement that nothing can be approved on this evidence, no form, the facts and narrative kept as history, the retry button for the producing node (named by the API's `API002` hint) with `fresh_with_feedback`, a link to that node | `GET …/approvals/{apr}`, `POST …/nodes/{node_path}/retry` |
| approved then moved | both commits, the request kept `approved`, the node failed with `candidate_moved` | the request view and the node view |

## Targets

| element | content | route |
|---|---|---|
| list row | the key; the strip of the holding run's top-level node states (the newest holder under `allow`; "idle" in gray without active work); the accepted change (date and commit, or a dash); the number of deferred runs when any; the list shows active targets by default with a switch to all | `GET /namespaces/{ns}/targets?active=true`, the runs' node states |
| target page: now | all holding runs and their `target_held_since`, including terminal runs with unsettled recovery, each state word and review link; execution occupancy is separate from the hold; the deferred runs with `run.target_deferred` times | `GET /namespaces/{ns}/targets/{key}` |
| target page: accepted | the latest `approved` request: commit, change-request link, `approver`, time; the three facts "accepted · merged · verified" with their sources (the request; the `change_request_status` observation of a named run; a run-level artifact such as a confirmation metrics report) or "unknown"; the cost of the search: the sum of cost totals over the key's runs since the previous accepted change, failed and cancelled included, tokens first, USD beside, with completeness | the target view's runs, their approval requests and cost totals |
| target page: history | the key's permanently bound runs newest first in the graph page's row style, each with its cost, including failed/rejected proposals and late usage; payload expiry preserves binding origin | the target view |

## Approvals

| element | content | route |
|---|---|---|
| waiting | rows: target, graph, how long it waits and the deadline, the bound commit, `run_cost`; click opens the review page | `GET /namespaces/{ns}/approvals?status=pending`; the deadline from the node's wait (ADR-0015) |
| history | rows: target, decision and approver, time, commit; `invalidated` and `expired` with their reason | `GET /namespaces/{ns}/approvals?status=` (ADR-0015) |

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
| Workers | id, incarnation, runtime domain, version, capacity, `last_seen_at`; recovery backlog including historical grants and terminal runs with retained reservations/target holds; highlighted when silent longer than the lease period; "holds": the attempts the worker holds, each linking to the node panel of its run (ADR-0015) | `GET /workers` |
| Schedules | per graph: trigger kind, spec, `next_fire_at`, paused; last accepted/skipped/rejected occurrence, reason, quota snapshot and time | `GET …/graphs/{graph}/triggers` |
| Presets and artifact types | the lists; a body on click | `GET /namespaces/{ns}/presets`, `GET /namespaces/{ns}/artifact-types/{name}` |
| Policy | namespace policy read-only, including outstanding accepted-run usage/limit (default 1000), observed time and explicit over-limit state after lowering | `GET /namespaces/{ns}` |

## Attempt comparison

Opens from the node panel; two attempts of one node, the counterpart as the route defaults it.

| section | left and right | route |
|---|---|---|
| outcome | status and error class; intended and observed `strategy`/`strategy_fallback`; retained `Feedback` with author and association when authorized, otherwise explicit content access/availability | `GET …/attempts/{att}/diff?against=` |
| outputs | per declared output: structured outputs as the changed fields from the JSON Patch; `VcsRef` as branch, commit, diff statistics, change-request link | the same |
| cost and events | usage per model with completeness; event counts | the same |
| configuration and versions | saved exact input IDs/selectors and request/config digests; permitted retained config differences and saved effective policy; observed `harness_version`, `image_digest`, checkout commit and `price_table_id`, with unknown facts explicit | the same |

Identical available rows are muted. Missing/expired/not-recorded/unknown values are labelled explicitly, never treated as equal empty values. Diff reads immutable attempt records and stored artifact projections, never current pointers or replacement plugins (ADR-0018). Prompt/feedback and equivalent config/diff projections require namespace `transcripts` access; a forbidden value is distinct from an expired one. A retry requiring unavailable inputs displays the API's structured conflict and source IDs, without adding a console action.

## Logs

Opens from the node panel; one attempt.

| element | content | route |
|---|---|---|
| transcript | the normalised events in order: `turn` and agent messages as text in the serif typeface; `tool_call` and `tool_result` as one collapsible row (tool, arguments in brief, duration, outcome) expanding to the full arguments and result, truncation markers kept; `permission_denied` and `usage` as rows of their own colour; `result` and `error` at the bottom; a page holds 1,000 events and its boundary is a visible line "earlier events" that loads the previous page above the reader without moving their position | `GET …/attempts/{att}/logs?follow=true`, paged at 1,000 events |
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

Nothing is drawn half-ready. The frame renders at once; each region of a page holds a gray placeholder of its own size until its data arrive, so the page never jumps. The graph is drawn only when both the version's IR and the run are present, in its final colours and with no transition; the colour transition applies to state changes after the first paint only. When a person selects another run, the canvas keeps the current run until the next one is ready, then swaps. A region whose data take longer than one second shows the quiet word "loading"; nothing spins.

## After an action

Every action answers in the place where it was taken: no toasts, no modal dialogs for results or errors. While a request is in flight its control is disabled and shows the verb in progress ("approving…", "starting…", "retrying…", "cancelling…"); one request at a time per control; a mutation carries a fresh idempotency key, so a repeated click or a retried request cannot act twice.

| action | on success | on refusal |
|---|---|---|
| approve or reject | the pinned bar becomes one line, "approved by you at <time> · commit <sha>" (or "rejected …"), the form goes, the facts and the narrative stay as history, and the line links the node that continues; the page stays | the structured error under the bar with its code and message; `API006` (already answered, or the request moved) reloads the request and shows its current state |
| run | accepted: select the new pending run and its pinned version; skipped: show "not started · graph overlap" with no invented run | validation errors under each field; full quota keeps the form filled and shows API002/run_backlog_full with scope, used/limit and retry guidance; no run was accepted |
| retry | the dialog closes; the node panel stays open and shows the new attempt as current in `created` after recovery settles, or recovery/capacity/target blockers and next recheck while admission waits; the nodes that clear-downstream reset turn gray | the error under the retry button; full outstanding quota states that nothing was cleared and shows used/limit; a node that is no longer failed reloads the node view |
| cancel | the run line (or the node's second line) reads "cancelling" until the API reports the terminal state, then `cancelled` with retained scopes/recovery reason when physical closure remains unknown; the panel stays open | the error under the cancel control; a run or node already finished reloads the view |

A network failure is shown like a refusal, with the words "no connection", and the control becomes active again.

## Live data

Every page with a live stream carries a quiet freshness word at the right end of its header line (the run line on the graph page): `live` while the stream is connected; `reconnecting` after a drop, while the console reconnects on its own and, on reconnection, refetches the run and resubscribes; `stale · updated <n> min ago` when reconnection has failed for longer than a minute. A stale page stays readable, its actions stay available, and every action revalidates the state it acts on by the API's own rules (an answered request is refused with `API006`, a node no longer failed refuses a retry). A `401` on any request sends the person to the login page and, after `kora open`, back to the address they were on. When new runs are inserted outside the visible part of the runs list, a mark "n new runs" appears at the top of the list and scrolls there on click; the scroll position never moves on its own.

## Partial data

Lists load in pages and say so at their boundary ("earlier runs", "earlier events") rather than scrolling without end. Wherever a cost is shown, an amount whose completeness is below full carries the word `partial` after it, with the completeness the cost view reports on hover; a sum over several runs (the target page's cost of the search) is `partial` when any of its parts is.

## Time

Durations and ages shorter than a day are relative ("48 min", "2 d" from a day on); timestamps are absolute in the browser's timezone, with the zone named once in the user menu, and the full timestamp on hover of any relative time. "Today" in the cost figures is the namespace's day as the API computes it; the Cost page names that timezone beside its heading.

## Responsive

Two tiers. From 1280 px the graph page is laid out as drawn above. From 768 to 1279 px the runs list starts collapsed to its 48 px column of marks and the node panel becomes a sheet over the right edge of the canvas (Esc or a click outside closes it), so that the canvas keeps at least 600 px. The review page, the Graphs, Targets, Approvals, Cost and System pages, the target page, the logs and the attempt comparison are one column and work at every width down to a phone: below 768 px the top bar folds the sections into a menu behind the kora mark, every tap target is at least 44 px, and the attempt comparison stacks its two columns. Below 768 px the graph page draws no canvas: it shows the graph header, the runs list with the state strips, and the line "open on a wider window to see the graph"; a graph view for phones is deferred.

## Keyboard and screen readers

Every page works from the keyboard. On the graph page the canvas is one tab stop; inside it the arrow keys move between nodes in layout order (left to right, then top to bottom), Enter opens the node's panel, Esc closes the panel and returns the focus to the node, and the strip's segments are reached with the arrow keys inside a node. Each node is a button whose accessible name reads "<name>, <state word>, <second line>"; a strip's accessible name reads its counts per state; the runs list is a listbox whose rows read the target, the state word and the age. The focus ring is 2 px in the accent colour and visible on every colour in both themes. The node panel's element list is the textual equivalent of a strip.
