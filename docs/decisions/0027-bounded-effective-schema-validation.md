# ADR-0027: Bounded effective-schema validation

- Status: accepted on 2026-10-30 after explicit owner approval of this record and the design plan under [issue #138](https://github.com/realkarych/kora/issues/138).
- Deciders: project owner
- Related: [ADR-0005](0005-eleven-protocols-with-conformance-suites.md), [ADR-0018](0018-immutable-attempt-history.md), [ADR-0026](0026-package-layers.md)

Preparation-foundation prerequisite amendment accepted on 2026-10-30 under issue #498 after explicit owner approval of the scoped implementation plan and exact wording below. The initial evaluation policy remains reserved and unavailable.

Evaluation-policy and numerical-foundation amendment accepted on 2026-10-31 under issue #515 after explicit owner approval of the complete evaluation resource and charging policy, the scoped numerical implementation plan and the exact wording below. Approval of the policy does not make its reserved identity executable before the complete implementation meets its activation gates.

## Context

Standalone artifact-type providers implement selected type behavior. Complete StructuredOutput validity additionally depends on the saved parent and reference closure, applicable inline schemas and immutable interpretation. General schema evaluation can consume unbounded work through regular expressions, references, numerical operations and intermediate errors. The existing synchronous provider hooks perform no I/O and carry only the exact built-in binding.

## Decision

Use a common core composition for complete effective validation, preserving the provider protocol and its constructor ABI. Pure preparation, exact numerical operations and schema evaluation belong in `kora.ir`. A shared pure helper in `kora.protocols` combines trusted bound type behavior with those operations. Future runner, API and scheduler consumers must use the same composition and saved input before accepting outputs or extracting effective digests and summaries. This decision implements no consumer.

The operation-local input retains the original declared, parent and reference schemas and their sources, exact target bindings and behavior identities, applicable inline schema, selected checker schema, the existing `kora.structured-output-view.v1` binding and the exact execution policy. Declared, parent and inline schemas constrain the same owned direct value; a reference document is evaluated where its reference is used, rather than becoming another top-level constraint. Original saved identity is separate from prepared execution views. No network, filesystem, current-alias or automatic-code lookup supplies schema authority.

Reserve `kora.structured-output-schema-policy.v1` for the initial policy. The selected architecture uses the pinned draft-2020-12 engine with bounded handlers for primitive assertions, exact numerical/equality constraints, object and array applicators, closed references and boolean composition. Format, content and metadata remain annotations. The policy does not execute `pattern`, `patternProperties`, `unevaluatedItems`, `unevaluatedProperties` or a cyclic potential validation-call graph. The eventual profile must refuse these unsupported constructs anywhere in relevant schema positions, and must refuse lone surrogates in retained schema/input text before UTF-8 encoding. Structural IR admission/export remains unchanged. Structurally valid IR outside this support policy remains representable; affected execution is unavailable under this policy. This is a limited execution profile, not unrestricted draft-2020-12 support.

Save `schema_validation.policy` and its complete `parameters` map in each affected future effective contract. The existing deployment/effective-contract writer owns their canonical saved representation and digest inclusion; this decision adds no second digest or database format. Missing or unsupported policy cannot inherit current defaults for new effects. Historical reads retain saved results. Acceptance-affecting changes require a new immutable identity or an explicitly supported immutable compatibility binding. Existing StructuredOutput schema, descriptor, vectors, payload bounds, canonical identity and checker binding remain unchanged.

The preparation foundation is implemented under issues #500 and #501. Its normalized input, ten capacities and deterministic preparation algorithm remain unchanged. Issue #515 defines the complete evaluation resource policy in the normative StructuredOutput specification and implements only the exact numerical foundation. The saved policy contains all ten preparation capacities plus an intermediate integer limit of 16,384 bits, an evaluation-work limit of 16,777,216 logical units and an active schema-frame limit of 64. These inclusive product support capacities and the deterministic numerical, handler, adapter and owned-state accounting are immutable interpretation, not time or Python heap/RSS guarantees. The policy identity remains reserved and unavailable for effective execution until exact arithmetic, recursive equality, every supported bounded handler, safe engine adaptation and common composition satisfy their named implementation and verification gates. No missing or partial component inherits a current default or grants artifact acceptance.

Work is charged before the bounded primitive or allocation. Each complete preparation/evaluation shares its meter across constraints, references and failed branches. The policy charges bytes, comparisons, bounded lookup/sort work, integer-limb operations and intermediate state; a keyword-entry counter is insufficient. Canonical keyword/property order and saved array/alternative order make traversal deterministic. Unmetered opaque helpers and content-bearing intermediate errors cannot establish support. Complete deterministic accounting/charging and safe execution rules must be accepted before the policy becomes executable and then belong to the normative [StructuredOutput specification](../../knowledge/specs/artifacts/StructuredOutput.md).

Numerical schema decisions use exact rational values of frozen canonical decimal spellings, including recursive equality. Boolean values remain distinct; payload identity independently retains integer/float kind and signed zero. Initial uniqueness checking uses bounded pairwise exact comparison without a new fingerprint identity. A large valid array may exhaust work; an unconstrained admitted array acquires no new item limit.

A completed mismatch is distinct from unsupported semantics or exhausted capacity. Only a valid complete result permits effective extraction. Refusal is never converted into schema-false inside `not`, `if`, `anyOf` or `oneOf`. Public diagnostics contain fixed codes and bounded counts, with no authored names, schema/payload content or native exception chain.

Preparation owns temporary snapshots and tables; the prepared-contract holder owns retained state. Each finite synchronous evaluation releases its frames, counters and temporary state on every exit. No process, thread, task, interrupt deadline, Python heap/RSS ceiling or latency guarantee is introduced. Future consumers must bound aggregate prepared-contract retention, concurrent calls and scheduling impact before activation. Derived lists and their aggregate admission remain separately unavailable.

## Alternatives considered

A core-owned evaluator process can contain non-cooperative schema work and preserve broader syntax. It additionally requires admission and queue limits, bounded transfer, portable enforced resource controls, cancellation and observed child exit/reap before releasing capacity. Those facilities are not implemented or selected here. A thread wait timeout and CEL pattern parsing do not supply them. Unrestricted stock schema evaluation inside provider hooks is rejected.

## Consequences

Common MVP schemas can be implemented incrementally without changing provider discovery or adding a matching dependency. Schemas using the explicitly unsupported features require a later approved support policy or evaluator design. Concrete implementation plans retain exact numerical semantics, closed authority, independent correctness/security/performance review, focused regressions and one successful final-head CI per PR. The limits are product support capacities, not measured performance guarantees.

## Where it is applied

[StructuredOutput](../../knowledge/specs/artifacts/StructuredOutput.md), [authoritative effective contracts](../../knowledge/specs/ir.md#structuredoutput-effective-contracts), [saved graph-version contracts](../../knowledge/specs/data-model.md#111-graph-version-contract) and the [ArtifactType/runner contracts](../../knowledge/specs/protocols.md). This record establishes the design; preparation, numerical primitives, evaluator handlers, common composition and real consumers retain separate implementing work and activation evidence.
