# Same-call trusted validation facts

The project owner selected this lifetime and ownership boundary on 2026-11-01 under [#596](https://github.com/realkarych/kora/issues/596), following the alternatives in [#549](https://github.com/realkarych/kora/issues/549). It is a normative design contract, not evidence of implemented provider selection, validation or execution. ADR-0009 governs protection requirements; ADR-0026 governs package placement. The exact internal record/API shape remains with its concrete implementation plan.

## 1. Scope and lifetime

One validation invocation receives a detached immutable view of the facts from its selected configured harness and runtime instances. The view belongs to that invocation and the unchanged authored document being checked. It is not serialized, persisted, transferred between processes, cached across calls or reused for deployment/recovery authority. This decision introduces no durable installation identity, database field, provider ABI member or public operation.

The trusted server composition owns selection and the consistency of the configuration view. A provider's registration name/version alone is not configured-instance identity. Authored IR, extensions, environment values and caller-supplied claims cannot substitute for trusted selection. Different configurations of the same provider remain distinct selections. A local invocation identity can detect accidentally mixed records; it cannot prove that the producer selected the right instance, captured complete facts or used current policy.

The producer releases provider/configuration references after projection and invocation use according to their existing owner lifetimes. Detached facts and results do not retain provider objects, credentials, mutable configuration, callbacks or environment values. This contract creates no new provider-lifetime owner, lock, cache or background service.

## 2. Responsibilities

| Owner | Required responsibility | Implementation ownership |
|---|---|---|
| Server validation/composition | Resolve the actual configured harness/runtime for each applicable agent site using the accepted selection/default rules; keep the selected instances and configuration consistent through fact capture and the synchronous checks; supply the correct current namespace policy through its trusted owner. Do not delegate selection to an IR checker. | Work items 3/11; the concrete production resolver and its selection/default rules remain prerequisites under #549. |
| Harness adapter | Own completeness and meaning of its native `reserved_env` and typed `policy_coverage` for that exact configured instance. Supply no claimed coverage or empty reservations as a substitute for unavailable facts. | Work item 15; the accepted harness protocol and each real adapter implementation. |
| Runtime/launcher | Own the complete names it assigns to the candidate environment for the selected configuration, including applicable enforcement settings. Authored workload keys and adapter-supplied inputs are not runtime reservations merely because they occur in the final environment. | Work item 14; the concrete complete projection is still missing under #549. No new Runtime protocol accessor is defined here. |
| Namespace policy/provisioning | Own the policy for the exact namespace and its current revision. Preserve existing policy authorization, provisioning and consistency requirements. | Work items 3/11/20; trusted provisioning/consumption remains a prerequisite. |
| Shared projection producer | Copy, validate and detach facts from the selected native owners into one immutable same-call view. Keep unresolved distinct from known-empty. Preserve the exact document site/selection association and each feature's completeness. | Server composition, shared by IR026 and IR019/IR028; no separate provider inventory per checker. |
| Feature checker | Consume the immutable view without loading providers, choosing defaults or calling native methods. Check applicable document/site/selection consistency before consuming facts. Produce known findings and explicit completeness; do not invent missing facts. | Work item 3; shared IR-owned input is compatible with ADR-0026, without an `ir` to `protocols` import. |
| Whole-validator/API composition | Combine feature findings and completeness with existing prerequisite failures. Refuse successful validation/admission when a required protection remains unresolved or inconsistent. Preserve the existing ownership of authored-input, unavailable-provider, transient-dependency and internal failures. | Work items 3/11; exact mapping of otherwise-unreported incompleteness remains unresolved under #549. |

Runtime selection/default resolution, native normalization and public failure mapping require their concrete accepted rules before executable activation. Naming an owner does not invent those rules or a working producer.

## 3. Consistency and incomplete facts

Facts must refer to the same invocation, namespace, authored agent site and selected configured instances as the document being checked. An internally consistent view still depends on trusted producer guarantees; matching names, hashes or local keys do not establish authenticity or current authority. Contradictory or mismatched supplied facts must not authorize successful validation. The concrete record shape and mismatch reporting belong to the implementing plan, preserving this fail-closed boundary.

Known-empty and unresolved are different states for every feature. A complete empty reservation set can establish no collision for that owner; a missing set cannot. Known failures from one owner remain reportable when another owner's facts are missing. A feature with no authored obligations does not prove that its provider is installed, supported or currently authorized. An empty diagnostic collection alone is never evidence that all required checks completed.

The whole validator must not return normal `valid: true` or admit deployment/start using incomplete required protections. Existing IR018, IR019, IR022, IR026, IR028 and API010/API011 meanings remain unchanged. This decision assigns no new error code or HTTP status and does not classify every unresolved internal state as an authored graph defect. If no accepted failure owner/mapping covers an incomplete case, that public integration remains unavailable until its concrete contract is decided.

## 4. Separation from execution authority

The same-call view is neither a credential nor a capability to execute. It supplies no persisted deployment interpretation, immutable execution-contract representation, lease, runtime grant, deadline, publication authority or recovery evidence. Deployment and canonical start retain their own saved-contract checks and fresh identity/policy comparisons under the existing contracts; a successful earlier validation cannot replace them.

The saved execution contract in [data-model section 11.1](data-model.md#111-graph-version-contract) remains a separate immutable interpretation. This call-local view is not adopted as its serialized format. Missing saved semantics still refuse new effects without importing today's defaults. Existing history and closure obligations remain unchanged.

## 5. Activation and remaining decisions

[#549](https://github.com/realkarych/kora/issues/549) continues to own the concrete producer/consumer prerequisites: exact selection/default resolution, complete runtime-owned environment vocabulary, harness name/prefix/case normalization, trusted policy required-kind/subagent classification, the `trust_repo_config` wording disposition, the exact immutable input/result records, and mapping of incomplete results not already owned by existing diagnostics. The earlier issue proposal's detailed DTOs are not approved by this narrower boundary decision.

Executable activation requires a real production source for every consumed fact and an approved implementation plan for its consumers. Provider registration metadata, testing/conformance composition and a fake harness's empty facts do not establish this prerequisite. No empty producer, fake-only inventory or unused checker skeleton is introduced to satisfy it.

The implementing issues must exercise exact selected-instance/configuration binding, same-call/document/site mismatch, complete-empty versus unresolved facts, partial known failures, current namespace policy, safe diagnostics and actual reference/resource release. Correctness and security reviews follow the changed boundary; performance is selected only for a concrete changed work/resource risk. This design creates no new numerical acceptance method or additional verification campaign.
