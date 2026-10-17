# ADR-0018: Immutable deployment contracts and attempt-owned execution history

- Status: direction and implementation plan accepted, 2026-10-17; document wording pending owner review
- Reviewed: pending line-by-line project-owner review
- Deciders: project owner
- Amends: [ADR-0006](0006-graph-definition-dsl-and-yaml-representation.md) (compiled type contracts), [ADR-0007](0007-attempt-model-and-retries.md) (historical configuration, lineage and retention)
- Related: ADR-0002, ADR-0008, ADR-0011, ADR-0016, ADR-0017
- Resolves: issue #53

## Context

Attempts and their differences are part of the MVP. A node's current input pointers are cleared on retry, and output-rooted lineage cannot describe a failed attempt with no outputs. A configuration digest cannot reconstruct bytes that were never saved or have expired. A graph pinned only to type names can also change meaning when a namespace replaces a schema or an installed implementation changes its summary or digest behavior.

Historical attribution and the authority to perform another effect are different concerns. Recovery changes worker authority, while the request that the original attempt received must stay identifiable. Preserving history must not weaken the execution and publication barriers of ADR-0016 and ADR-0017.

## Decision

**Freeze the deployment contract by value.** Compiled IR carries authoritative `type_contracts` outside informational `meta`: exact custom definitions and immutable built-in revisions. Deployment snapshots the used schema, parent/reference and behavior closure into `graph_version.execution_contract`. Validation, assignability, branches, collections and approvals use this contract, including scheduler-only nodes. Deployment identity binds the canonical IR and resolved contract; current registry aliases never reinterpret an existing version. Compilation remains offline and author-side. JSON Schema draft 2020-12 is supported; references resolve within the saved closure, without network lookups.

**Give each attempt its own inputs and request.** Creating an attempt copies its exact input bindings independently of outputs and mutable node pointers. Before preparation or execution effects, the API saves its nonsecret execution request once: configuration and rendered text, feedback, input and contract identities, intended workspace/retry behavior and the effective namespace execution policy. Attempts that never reach this boundary report that no request was recorded. A policy-revision check at sealing prevents using a stale namespace policy. A new attempt uses current policy; recovery of the same attempt keeps its saved request.

**Separate content from authority and observations.** The semantic request digest excludes dispatcher/lease authority, credential values and secret-derived hashes. Runtime workloads bind this request and verify their immutable logical content against it; allocation-specific paths, prepared plans and actual checkout facts are recorded in the existing trusted checkpoint. Recovery does not re-render the request or resolve current inputs, feedback or branch heads. Existing records, events and the supervisor journal capture actual checkout, image, harness, strategy/fallback and resource observations when known. An absent observation means unknown. These records grant no execution/publication authority and prove no termination.

**Keep compatibility explicit.** Store the applied type-contract identity and validation, summary and digest-selection results with artifacts, so historical reads do not execute today's plugin. Content-bearing results remain payloads. A behavior descriptor identifies required semantics; it does not archive executable Python. New execution requiring unavailable semantics fails explicitly before the affected effects. A compatible installation upgrade retains support for active contracts. IR format, execution-record format, execution semantics and installed build are separate identities; SQL compatibility alone proves none of the others. NFR-4.6 remains target scope, without a full environment archive or replay promise.

**Preserve current authorization.** API/namespace access, secret authorization, quotas, daily-budget admission and late publication authorization keep their current checks. A saved execution policy is historical evidence, not permission to bypass them. Updating a namespace policy does not promise to revoke already sealed execution immediately; cancellation, runtime retirement and external-service permissions retain their existing roles.

**Expire content honestly.** Compact bindings, contracts, policy values, digests and attribution metadata remain durable. Rendered configuration/prompts, feedback and content-bearing summary/projection values follow resolved run payload retention, including inline copies: by default 30 days after run completion. APIs report expired, missing, not-recorded or unknown evidence without substituting current data. Manual retry requires available inputs and otherwise returns structured `API002` details before effects; internal execution reports `config_error`. Possible future retry does not pin terminal-run payloads forever. Active execution and unresolved recovery retain only their required references/checkpoints, with shared-reference-safe deletion. Prompt/feedback content and equivalent diff projections require the existing namespace `transcripts` capability across all clients.

## Alternatives considered

- **Immutable revision registries.** Viable when shared revision management becomes a product requirement. Complete by-value graph/attempt records provide the needed guarantees without extra revision APIs and lifecycle rules.
- **Reconstruction from current pointers or event summaries.** Rejected: pointers change, output-less attempts exist, and current events do not contain every input and rendered value.
- **A complete event-sourced execution ledger or environment archive.** Deferred: existing entities, events and runtime checkpoints suffice for the MVP; arbitrary historical Python/plugin execution and deterministic model replay are not promised.

## Consequences

- Some JSON metadata is duplicated; content-addressed payload references avoid repeated large prompt/input copies.
- Retry diffs remain useful after content expiry, but become explicitly partial. Deleted required inputs can prevent retry of an old terminal run.
- Built-in and custom behavior identities need compatibility checks. Removing support for active contracts is not a compatible upgrade.
- Resource and skill delivery remain separate changes (issues #45 and #48). History records declared/observed identity and availability without implementing their transport.

## Where it is applied

- [Functional requirements](../requirements/functional.md): FR-1.7, FR-4.3, FR-4.5, FR-6.8, FR-11.5, FR-12.5; [non-functional requirements](../requirements/non-functional.md): NFR-2.1, NFR-4.2–4.3, NFR-7.2.
- [IR](../../knowledge/specs/ir.md), [schema](../../knowledge/specs/ir.schema.json), [DSL](../../knowledge/specs/dsl.md) and both [example documents](../../knowledge/specs/examples/).
- [Data model](../../knowledge/specs/data-model.md), [protocols](../../knowledge/specs/protocols.md), [scheduler](../../knowledge/specs/scheduler.md), [API and thin clients](../../knowledge/specs/api-and-agent-surface.md).
- [Detailed architecture](../../knowledge/architecture/detailed.md), [contributor introduction](../../knowledge/architecture/internals.md) and [console pages](../../knowledge/design/console/pages.md).
