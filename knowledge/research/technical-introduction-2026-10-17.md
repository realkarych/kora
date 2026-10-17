# Research: a concise introduction to kora's core

Checked: 2026-10-17. Baseline: `bd817fbf8d42643d639e3beccb67646a12183c9c`. Status: editorial research, not an architecture decision. Approved scope and plan: [issue #67](https://github.com/realkarych/kora/issues/67). The [technical introduction](../../docs/technical-introduction.md) requires line-by-line owner review.

## Reader and outcome

The intended readers are the framework author, external contributors and adopters who study internals before using a system. They need a model of the core: its components, responsibilities, inputs and outputs, stored state and interactions. A user guide depends on later implementation and real examples.

## Source audit

At the baseline, [internals.md](../architecture/internals.md) contains 1,733 lines, 21,190 whitespace-separated source words, 20 numbered chapters, three appendices and 24 Mermaid diagrams. Excluding fenced blocks leaves approximately 14,598 words. These are source measurements, not page counts. The knowledge index directs new contributors to read it front to back, and its first reading path is estimated at an hour.

The book prepares readers for implementation: it combines concepts with database entities, state transitions, invariants, provider contracts, future tests and contribution workflows. Shortening each chapter would retain that reference-oriented structure. Its dependency-upgrade example does provide a usable execution narrative.

The [architecture overview](../../docs/architecture.md) contains 1,348 source words and two Mermaid diagrams. It is a two-page map of context, processes, stack and boundaries. The missing layer is an explanation connecting that map to graph execution. Both existing documents can keep their purposes.

There is no framework implementation or runtime test suite. [README.md](../../README.md), the [roadmap](../../docs/roadmap.md) and the internals status all say so. The introduction must explain design contracts without claiming successful runtime or isolation testing.

## Editorial evidence and choice

Primary external references, checked on 2026-10-17:

- [Diátaxis: Explanation](https://diataxis.fr/explanation/) describes explanation as connecting concepts and providing context. It cautions against absorbing procedural instruction and exhaustive reference. Applied here: explain why the boundaries exist, while linking detailed contracts.
- [C4: Container diagram](https://c4model.com/diagrams/container) describes applications, stores, responsibilities and communication, separately from deployment. Applied here: draw core process/store connections, then explain standalone versus separate processes in prose and a table.
- [C4: Dynamic diagram](https://c4model.com/diagrams/dynamic) describes concrete interactions implementing a feature or use case. Applied here: complement the map with one successful attempt sequence.

| Structure considered | Assessment |
|---|---|
| Compress the existing book chapter by chapter | Retains too many reference categories before readers have a stable mental model. |
| Expand the two-page architecture overview | Removes the useful compact reference map. |
| Add a component-led introduction with one run | Fits the core-understanding goal while preserving both reading depths; selected in the approved plan. |

Use at most three diagrams: graph example, core connections and an attempt sequence. The [dependency-upgrade IR](../specs/examples/dependency-upgrade.ir.yaml) already covers an agent, a command, content-bound approval and an external wait. Exclude full state machines, signatures, table inventories, lock order, future package catalogs and named future tests. Distinguish runtime, harness and trusted supervision, and logical responsibilities from process placement.

## Architectural source map

Decision records govern specifications. The introduction explains those contracts and does not resolve open implementation details.

| Introduction section | Claims and governing sources |
|---|---|
| What the core does | [Positioning](../../docs/positioning.md), [functional requirements](../../docs/requirements/functional.md), [example IR](../specs/examples/dependency-upgrade.ir.yaml). |
| Objects | Attempts, iterations and invalidation: [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md). Immutable inputs and contracts: [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md), [data model](../specs/data-model.md). |
| Compiler and API | Local compilation, thin clients and the two agent roles: [ADR-0002](../../docs/decisions/0002-ai-first-api-and-thin-clients.md), [DSL](../specs/dsl.md), [IR](../specs/ir.md), [API surface](../specs/api-and-agent-surface.md). |
| Scheduler and worker | Persisted intent and API-mediated workers: [ADR-0003](../../docs/decisions/0003-scheduler-and-workers-over-http.md), [scheduler](../specs/scheduler.md). Unlocked observations: [ADR-0021](../../docs/decisions/0021-external-observations-outside-transactions.md). |
| Runtime, supervisor and harness | Authority, adapter placement, lifecycle and Docker/local boundaries: [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md), [runtime and harness protocols](../specs/protocols.md#2-runtime-fr-81). |
| Persistence | Metadata, inline values, payloads, API-proxied transfer and private checkpoints: [data model](../specs/data-model.md), [storage protocol](../specs/protocols.md#7-artifact-storage-fr-44), ADR-0016. |
| Run walkthrough | [Existing sequence](../architecture/internals.md#7-one-run-end-to-end), example IR and [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md). Targets, acceptance and observations: [ADR-0019](../../docs/decisions/0019-immutable-run-target-identity.md), [ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md), ADR-0021. |
| Recovery, trust and limits | [ADR-0017](../../docs/decisions/0017-execution-uncertainty-and-safe-replacement.md), ADR-0016, ADR-0008, ADR-0020 and [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md). |
| Deployment and extensions | [ADR-0005](../../docs/decisions/0005-eleven-protocols-with-conformance-suites.md), [ADR-0013](../../docs/decisions/0013-implementation-stack.md), [scheduler process layouts](../specs/scheduler.md#1-components-and-processes). |
| Open mechanics | The internals book's [scheduler](../architecture/internals.md#12-scheduler), [persistence](../architecture/internals.md#14-data-model-and-persistence) and [wait/retry](../architecture/internals.md#9-waits-approvals-and-retries) chapters; [issue #48](https://github.com/realkarych/kora/issues/48). |

The introduction preserves the documented uncertainty about API-to-scheduler entry points, some attempt transition writers and terminal run-failure aggregation. It explains scheduling policy and API mediation without choosing a new call mechanism. Later accepted ADR directions retain their pending wording-review status.

## Size and validation

Markdown has no intrinsic page count. The approved limits are 2,800 whitespace-separated prose words outside fenced blocks, three Mermaid diagrams and nine rendered A4 pages including figures and references. Target seven to eight pages, with 11-point body text, 20 mm margins and no cover. Simplify content before reducing readability.

The export is a one-off review artifact, not a repository publishing system. The pull request records exact rendering/counting commands and the page count. Inspect every page for clipped text, unreadable labels and table breaks.

Current CI, checked on 2026-10-17, uses Markdownlint CLI 0.45.0, Lychee 0.24.2, Mermaid CLI 12.0.0, and check-jsonschema 0.38.2 for IR examples and GitHub templates. Run those unchanged. Also lint this file explicitly, since the normal Markdownlint configuration excludes `knowledge/research/`. These checks validate documentation and schemas, not an implemented runtime.
