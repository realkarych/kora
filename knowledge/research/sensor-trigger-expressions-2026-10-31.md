# Sensor-trigger contextual CEL evidence

Date: 2026-10-31. Owner: [issue #537](https://github.com/realkarych/kora/issues/537). Base: `010698995828ddbf700e9b3622e0206b3af8a7b2`.

## Scope and interpretation

`validate_sensor_trigger_expressions` connects only root graph sensor `poll.until` expressions to their existing CEL environment. IR section 7.1 permits only observation; the settled section 7.4 environment rule and existing checker tests supply `Variable(DYN)` with existing `DATA_BOUND`. No Observation schema, sensor configuration schema, provider inventory or mutable alias is inferred. `ResultKind.BOOL` rejects known wrong types but accepts dynamic results; runtime exact-Boolean checking and start authority remain unimplemented here.

Existing syntax ownership is preserved through a local prerequisite parse: parser failures increment unresolved accounting without another finding. Existing typed checking then supplies contextual rules, canonical rendering/reparse and logical cost admission. Findings retain the exact indexed field, graph source only, null node and code-point offset through the existing safe converter/collector. Results contain diagnostics/scalars; authored values and field sets are unchanged.

Existing TM-04/TM-10 input/work ownership, TM-06/TM-07 content exclusion and TM-11 interpretation/non-authority boundaries cover this scope. No actor, control, trust assumption, saved format, dependency, package permission or numerical gate changes. No `docs/` wording changes.

## Verification method and status

Independent correctness, security and performance design contexts read the plan, accepted contracts and baseline source before edits, and returned passed for the proposed scope with implementation pending. Correctness identified missing event-catalogue/command planning evidence; the exact scalar field/type list and runnable checks were added to the issue before source edits. Design results are not implementation proof. Actual-result review uses the same contexts and complete diff, with raw local/native evidence and provenance recorded in the PR.

The semantic module covers permitted dynamic observation behavior, unavailable roots, known wrong results, overload/shadow/size checks and logical cost refusal; syntax-unresolved/no-duplicate behavior, independent trigger indexes, opaque config/params, absent/empty triggers, safe graph source and non-BMP offsets; and dense count/byte pressure. Resource checks compare existing instrumented work across 1/100/1,000 identical predicates and test admitted source/depth shapes. Exact weak references and temporary-owner counts check success, typed refusal and syntax-unresolved release while results survive, then result/diagnostic disposal. Real blocked/broken pipes exercise continuing identical results, finite queues and writer closure. They add no timing, heap/RSS or universal additive graph-validation guarantee.

The first semantic run passed all 35 cases; the focused semantic/resource/logging run passed 54 cases. A test-helper kwargs typing ambiguity was corrected without production behavior changes; strict Pyright then passed with zero errors. After the test typing and explicit poll-owner reference were finalized, the focused run passed again. The related syntax/checker/cost/architecture run passed 1,495 cases in 35.10 seconds. Ruff format/lint passed. Raw JUnit/logs are retained in a task-private directory for independent inspection. Independent actual conclusions and final-head CI must be recorded on the PR. One complete successful final-head CI with supported Linux/macOS and existing SQLite/PostgreSQL coverage remains required. The pass itself touches no database.

## Remaining owners

Wait-event and general node inputs/params/output environments, provider inventory/config/minimum interval, observation production/projection/binding, native binding/regex compilation through server `Evaluator.validate`, saved execution contracts, public API/client validation and scheduler start/apply transactions remain separate implementation owners. ADR-0026 keeps `ir` server-free. Static contextual success does not establish native compilability, request-wide capacity, exact runtime bool, complete graph validation or runtime admission.
