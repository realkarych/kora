# Own scheduler versus existing engines: retained evidence

Observed: 2026-10-15; corrected and extended with DBOS on 2026-10-16. Consolidated: 2026-10-18, issue #95. Method: requirements-to-documentation comparison, with no prototype or benchmark. These are dated source observations and design inferences, not present-day vendor guarantees. [ADR-0001](../../../docs/decisions/0001-own-control-plane.md) owns the decision.

## Decision and shared work

Keep the own control plane for MVP. Temporal remains a possible backend after MVP by demand; DBOS is reconsidered after the scheduler-prototype milestone, which follows the first implementation slice, supplies data. Do not start a comparative prototype before that milestone. Its [ten fault checks](../../specs/scheduler.md#18-scheduler-prototype-milestone-acceptance) include operating cost and remain mandatory whichever engine is considered.

All candidates leave kora-specific work: compiled graph documents, presets, artifact types and lineage, content-bound approvals, harness/runtime/VCS adapters, cost accounting and the public surfaces. Engine retries do not themselves provide kora's `previous_attempt` artifact inputs. Submission still needs durable identity and authoritative lookup. The original percentages of requirements covered, claims that an engine supplies all scale work, and cost estimates were not measured and are withdrawn.

## Temporal

The reviewed model used a fixed workflow interpreting the graph as data, with activities around attempts. Durable timers, messaging, retry policies and workflow identity are useful primitives. Activities are at-least-once: an activity may repeat after a lost completion, so the adapter must preserve the runtime submission key and reconcile before replacement. Engine activity retry is not proof of at-most-once external execution.

Trade-offs: a separate service deployment, workflow determinism/versioning and a history-to-kora projection that must recover from partial writes. The analysed deployment did not supply kora's single-process SQLite shape. Neither engine history nor a task token automatically satisfies later execution/publication fencing decisions. The [deferred proposal](../../../docs/proposals/0001-temporal-execution-backend.md) must prove compatibility if activated.

Primary sources retained from the review: [long-running activities](https://docs.temporal.io/design-patterns/long-running-activity), [message passing](https://docs.temporal.io/develop/python/message-passing), [workflow identity](https://docs.temporal.io/workflow-execution/workflowid-runid), [retry policies](https://docs.temporal.io/encyclopedia/retry-policies). [Original classification](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/own-scheduler-vs-existing-engine/temporal.md).

## Airflow

The review covered Airflow 3.3. Airflow has task attempts and per-try logs; the rejected claim was that it has no attempt object. The mismatch is kora's prior-attempt artifacts/lineage and sub-graph iteration, not absence of retries. The DAG processor parses Python DAG files and emits serialized DAGs that the scheduler reads; serialized JSON is not the equivalent of an independently authored kora IR. A generated loader is possible but adds a lowering and ownership boundary.

Task-instance human input does not by itself establish kora's content-bound approval invariant. Managed-agent operators also mean that “all agents are model loops inside a task” is too broad. These findings support the chosen integration boundary, not a claim that a sufficiently large adapter is impossible.

Primary sources: [DAG serialization](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/dag-serialization.html), [XComs](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/xcoms.html), [human input](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/hitl.html), [Anthropic operator](https://airflow.apache.org/docs/apache-airflow-providers-anthropic/stable/operators/anthropic.html). [Original classification](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/own-scheduler-vs-existing-engine/airflow.md).

## Kestra

The review covered Kestra 2.0. Kestra records task-run attempts; those records were not the prior-attempt artifact input and lineage model required by kora. Subflow retry can create a new child execution. The survey correction distinguished that released behavior from whole-group retry in a later development change, so a blanket “cannot retry groups” claim is not retained.

The analysed adapter still needed kora-owned retry inputs, approval validation and infrastructure routing. The reviewed plugin/deployment choices increased integration work. Kestra remains an integration target under ADR-0001; the analysis did not prove that custom plugins could never implement the missing behavior. The [original classification](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/own-scheduler-vs-existing-engine/kestra.md) records requirement dispositions; the [corrected survey](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/research/agentic-pipeline-orchestrators.md) preserves its primary-source links, including the release/development distinction.

## DBOS

The dated review found an embedded durability library with SQLite and PostgreSQL, durable waits/messages, queues and restart recovery. This corrects the old verdict's claim that no candidate supports the standalone shape. The executor stays in the control plane; an adopter's worker still uses kora's HTTP boundary.

Steps are at-least-once. Kora still needs submission reconciliation, attempts, artifacts, approvals and projections beside durable workflow state. Application versioning and recovery across processes require explicit operational choices; the reviewed multi-process coordination option was Conductor or coordination implemented by the application. This is dated evidence, not a current pricing or licensing recommendation. No DBOS integration or replacement decision has been made.

Primary sources: [architecture](https://docs.dbos.dev/architecture), [configuration](https://docs.dbos.dev/python/reference/configuration), [steps](https://docs.dbos.dev/python/tutorials/step-tutorial), [recovery](https://docs.dbos.dev/production/workflow-recovery), [Conductor](https://docs.dbos.dev/conductor/overview). [Original classification](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/own-scheduler-vs-existing-engine/dbos.md).

## Reconsideration evidence

Compare the same fault scenario and data contracts, both database modes, authoritative runtime identity, publication recovery, operational dependencies, upgrades and measured cost. The current owners are [scheduler sections 12 and 18](../../specs/scheduler.md#12-execution-backend-boundary-nfr-55), [data-model section 9](../../specs/data-model.md#9-compatibility-with-a-temporal-backed-backend-nfr-55), and the later ADRs they cite. [Original verdict](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/own-scheduler-vs-existing-engine/verdict.md) retains the superseded matrix for historical inspection.
