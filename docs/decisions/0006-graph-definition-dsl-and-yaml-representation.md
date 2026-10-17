# ADR-0006: Graphs are written in a Python DSL and compiled to a versioned YAML intermediate representation

- Status: accepted, 2026-10-15; amended 2026-10-16 (the compile step runs in the author's own process, [ADR-0002](0002-ai-first-api-and-thin-clients.md), issue #15)
- Reviewed: by the project owner, 2026-10-16
- Amended by: [ADR-0018](0018-immutable-attempt-history.md), 2026-10-17 (deployment contracts and attempt-owned history); wording pending owner review
- Amended by: [ADR-0020](0020-execution-capacity-and-bounded-run-acceptance.md), 2026-10-17 (bounded acceptance, execution occupancy and pending-window overlap); wording pending owner review
- Deciders: project owner, after the design review of 2026-10-15 (first cycle, simplicity and practice lenses; second cycle, simplicity pass and consistency fixes), see [review-2026-10-15.md](../../knowledge/reviews/review-2026-10-15.md)
- Absorbs: DD-1, DD-3, DD-9, DD-11, DD-12, DD-24, DD-25, DD-26, the DSL part of requirements decision 17, NFR section 6
- Related: [ADR-0001](0001-own-control-plane.md), [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0007](0007-attempt-model-and-retries.md), [ADR-0012](0012-no-transform-node.md), [ADR-0013](0013-implementation-stack.md)

## Context

kora orchestrates graphs whose nodes are whole coding-agent sessions. Graph authors are engineers used to Airflow-style Python; a platform team must review, diff and operate hundreds of graphs it did not write. The scheduler is shared by many teams, so it must never execute author code at run time (FR-1.2, FR-7.5), and the definition must stay portable to other SDKs and a UI editor (NFR-6.1). Archon, the primary contrast in the positioning document (requirements decision 13), has authors write YAML by hand.

## Decision

Graphs are authored in Python with decorators and the `>>` operator; `kora compile` lowers them to a YAML intermediate representation (IR), the only thing the scheduler consumes.

- **DSL surface.** `@graph` and `@subgraph` functions declare nodes through `node.*` constructors; keyword arguments carrying other nodes' outputs create data edges, `>>` and `<<` create control edges. Node inputs are keyword-only; a positional argument to a node is compile error `DSL003` (DD-24). The graph function runs once at compile time; no Python runs on the execution path. `@graph(repo=…)` is optional; a node left without any workspace source is `DSL003` (DD-26).
- **Compile step.** `kora compile` imports the module in the author's own process (compilation is client-side and never an API operation, [ADR-0002](0002-ai-first-api-and-thin-clients.md), amended 2026-10-16), resolves the global → graph → preset → node layers, validates with the validator the API uses and emits YAML in schema key order without timestamps; identical sources give byte-identical output (NFR-6.2), checked in CI. The document carries `ir_version` (format, semver) apart from `graph.version` (the author's), validates against the published JSON Schema [ir.schema.json](../../knowledge/specs/ir.schema.json), and separates topology (`graph`), policy (`runtime`) and secret references (`secrets`), with authoritative `type_contracts` for used built-in revisions and custom schemas/behavior, and compiler provenance under `meta` (ADR-0018). The deployment snapshots the resolved contract by value; informational provenance never supplies an execution input.
- **Trigger rules** are a closed enum of seven in IR v1: `all_success`, `all_done`, `one_success`, `one_failed`, `none_failed`, `all_skipped` and `none_failed_min_one_success`, the last borrowed from Archon so that a node after an all-skipped branch stays skipped (DD-1).
- **Expressions are CEL** (Common Expression Language): branch conditions, retry predicates, `approve_when`, event `match` and sensor `until` (DD-3). The DSL takes CEL strings only. Evaluation goes through the Rust-backed `common-expression-language` binding; since it has no static checker, `kora validate` runs kora's own schema-driven checker (`IR011`), and the CEL conformance suite runs in CI.
- **Prompt templates are full Jinja2 in a sandbox** (DD-11): no filesystem loaders, imports or includes, no Python attribute access beyond the bound data; rendered once at attempt start and recorded on the attempt.
- **Fan-out body is always a sub-graph** (DD-9). The compiler wraps a single node; the `fan_out` node itself declares the collected `List[T]` outputs, so there is no fan-in node kind. A body is fed only through the node's `inputs`, with `over` naming the mapped one.
- **Trigger overlap** defaults to `allow` for every trigger kind except webhooks, which default to `queue` so that a burst of events does not start a burst of runs (FR-6.2, aligned on 2026-10-16); `skip` and `queue` are per-graph options in `graph.overlap` (DD-12). [ADR-0020](0020-execution-capacity-and-bounded-run-acceptance.md) makes starts bounded and canonical: `skip` considers accepted `pending` as well as `running` runs; `queue` promotion selects the oldest pending run under the graph scope lock when no run is running. Accepted delivery/skip replay precedes current-version validation and quota checks.
- **Presets live in two places** (DD-25): Python objects in graph code and registry presets published by the platform team. `kora presets pull` fetches the latter into `.kora/presets/` beforehand, so compile stays offline; both kinds are snapshotted under `meta.presets`, and the scheduler reads only each node's resolved `config`.

## Alternatives considered

- **YAML authored by hand**, as Archon does. Rejected: typed artifacts as Pydantic models, presets with merge rules and source locations in compile errors (NFR-10.3) come naturally from a language, and the YAML still exists as the committed, generated output.
- **A Python-only runtime graph with no intermediate document.** Rejected: author code would sit on the scheduler's execution path, portability would be lost, and "what will run" could not be diffed.
- **A restricted, expression-only template language** for prompts, proposed by the simplicity lens of the design review of 2026-10-15 (first cycle). Declined by the owner; the full Jinja2 sandbox was confirmed (DD-11).
- **A Python expression builder** for conditions. Dropped in the same review in favour of CEL strings that land in the IR unchanged.

## Consequences

- Three file classes stay distinct (NFR-6.4): human-authored Python and presets, generated and committed `build/*.ir.yaml` and `.kora/presets/*.json`, and secrets excluded from version control; the IR is never hand-edited.
- Compile and validate errors share one vocabulary of stable codes (`DSLnnn`, `IRnnn`) with locations, so an agent in the user's own coding session can fix graphs without parsing prose (FR-0.4).
- Every feature must be expressible in the IR first; the DSL is a client of it. The closed trigger-rule enum and the absence of a fan-in kind keep IR v1 small; adding either is a minor format bump.
- The CEL checker and the sandboxed Jinja2 renderer are the two places where author-provided text is interpreted, and kora code to maintain and unit-test.

## Where it is applied

- [FR-1.1–1.7](../requirements/functional.md), [FR-2.5, FR-2.6](../requirements/functional.md), [FR-3.2](../requirements/functional.md)
- [NFR-6.1–6.4](../requirements/non-functional.md), [NFR-10.3](../requirements/non-functional.md)
- [ir.md §1, §2](../../knowledge/specs/ir.md) (design rules, layout, `meta.presets`, `graph.overlap`), [ir.md §3](../../knowledge/specs/ir.md) (trigger rules), [ir.md §4.4](../../knowledge/specs/ir.md) (fan-out), [ir.md §7](../../knowledge/specs/ir.md) (CEL and Jinja2), [ir.md §8](../../knowledge/specs/ir.md) (overlap), [ir.md §10](../../knowledge/specs/ir.md) (`IR011`, `IR012`)
- [ir.schema.json](../../knowledge/specs/ir.schema.json) (`triggerRule`, `fanOutConfig`, `outputDecl.collect`, `graph.overlap`)
- [dsl.md §1](../../knowledge/specs/dsl.md) (surface), [dsl.md §2](../../knowledge/specs/dsl.md) (compile step, error codes), [dsl.md §3](../../knowledge/specs/dsl.md) (preset layering), [dsl.md §6](../../knowledge/specs/dsl.md) (determinism and versioning)
- [design-decisions-2026-10-15.md](../../knowledge/reviews/design-decisions-2026-10-15.md) DD-1, DD-3, DD-9, DD-11, DD-12, DD-24, DD-25, DD-26
