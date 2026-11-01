# Same-call trusted validation facts

The project owner selected this lifetime and ownership boundary on 2026-11-01 under [#596](https://github.com/realkarych/kora/issues/596), following the alternatives in [#549](https://github.com/realkarych/kora/issues/549). It is a normative design contract, not evidence of implemented provider selection, validation or execution. ADR-0009 governs protection requirements; ADR-0026 governs package placement. The exact internal record/API shape remains with its concrete implementation plan.

The native environment-reservation contract in section 6 was separately approved on 2026-11-01 under [#617](https://github.com/realkarych/kora/issues/617). Its source ABI, real producers and consuming validation remain pending implementation; the earlier same-call decision did not implicitly select that accessor or grammar.

## 1. Scope and lifetime

One validation invocation receives a detached immutable view of the facts from its selected configured harness and runtime instances. The view belongs to that invocation and the unchanged authored document being checked. It is not serialized, persisted, transferred between processes, cached across calls or reused for deployment/recovery authority. The #596 lifetime decision introduces no durable installation identity, database field, provider ABI member or public operation; section 6 separately specifies a future native accessor.

The trusted server composition owns selection and the consistency of the configuration view. A provider's registration name/version alone is not configured-instance identity. Authored IR, extensions, environment values and caller-supplied claims cannot substitute for trusted selection. Different configurations of the same provider remain distinct selections. A local invocation identity can detect accidentally mixed records; it cannot prove that the producer selected the right instance, captured complete facts or used current policy.

The producer releases provider/configuration references after projection and invocation use according to their existing owner lifetimes. Detached facts and results do not retain provider objects, credentials, mutable configuration, callbacks or environment values. This contract creates no new provider-lifetime owner, lock, cache or background service.

## 2. Responsibilities

| Owner | Required responsibility | Implementation ownership |
|---|---|---|
| Server validation/composition | Resolve the actual configured harness/runtime for each applicable agent site using the accepted selection/default rules; keep the selected instances and configuration consistent through fact capture and the synchronous checks; supply the correct current namespace policy through its trusted owner. Do not delegate selection to an IR checker. | Work items 3/11; the concrete production resolver and its selection/default rules remain prerequisites under #549. |
| Harness adapter | Own completeness and meaning of its native `reserved_env` and typed `policy_coverage` for that exact configured instance. Supply no claimed coverage or empty reservations as a substitute for unavailable facts. | Work item 15; the accepted harness protocol and each real adapter implementation. |
| Runtime/launcher | Own the complete names it assigns to the candidate environment for the selected configuration, including applicable enforcement settings. Authored workload keys and adapter-supplied inputs are not runtime reservations merely because they occur in the final environment. | Work item 14; section 6 specifies the native accessor under #617. Complete production sources and receiver acquisition remain missing under #549. |
| Namespace policy/provisioning | Own the policy for the exact namespace and its current revision. Preserve existing policy authorization, provisioning and consistency requirements. | Work items 3/11/20; trusted provisioning/consumption remains a prerequisite. |
| Shared projection producer | Copy, validate and detach facts from the selected native owners into one immutable same-call view. Keep unresolved distinct from known-empty. Preserve the exact document site/selection association and each feature's completeness. | Server composition, shared by IR026 and IR019/IR028; no separate provider inventory per checker. |
| Feature checker | Consume the immutable view without loading providers, choosing defaults or calling native methods. Check applicable document/site/selection consistency before consuming facts. Produce known findings and explicit completeness; do not invent missing facts. | Work item 3; shared IR-owned input is compatible with ADR-0026, without an `ir` to `protocols` import. |
| Whole-validator/API composition | Combine feature findings and completeness with existing prerequisite failures. Refuse successful validation/admission when a required protection remains unresolved or inconsistent. Preserve the existing ownership of authored-input, unavailable-provider, transient-dependency and internal failures. | Work items 3/11; exact mapping of otherwise-unreported incompleteness remains unresolved under #549. |

Runtime selection/default resolution and public failure mapping require their concrete accepted rules before executable activation. Section 6 fixes native reservation spelling and completeness; its actual normalization and producer integration remain pending. Naming an owner does not invent a working producer.

## 3. Consistency and incomplete facts

Facts must refer to the same invocation, namespace, authored agent site and selected configured instances as the document being checked. An internally consistent view still depends on trusted producer guarantees; matching names, hashes or local keys do not establish authenticity or current authority. Contradictory or mismatched supplied facts must not authorize successful validation. The concrete record shape and mismatch reporting belong to the implementing plan, preserving this fail-closed boundary.

Known-empty and unresolved are different states for every feature. A complete empty reservation set can establish no collision for that owner; a missing set cannot. Known failures from one owner remain reportable when another owner's facts are missing. A feature with no authored obligations does not prove that its provider is installed, supported or currently authorized. An empty diagnostic collection alone is never evidence that all required checks completed.

The whole validator must not return normal `valid: true` or admit deployment/start using incomplete required protections. Existing IR018, IR019, IR022, IR026, IR028 and API010/API011 meanings remain unchanged. This decision assigns no new error code or HTTP status and does not classify every unresolved internal state as an authored graph defect. If no accepted failure owner/mapping covers an incomplete case, that public integration remains unavailable until its concrete contract is decided.

## 4. Separation from execution authority

The same-call view is neither a credential nor a capability to execute. It supplies no persisted deployment interpretation, immutable execution-contract representation, lease, runtime grant, deadline, publication authority or recovery evidence. Deployment and canonical start retain their own saved-contract checks and fresh identity/policy comparisons under the existing contracts; a successful earlier validation cannot replace them.

The saved execution contract in [data-model section 11.1](data-model.md#111-graph-version-contract) remains a separate immutable interpretation. This call-local view is not adopted as its serialized format. Missing saved semantics still refuse new effects without importing today's defaults. Existing history and closure obligations remain unchanged.

## 5. Activation and remaining decisions

[#549](https://github.com/realkarych/kora/issues/549) continues to own the concrete producer/consumer prerequisites: exact selection/default resolution and receiver acquisition, complete runtime-owned environment inventory, real harness declarations and normalization under section 6, trusted policy required-kind/subagent classification, the `trust_repo_config` wording disposition, the exact immutable input/result records, and mapping of incomplete results not already owned by existing diagnostics. The earlier issue proposal's detailed DTOs are not approved by these bounded decisions.

Executable activation requires a real production source for every consumed fact and an approved implementation plan for its consumers. Provider registration metadata, testing/conformance composition and a fake harness's empty facts do not establish this prerequisite. No empty producer, fake-only inventory or unused checker skeleton is introduced to satisfy it.

The implementing issues must exercise exact selected-instance/configuration binding, same-call/document/site mismatch, complete-empty versus unresolved facts, partial known failures, current namespace policy, safe diagnostics and actual reference/resource release. Correctness and security reviews follow the changed boundary; performance is selected only for a concrete changed work/resource risk. This design creates no new numerical acceptance method or additional verification campaign.

## 6. Native candidate-environment reservations

The accepted future Runtime instance method is `candidate_environment_reservations() -> tuple[str, ...] | None`. The selected configured runtime owns the answer. It is synchronous and reads already-validated configuration and native ownership definitions only, with no runtime resource allocation, candidate inspection, secret resolution, database, filesystem or network I/O. It does not construct a runtime, determine availability, identify a worker or attest to host isolation.

| Native result | Meaning |
|---|---|
| Tuple of reservations | Complete names and name prefixes reserved by this runtime in the candidate environment for the selected configuration, including applicable conditional enforcement controls |
| Empty tuple | Complete, known-empty set, requiring the same native conformance evidence as a nonempty declaration |
| `None` or an older provider without the accessor | Unavailable or incomplete projection; never a known-empty declaration |

The implementation shares its ownership definitions with actual candidate-environment construction. A separate validator whitelist is insufficient. Forwarded workload-authored variables, resolved secret values and adapter-supplied values do not become runtime reservations merely because the runtime forwards them. The supervisor environment, provider registration metadata and examples in the launcher specification are not an exhaustive candidate inventory. An unfinished candidate-environment path cannot claim complete production evidence from them or from a fake.

Both this tuple and the harness's existing `reserved_env` use exact ASCII environment names matching `[A-Za-z_][A-Za-z0-9_]*`, or a nonempty such literal prefix followed by one final `*`. A prefix matches zero or more following characters. A lone `*`, interior wildcard, question mark, bracket pattern or Unicode normalization is unsupported. Matching is case-sensitive on Linux and macOS; no general path/tool glob or global case folding is reused. Duplicate and subsumed reservations have the same meaning as their set and may be removed without changing ownership.

This spelling rule preserves every native case obligation. In particular, the [Claude environment contract](protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) still reserves `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` in every letter case. Its complete native declaration covers every required spelling, including mixed case; an uppercase-only list is incomplete even though authored IR currently admits uppercase environment names only. This decision selects no implementation algorithm or new numerical bound for that declaration.

The shared producer validates and detaches declarations into literal names and prefixes for the same-call view. It receives no values. The configured-instance, namespace, document/site and invocation associations remain the composition's responsibility: a return value alone does not prove selection or freshness. Known authored collisions remain `IR026`; malformed or incomplete native facts cannot establish successful validation, deployment or start. No new public error code or otherwise-unreported incomplete-result mapping is assigned here.

Launch-time single-owner collision checks remain mandatory before launch. Early validation does not replace them, persist reservations, grant execution or authorize a changed installation. Saved-contract compatibility, current policy, grants, deadlines and recovery keep their existing owners and guarantees.

### Implementation and activation

The source accessor must land with its actual producer and candidate builder, provider conformance and consuming integration under a linked concrete plan. That plan must establish how validation obtains its configured receiver without opening an execution host merely to obtain facts. Both receiver acquisition and accessor invocation satisfy the no-initialization/no-I/O boundary; the current effectful `LocalRuntime.open` is not a validation construction path. A pure method on an effectfully acquired receiver does not satisfy this prerequisite.

Runtime ownership remains with work item 14 and [#184](https://github.com/realkarych/kora/issues/184)/[#196](https://github.com/realkarych/kora/issues/196)/[#199](https://github.com/realkarych/kora/issues/199) as applicable. Work item 15 owns each real harness declaration. Server selection/projection and whole validation remain under #549 and work items 3/11. This decision introduces no partial provider, unused checker, metadata cache, persisted field, public operation, dependency or new resource owner.

Future executable evidence must cover exact/prefix matching and every required mixed-case proxy spelling; malformed, missing, unavailable and complete-empty declarations; conditional controls; exclusion of forwarded author/adapter values from runtime ownership; configured-instance mismatch; no values in facts or diagnostics; effect-free receiver acquisition and invocation; agreement with actual candidate construction and its duplicate-owner refusal; and no successful validation while a required producer is incomplete. Real implementations and conformance supply that evidence, with applicable failure, cancellation and cleanup cases. No executable evidence is established by this design.
