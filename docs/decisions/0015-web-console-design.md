# ADR-0015: The web console's design: the graph as the primary view, four actions in MVP, one state vocabulary, and the data the API exposes for it

- Status: accepted, 2026-10-16
- Reviewed: by the project owner, 2026-10-16; publication-boundary amendment dated 2026-10-17 pending owner review
- Superseded in part by: [ADR-0016](0016-trusted-execution-and-publication.md) (publication placement and phases); amended and reviewed again after the design review of 2026-10-17
- Deciders: project owner, after the console design dialogue of roadmap step 2 (issue #23)
- Absorbs: requirements decision 27
- Related: [0002-ai-first-api-and-thin-clients.md](0002-ai-first-api-and-thin-clients.md), [0007-attempt-model-and-retries.md](0007-attempt-model-and-retries.md), [0008-content-bound-approvals.md](0008-content-bound-approvals.md), [0010-cost-accounting-and-limits.md](0010-cost-accounting-and-limits.md), [0011-namespaces-and-personal-tokens.md](0011-namespaces-and-personal-tokens.md), [0014-target-identity.md](0014-target-identity.md)
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

## Context

Requirements decision 21 made the web console a full console in the MVP, held to the reference-grade bar of NFR-13.1, and the roadmap put its design before its implementation. The architecture critique of 2026-10-16 asked the design to settle the entities and the failure states before the screens: an attention queue, a target page, a review-revision page, the run graph with instances, attempts and superseded outputs apart, attempt comparison, and two product-acceptance scenarios (an engineer without database access explains a run; an operator resolves a lost worker and an ambiguous publish through the public surface). [ADR-0014](0014-target-identity.md) gave the target page its footing.

The specification of the console before this record ([api-and-agent-surface.md](../../knowledge/specs/api-and-agent-surface.md) section 6) listed views over existing routes and one mutation, the approval response. Walking the two scenarios over those routes found data the console needs and no route exposed: a per-graph summary for the entry screen; the wait a node is in (its kind, deadline, subscription and, for an admission wait, the reason); the worker, lease and heartbeat of an attempt and the count of infrastructure retries; the history of approval requests; and the collection phase after a session ends, where a publish may be repeated on an unknown outcome.

The design itself, page by page, is recorded in [docs/console.md](../console.md) with the element lists in [knowledge/design/console/](../../knowledge/design/console/pages.md). This record holds the decisions that change the console's scope, its vocabulary or the API.

## Decision

**The graph is the primary view and the list of graphs is the entry.** The console is designed for the engineer who owns targets. It opens on the graphs of one namespace as a list of rows with aligned columns, each row of five elements (a traffic light over the active runs' stop states, name, one-line description, declared node count, tokens spent today), the graphs waiting for a person first, and the graph page shows one run's graph with the runs of the graph beside it. The console is not an attention queue; the attention states of the critique are carried by node colours, the node's second line and the Approvals section. The pages are composed from public routes and the console adds no route of its own ([ADR-0002](0002-ai-first-api-and-thin-clients.md)).

**Four actions in the MVP.** The console performs the three human touchpoints of the canonical use case and nothing else: the approval response; triggering a run with parameters from a form rendered from the graph's parameter schema, on the latest active version; retrying a failed node through a dialog that states the node's strategy and lists what clear-downstream resets; cancelling a run or a node. Graph deployment and every operation behind the `platform` flag stay in the CLI and the MCP tools. Each action is the API operation the CLI and the MCP tools call, under the cookie session's CSRF and `Origin` rules of section 6.

**One state vocabulary.** Five colours carry every state (gray not started, skipped, upstream failed, cancelled; blue running; amber waiting for a person; green succeeded; red failed), and amber means the person's move only: a timer, an event, a sensor poll, a retry back-off, the queue for a worker and an admission wait are blue with their reason in words. The console names states with the API's words plus the one phrase `waiting for you` for the amber group, so that the console, the CLI, the MCP tools and the documentation describe a run in one vocabulary; error classes are words in the node's second line, never colours. The full mapping is [knowledge/design/console/states.md](../../knowledge/design/console/states.md).

**What the API exposes for the console** (all read projections over existing rows, computed at request time, except the two recorded fields below):

- the graphs list carries a per-graph summary: the latest active version's declared top-level node count; the counts of the graph's runs in `running` or `failed` split into runs whose only live work waits for a person, runs in `failed` and runs running otherwise; the tokens and USD estimate of the graph's attempts started today in the namespace's timezone, with the weakest completeness;
- the node view carries the instance's current wait when it has one: `kind`, `wake_at`, the subscription's event name or poll spec, and for an `admission` wait the `reason` that the admission pass now records on the wait row: `concurrency_limit` with its scope, `daily_budget`, or `target_held` with the holding run;
- the attempt view carries `worker_id`, `lease_until`, `last_heartbeat_at`, `desired_state` and `infrastructure_retries`, a new field counting the resubmissions under the runtime's `infrastructure_retry` policy that preceded the attempt;
- the approvals list accepts `status=` with any request status, `pending` by default, so that the history of responses can be listed; its rows carry the run's `target_key` and the wait's `wake_at` as the deadline;
- `VcsRef` carries `commit_url` and, where the host has one, `compare_url` beside `url`, added by `open_change_request` ([protocols.md](../../knowledge/specs/protocols.md) section 4), so that the review page links the revision the facts cover;
- the workers list carries, per worker, the attempts it holds (`run_id`, `node_path`, `attempt_id`, `lease_until`), a read projection over `attempt.worker_id`, so that System links a silent worker to the runs it affects;
- runtime publication progress is visible while the attempt is `running`: supervisor phases identify sealing, inspection, awaiting authorization and publication (ADR-0016). Reconciliation of an unknown outcome emits `vcs.publish_repeated` with the branch and exact prepared revision. `attempt.finishing` follows the reconciled runtime outcome and marks final artifact validation/storage. Infrastructure failure after a push names the branch left on the code host.

**The target page builds on ADR-0014.** It reads the target projection and claims only what the projection claims: *accepted* from the latest approved request, *merged* from the change-request-status sensor of a run that waits for it, *verified* from a run-level artifact; where no node of the graph reports a fact the page says "unknown". The cost it shows beside the accepted change is the cost of the whole search: the sum over every run of the key since the previous accepted change, failed and cancelled runs included, summed by the console from the projection's per-run totals.

**The diff stays on the code host.** The review page shows the changed paths and the diff statistics from the bundle's facts and links the bound commit's page on the code host (`VcsRef.commit_url`, or `compare_url` where the host compares the base to the commit) under the revision's name, with the change request's current head as a second link, so that the viewer shows the revision the facts cover; kora stores no diff in the MVP.

## Alternatives considered

- **An attention queue as the first screen.** Rejected by the owner: the engineer looks at the graph as an Airflow operator looks at a DAG; the nodes themselves say what finished, what failed and what waits. The six attention states are carried by node colours, second lines and the Approvals section instead.
- **dagre for the graph layout.** Rejected in the design review of 2026-10-17: it has no compound nodes, so the expanded fan-out and sub-graph frames would be laid out apart and placed by the console's own code; the ELK layered algorithm lays out frames as compound nodes and the console only chooses left to right.
- **A grid of graph cards as the entry.** Rejected in the design review of 2026-10-17: graphs are comparable records, and a card grid makes the traffic lights and the spend of twenty graphs impossible to compare at a glance and turns a namespace of fifty graphs into a scroll; aligned rows compare by column, sort by who waits, and hold hundreds of graphs, as the DAG list of Airflow does.
- **The approval response as the console's only mutation** (the specification until this record). Rejected: two of the three human touchpoints, triggering a run and handling an error, would stay in the terminal, and the stale-revision page could only print a command to copy.
- **Every engineer operation in the console** (adding a rerun on another graph version) and **every platform write** (schedule pause, presets, policy). Rejected: a rerun with a version choice is an author's action with a terminal open; platform writes are JSON editing forms the MVP does not need.
- **A sixth colour for a person's wait.** Rejected: amber carries that meaning once every other wait is blue; five colours stay legible without a legend.
- **One colour per internal state** (Airflow 2.x) and **three colours** (GitHub Actions). Rejected: the first needs a legend, the second dissolves "waits for a person" into "in progress".
- **Human words for states** (`done`, `not started`). Rejected: a person and an agent reading `kora runs --json` would describe one run in two vocabularies.
- **Storing the diff as an artifact** or **fetching it from the code host at page load**. Deferred: the code host has the better viewer, the approval binds the commit, and a stored diff needs retention and size rules of its own.
- **A namespace-wide runs section.** Rejected: runs live on the graph page and the target page; a cross-graph list duplicates them for the engineer and serves the operator, who has the CLI.
- **Accepting the data gaps until after the pilot.** Rejected: both pilot scenarios would fall back to database access or `kora events` in a terminal, which is what they must prove unnecessary.

## Consequences

- `api-and-agent-surface.md` section 6 describes the sections, pages and four actions; sections 2, 3 and 4 gain the summary, the wait, the attempt fields and the approvals status filter; the parity checklist is unchanged because no operation is added, only fields and a filter.
- `data-model.md` gains `wait.reason`, `attempt.infrastructure_retries` and the event `vcs.publish_repeated`; `scheduler.md` records the admission reason and writes the repeated-publish event.
- The graph is laid out left to right by the ELK layered algorithm through its JavaScript build, with fan-out and sub-graph frames as compound nodes; the dependency and its reason are recorded in the [stack reference](../../knowledge/architecture/detailed.md#stack-per-component).
- The per-graph summary and the runs list with node states are one query each; the implementation measures them at hundreds of runs per graph (NFR-1).
- The console's pages are reviewed against NFR-13.1 in both themes before release; the design review covers the five colours in light and dark.
- `knowledge/design/` is a new folder for the detailed design of a user-facing surface behind a page in `docs/`; it holds element lists and scenarios, never decisions.

## Where it is applied

- [docs/console.md](../console.md); [knowledge/design/console/pages.md](../../knowledge/design/console/pages.md), [states.md](../../knowledge/design/console/states.md), [tokens.md](../../knowledge/design/console/tokens.md), [pilot-scenarios.md](../../knowledge/design/console/pilot-scenarios.md); [knowledge/research/orchestrator-consoles.md](../../knowledge/research/orchestrator-consoles.md)
- [api-and-agent-surface.md §2](../../knowledge/specs/api-and-agent-surface.md) (graphs summary, node wait, attempt fields, approvals status filter), [§3](../../knowledge/specs/api-and-agent-surface.md) (`kora approvals --status`), [§4](../../knowledge/specs/api-and-agent-surface.md) (`kora_approvals`), [§6](../../knowledge/specs/api-and-agent-surface.md) (the console)
- [data-model.md §2](../../knowledge/specs/data-model.md) (`wait.reason`, `attempt.infrastructure_retries`, `vcs.publish_repeated`)
- [scheduler.md §3](../../knowledge/specs/scheduler.md) (the repeated publish event, the branch named on an infrastructure failure), [scheduler.md §2](../../knowledge/specs/scheduler.md) (the admission reason)
- [stack reference](../../knowledge/architecture/detailed.md#stack-per-component) (Frontend row)
- [FR-7.3](../requirements/functional.md); [functional.md §13, decision 27](../requirements/functional.md); [NFR-13.1](../requirements/non-functional.md); [roadmap.md](../roadmap.md) step 2
