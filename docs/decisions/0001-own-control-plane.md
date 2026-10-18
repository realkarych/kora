# ADR-0001: Own control plane, with a Temporal-backed execution backend kept possible

- Status: accepted, 2026-10-15; amended 2026-10-16 (factual corrections to the engine analysis and a dated DBOS note; [architecture critique of 2026-10-16, corrections to the competitor analysis](../../knowledge/reviews/architecture-critique-2026-10-16.md#corrections-to-the-competitor-analysis) and [how to decide about the own scheduler](../../knowledge/reviews/architecture-critique-2026-10-16.md#how-to-decide-about-the-own-scheduler), issue #22)
- Reviewed: by the project owner, 2026-10-16
- Deciders: project owner, after the desk analysis of existing engines and the design review of 2026-10-15
- Absorbs: DD-7, requirements decisions 6 and 12, NFR-5.5
- Related: ADR-0003, ADR-0007, ADR-0008, ADR-0013
- Acceptance-location amendment dated 2026-10-18: the checks adopted by this record now live in the scheduler specification; scope approved under issue #95, wording pending owner review.
- Navigation amendment dated 2026-10-17: scope and implementation plan approved under [issue #76](https://github.com/realkarych/kora/issues/76); wording pending line-by-line owner review.

- Evidence-consolidation amendment dated 2026-10-18: scope approved under issue #95; wording pending owner review.

## Context

kora runs graphs whose nodes are whole coding-agent sessions, and the requirements ask for a control plane from day one: scheduler, metadata database, HTTP API, workers (FR-6.1). Writing a scheduler is expensive, and mature engines already provide retries, durable waits, cron and human-in-the-loop steps. The main alternative was "an existing engine plus a session adapter": one coding session as one Temporal activity, Kestra task or Airflow task, with the engine owning retries, waits and approvals. The positioning document recorded this as the hypothesis "own scheduler is necessary" and required a test before scope freeze (requirements decision 6).

The test was a desk analysis of Temporal, Kestra and Airflow against kora's requirements, modelling kora as a thin adapter on each ([verdict](../../knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md)); no prototype was built. The reviewed thin-adapter models for Kestra and Airflow fail the test: both record task-run attempts, but those records do not provide kora's `previous_attempt` artifact inputs and lineage; content-bound approval requires kora-owned validation and authority beyond their task-instance pause/resume operations; neither has a production standalone mode; Airflow's scheduler reads a serialised DAG that a DAG processor parses from Python files, so a YAML graph would have to be lowered into generated Python rather than being what runs, and Kestra's plugins are Java only. Temporal is viable: it provides durable scheduling primitives and its Update validators can express content-bound approvals, but its activities are at-least-once, so a lost completion re-runs the activity and submission to a runtime still needs kora's durable idempotency key and authoritative lookup; it also forces a mandatory deployment with no single-process mode, a second source of truth beside kora's database and a determinism discipline on kora's interpreter. Every engine leaves the same engine-neutral work: DSL and IR, artifact model with lineage, harness adapters, protocols, cost ledger. The hypothesis was partly confirmed: an own scheduler is not strictly necessary, but no engine removes kora's state model.

Amendment of 2026-10-16. The [architecture critique](../../knowledge/reviews/architecture-critique-2026-10-16.md#corrections-to-the-competitor-analysis) found factual errors in the paragraph above (Temporal credited with at-most-once submission, Airflow's scheduler said to consume Python files, Kestra and Airflow said to own no attempt object) and noted that DBOS, a durable-execution library embedded in the application over PostgreSQL or SQLite, was missing from the analysis. The paragraph is corrected and dated DBOS evidence is retained in the consolidated analysis ([DBOS evidence](../../knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md#dbos)). DBOS is the engine closest to kora's deployment shape; it would replace the scheduler's passes, not the state model, the runtime protocol or the worker boundary, and its recovery across several processes depends on a paid control plane or on coordination kora writes itself. The decision stands. No comparative prototype is started before the first vertical slice: the slice is the prototype of the SQL scheduler, the [ten fault checks in scheduler section 18](../../knowledge/specs/scheduler.md#18-first-vertical-slice-acceptance) are its acceptance list, and the DBOS comparison is re-evaluated when the slice has data (issue #22).

## Decision

kora keeps its own control plane for the MVP (requirements decision 12): a scheduler that is a stateless loop over the metadata database, an HTTP API, and workers per runtime (ADR-0003).

The run-state model is designed so that a Temporal-backed execution backend stays possible (NFR-5.5). "Run state as interpretable data with explicit attempts" means:

- Every `run`, `node_instance`, `attempt` and `wait` row is plain JSON-compatible data: no pickled objects, no callables. The scheduler is a pure function from rows to actions and keeps no in-memory state between passes (NFR-1.2).
- Each runtime submission is an explicit `attempt` row with its own status, lease, counters, artifact versions and usage records (ADR-0007), the object no engine owns.
- Timers, approvals, event subscriptions, polls, retry back-off and admission back-pressure are `wait` rows with a `wake_at` or a subscription; nothing holds a worker.
- The backend boundary is prose. The scheduling passes are the only code that moves the four execution tables between states, reached through three entry points: start a run, deliver a signal (approval response, inbound event, cancel, manual retry) and tick. The design review of 2026-10-15 ([record](../../knowledge/reviews/review-2026-10-15.md)) found that an `ExecutionBackend` Python interface with one implementation adds nothing, so none exists (DD-7).

The Temporal backend is prototyped after MVP, by demand (DD-7). A generic interpreter workflow would then replace the passes and project its transitions into the same four tables, while artifacts, lineage, approval requests, usage records, session segments, events and the registry stay in kora's database, written through the same repository layer; readers always read kora's tables. Two rules keep the door open: no table stores backend-specific handles except `attempt.handle` and `attempt.lease_*`, and the scheduler never relies on in-memory state.

Kestra and Airflow become integration targets only: they start kora runs through the webhook trigger, and a kora command node can call their APIs. A dedicated Airflow operator or Kestra task can follow after MVP.

## Alternatives considered

- **Temporal as the core from the start.** Rejected for MVP: a mandatory cluster or Temporal Cloud bill, no standalone mode (NFR-7.1, NFR-0.1), run state split across two stores. Kept as an alternative backend; the later DBOS assessment remains subject to the first-slice gate.
- **Kestra or Airflow as the core.** Rejected for MVP: the reviewed thin-adapter models still require kora-owned attempt lineage, content-bound approval authority and IR lowering; the result is an orchestrator inside an orchestrator, not elimination of that work.
- **DBOS as an embedded durability library.** Not in the original analysis; assessed on 2026-10-16 ([DBOS evidence](../../knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md#dbos)). Not adopted before the first vertical slice; re-evaluated with the slice's data.
- **A coded `ExecutionBackend` interface now.** Rejected as an abstraction with one implementation.

## Consequences

- kora owns scheduling, leases, adoption after crash, stall detection and scale, responsibilities an alternative engine could share, with adapter and operational costs still to prove.
- Standalone mode on SQLite keeps the same schema and queries as PostgreSQL (NFR-0.1).
- A future backend inherits the data model unchanged; nothing in the API or the protocols depends on which backend performs transitions.

## Where it is applied

- [scheduler.md §2](../../knowledge/specs/scheduler.md) (the passes) and [scheduler.md §12](../../knowledge/specs/scheduler.md) (execution backend boundary)
- [data-model.md §1](../../knowledge/specs/data-model.md) (principles) and [data-model.md §9](../../knowledge/specs/data-model.md) (compatibility with a Temporal-backed backend)
- [verdict.md](../../knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md) with retained findings and immutable source records
- [NFR-5.5, NFR-1.2, NFR-0.1](../requirements/non-functional.md)
- [FR-6.1](../requirements/functional.md) and [functional.md §13, decisions 6 and 12](../requirements/functional.md)
- [positioning.md, "Evidence, hypotheses and open risks"](../positioning.md)
- [component map](../technical-introduction.md#3-the-component-map)
