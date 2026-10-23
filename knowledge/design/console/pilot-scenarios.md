# Pilot scenarios for the console

Status: design detail behind [docs/console.md](../../../docs/console.md), written 2026-10-16, revised after the design review of 2026-10-17. The two scenarios are the product-acceptance checks the architecture critique set for the console ([architecture-critique-2026-10-16.md](../../reviews/architecture-critique-2026-10-16.md), "Priorities for the user-experience design"; issue #23). Each step names the view that answers it and the API route behind the view; where the route did not expose the data before the console design, the step names the change in [ADR-0015](../../../docs/decisions/0015-web-console-design.md) that closed it. The scenarios run on the canonical SQL optimisation graph ([examples/sql-optimization.ir.yaml](../../specs/examples/sql-optimization.ir.yaml)).

## Bounded read acceptance

Both pilots use [the bounded-read contract](../../specs/bounded-read-projections.md) and remain required when the console activates. Every history is paged with exact incomplete markers, retained within one displayed page plus replacement. Approval facts/narrative use an explicitly selected complete context; forms and bound-digest maps must be fully verified before submission. Branch/change-request links require the exact complete bound VcsRef source, independently of summary availability. Graph colours/counts use a complete pinned scope, not run-list samples or event reconstruction. Recovery/worker details follow scoped paged attempt/dispatcher references. Target search cost is the API's named approval cohort, never a console sum.

Extend both journeys with 1,000-node and near-cap VcsRef metadata traces proving no body prefetch; retained source with expired/missing summary; cross-namespace and transcripts denial; prefix/render-limit handling; history page replacement; more than eight expanded scopes including Back restoration; and hot-scope invalidations with fair progress. Hang headers, stall a body and repeat 15-second total refresh expiry: owned fetch/reader/tasks settle before next-scope dispatch, old data is labelled stale, late results cannot install or resurrect timers, and unresolved cleanup stays owned. Navigate or change identity/rights during each selected read; no old protected result or action appears under a new header. Retry/cancel previews show exact observed counts and paged members, support refusal without disabling authorized mutation, and POST recomputes effects. These are functional/resource cases; excluded numerical browser benchmarks remain excluded. Dedicated console/generated-client issues name activation and real evidence before code.

Include two parents using one reusable subgraph body with the same nested fan-out ID. Expanding either produces a distinct full declared scope-path array in the URL. A copied address and Back restore the exact parent/body scope in the selected pinned graph version without substituting the other use, while retaining the eight-scope bound and late-generation rejection.

## Scenario 1: an engineer without database access explains a run from the console

Setting: the graph `sql_optimization` in the engineer's namespace has three runs in its current summary. A run started on Friday for the target `etl.orders_agg` produced a change request and waits for review. A run started on Saturday for `etl.user_profile` failed. A run started today for `etl.daily_sales` is measuring the baseline. A colleague pushed a commit to the Friday candidate's branch on Monday morning. The sections below are snapshots at two moments, named in their headings: before kora observes the Monday push, when the request is still pending and `final_pr` amber, and after the invalidation.

### Why the change is ready for approval (before the push is observed)

1. Open Approvals; the waiting block lists the Friday request with its target, how long it waits, the deadline, the bound commit and the run's cost. View: Approvals. Route: `GET /namespaces/{ns}/approvals?status=pending`; the deadline from the node's wait (ADR-0015).
2. Open the request; the header names the commit, the branch, the change request and the target. View: review page. Route: `GET …/approvals/{apr}`.
3. Read the metrics table: baseline and candidate values each marked "measured · baseline" and "measured · dev_run", the gain, and the review verdict marked "reported by the agent · review". View: review page, facts. Route: explicitly selected complete context artifact, `ReviewBundle.facts`.
4. Read the gates: the equivalence check with exit code 0, "verified" against the protected paths; the cost with its completeness. View: review page, facts.
5. Read the agent's summary and rationale, set apart. View: review page, narrative.
6. Explain: the candidate is equivalent (gate), faster by the measured gain (facts), and the agent's reasoning is a separate block that binds nothing; the approval would bind the named commit and these facts. Checkable: every number the engineer quotes carries a provenance word.

### Why an old approval lapsed (after the invalidation)

1. Open Approvals; the history block lists the Friday request as `invalidated` with the reason "candidate moved". View: Approvals. Route: `GET /namespaces/{ns}/approvals?status=invalidated` (ADR-0015: status filter).
2. Open it; the banner shows the bound commit and the observed head, states that nothing can be approved on this evidence, and names the node to retry. View: review page. Route: `GET …/approvals/{apr}` (`bound_commit_sha`, `observed_head_sha`).
3. On the graph page the node `final_pr` is red with "candidate moved"; the timeline shows `approval.invalidated` with the observed head and `node.failed` with reason `candidate_moved`. View: graph page. Route: `GET …/runs/{run}/view?scope=root`, paged events with explicitly selected payload detail.
4. Explain: a push changed the branch after the evidence was gathered; the request covered one revision and cannot cover another; a new request appears after the producing node is retried. Checkable: the two commits are both on screen.

### Where the work is now (before the push is observed)

1. Open Graphs; the row of `sql_optimization` is at the top of the list and its traffic light reads "1 waiting for you · 1 failed · 1 running". View: Graphs. Route: the graphs list summary (ADR-0015).
2. Click "1 waiting for you" in the row; the graph page opens with the Friday run selected, the runs list showing three bounded rows and the selected complete compact scope showing its strip: `final_pr` is amber, everything before it green. View: graph page. Route: `GET /namespaces/{ns}/runs?graph=`, `GET …/runs/{run}/view?scope=root`.
3. Open Targets; `etl.orders_agg` is held by the Friday run since Friday, `etl.daily_sales` has a running holder. Switch to all targets to find the settled failed Saturday run under `etl.user_profile`; it is not an active holder. View: Targets. Routes: the active-target list and the unfiltered target/history reads.
4. Open the target page of `etl.orders_agg`; the "now" block names the holding run and the review link; the "accepted" block shows the previous accepted change with its three facts; the history lists the runs. View: target page. Route: `GET /namespaces/{ns}/targets/{key}`.

### Why it waits (before the push is observed)

1. The Friday run: `final_pr` second line "approval · 2 days of 3". View: graph page. Route: the node's wait (ADR-0015: kind `approval`, `wake_at`).
2. The Saturday run after a retry: `baseline` second line "in queue · 12 min" while workers are busy. View: graph page. Route: the node instance in `queued` and its attempt's `created` time.
3. In a separate target-admission fixture, a new run discovers `etl.orders_agg`, the same key held by Friday's run. Its `find_target` has completed and bound the output-derived target; `baseline` waits under `defer` with "waiting for admission · target held by run <Friday run>". Today's `etl.daily_sales` run in the three-run summary is not blocked by that different key. View: graph page. Route: the node's wait (ADR-0015: kind `admission`, `reason`), the event `run.target_deferred`.
4. A node in retry back-off: "retry in 2 min · attempt 2 of 3". Route: the node's wait (ADR-0015: kind `retry_backoff`, `wake_at`).
5. A node waiting for the deploy webhook: "waiting for deploy.finished". Route: the node's wait (ADR-0015: kind `event`, the subscription name).
6. A node under the daily budget: "waiting for admission · daily budget reached", and Cost shows "admission blocked". Route: the node's wait (ADR-0015: reason `daily_budget`), `GET /namespaces/{ns}/cost/daily`.

## Scenario 2: an operator resolves a lost worker and an ambiguous publish through the public surface

Setting: the worker `w-docker-03` on the `docker` runtime stopped sending heartbeats twenty minutes ago while it held an attempt of `implement` for one hypothesis and was finalising another attempt whose session had ended and whose branch had been pushed but whose `VcsRef` had not been uploaded.

### The lost worker

1. Open System; Workers lists a worker silent longer than the lease. Its recovery backlog still includes the original attempt after two lease periods and after logical run cancellation. Routes: paged `GET /workers`, scoped `GET /namespaces/{ns}/runs?worker=&worker_attempts=recovery` and matching run-wide attempt pages.
2. Follow the attempt link. The node panel says "execution unknown", original domain/version, last observation/time, pending dispatcher grants and retained scopes; there is no replacement and retry counts are unchanged. Routes: node/attempt reads. Cost is last-known/partial rather than zero/complete.
3. Read `attempt.execution_unknown` in the timeline and the acquired target hold in Targets, even when the run is cancelled. A manual retry records intent but starts no new attempt; a linked rerun returns `API002`. Routes: run events, target projection, retry/rerun operations.
4. Copy the fully bound operator `kora reconcile` command. With namespace access and `platform`, request `observe` or `stop` with expected version/domain and idempotency key; the API returns persisted intent. A recovery worker observes/adopts the original live session read-only while an older dispatcher remains unresolved, or retires it and collects closure evidence. No runtime call happens in the API transaction.
5. If the domain never returns, establish actual host termination/decommissioning and settlement of every old dispatcher/publication operation outside kora; record `confirm_stopped` with reason and evidence reference/attestations. The event names the operator and labels the evidence as human attestation. A stale version/domain is `API002`; incomplete evidence is `API003`; no permission is `API005`.
6. Verify that retirement alone does not release capacity when an earlier worker operation remains unresolved. Once all closure evidence is applied, reservations release once; a terminal run's acquired target hold clears, or a running run retains its ordinary hold. Only confirmed infrastructure loss follows retry policy with a distinct attempt branch; a cancelled attempt does not retry. Console mutations remain the same four; operator reconciliation stays CLI/MCP.

### The ambiguous publish

1. Open the node panel of the attempt that was finalising; its state is `running` with runtime phase `publication` and the attempt's events show `vcs.publication_prepared` with the trusted branch and inspected commit, while `vcs.publish` appears only after reconciliation confirms publication. View: node panel. Route: `…/attempts/{att}`, `GET …/runs/{run}/events` (supervisor phase events mark inspection, awaiting authorization and publication; `attempt.finishing` follows the reconciled runtime outcome).
2. Before the inherited deadline, and after all earlier dispatcher grants and their in-flight operations are closed, when the adopting worker reconciles the stored prepared revision through `finalize`, the timeline shows `vcs.publish_repeated` with the branch, and then the `VcsRef` artifact appears with the change-request URL from the search before create. View: timeline, node panel artifacts. Route: paged events, artifact metadata and explicitly selected complete VcsRef source (ADR-0015: `vcs.publish_repeated`).
3. Repeat after expiry: recovery may observe the existing remote outcome and close old operations, but cannot authorize another push/change request, promote current output or restore success, even with a valid prepared checkpoint (ADR-0022). Deadline expiry alone does not prove termination or release retained capacity.
4. When the required sealed input or prepared-result checkpoint was lost or corrupt, the attempt remains non-terminal with a recovery reason until runtime termination and dispatch/publication closure are established, then the node is red with "infrastructure · branch <deterministic name> left on the code host"; the operator opens the branch on the code host from that name. View: graph page, node panel. Route: the attempt's `error` (ADR-0015: the branch named in the error).
5. Decide: request retry, with admission blocked until all old recovery is settled (the next attempt has its own deterministic branch; reconciliation within the original attempt reuses its prepared revision and searches for its existing change request), or cancel. An expired physical node-attempt deadline allows an otherwise eligible new attempt; an expired run or enclosing subgraph refuses retry with `API002`, `deadline_expired`, before clearing, and requires a new run after recovery barriers permit it. Cleanup of a branch is the graph author's command node, never the console's. View: node panel. Route: `POST …/nodes/{node_path}/retry`.

Both scenarios are satisfied by the views in [pages.md](pages.md) and the routes of [api-and-agent-surface.md](../../specs/api-and-agent-surface.md) section 2 as amended by ADR-0015; no step depends on database access or on `kora events` in a terminal.

## API error and stream integration cases

These cases are pending the generated-client/console implementation issue under #135; they do not establish runnable product journeys. Apply them to each pilot's live run/log views and existing mutation controls, using real API/client integration and configured protocol fakes, injected barriers/time and both metadata databases.

- Deliver API009 as an HTTP-200 server stream terminal: the view stops/reclaims subscriptions and timers, clears old protected data, logs in and refetches the remembered address; no mutation is replayed.
- Deliver API005 after namespace/transcript rights are removed: the affected view shows forbidden, clears protected data and never loops through reconnect. A separate authorized metadata read preserves permitted metadata; expiry/truncation remains a different state.
- Exercise ordinary API errors, API010/API011 terminals and unknown additive API codes, then local connection/deadline/stream loss versus malformed frames. Code, origin and safe exchange/header correlation stay distinct; earlier complete records and incomplete tails follow the API contract.
- Give an older adapter a permitted new optional record/envelope field or documented extension variant: it remains bounded data, never CLT003 solely for that addition, trusted markup or a control frame. Malformed required fields and mixed/reserved control discriminants fail safely.
- Accept a mutation durably, then lose its response: show unknown result, preserve the in-page logical key/body, read/recover authority before explicit retry and never create a fresh duplicate start. Test repeated clicks, current authorization and exact accepted receipt recovery; cross-reload/reauthentication key persistence remains its separate open question.
- Repeatedly navigate, switch namespace/identity, deny/reconnect, cancel and unmount: all view-owned streams, timers, subscriptions and query handles settle. No protected old-identity cache or detached loop survives. Preserve keyboard/focus and in-place error feedback; no additional console mutation is introduced.
