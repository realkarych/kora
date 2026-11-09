# Same-call trusted validation facts

The project owner selected this lifetime and ownership boundary on 2026-11-01 under [#596](https://github.com/realkarych/kora/issues/596), following the alternatives in [#549](https://github.com/realkarych/kora/issues/549). It is a normative design contract, not evidence of implemented provider selection, validation or execution. ADR-0009 governs protection requirements; ADR-0026 governs package placement. The exact internal record/API shape remains with its concrete implementation plan.

The native environment-reservation contract in section 6 was separately approved on 2026-11-01 under [#617](https://github.com/realkarych/kora/issues/617). Its source ABI, real producers and consuming validation remain pending implementation; the earlier same-call decision did not implicitly select that accessor or grammar.

The project owner approved provider-owned validated immutable configuration as a native declaration source on 2026-11-01 under [#620](https://github.com/realkarych/kora/issues/620). Section 5 records the implemented local capability source and its limits; configured selection, environment reservations and whole validation remain pending.

The project owner approved the public disposition of otherwise-unreported incomplete or inconsistent required facts on 2026-11-02 under [#628](https://github.com/realkarych/kora/issues/628). Section 7 records API010/API011 ownership and precedence without activating validation, deployment or execution.

The project owner accepted mandatory policy-coverage validation in trusted-repository mode on 2026-11-02 under [#630](https://github.com/realkarych/kora/issues/630). Section 8 records the ADR-0009 clarification, preserving the trusted-code bypass residual and first-slice refusal without activating the validator.

The project owner accepted the complete tool-kind vocabulary and conservative whole-layer coverage rules on 2026-11-02 under [#636](https://github.com/realkarych/kora/issues/636). Section 9 adds the residual `other_tool` kind and preserves native evidence and consumer activation as prerequisites.

## 1. Scope and lifetime

One validation invocation receives a detached immutable view of the facts from its selected configured harness and runtime instances. The view belongs to that invocation and the unchanged authored document being checked. It is not serialized, persisted, transferred between processes, cached across calls or reused for deployment/recovery authority. The #596 lifetime decision introduces no durable installation identity, database field, provider ABI member or public operation; section 6 separately specifies a future native accessor.

A native declaration receiver may be the selected provider's validated immutable configuration, sharing the declaration definitions used by its operational instance. Selection must retain the exact configuration association. Neither acquiring this receiver nor reading its declarations may initialize an execution host or inspect its resources. This permits effect-free inspection before opening a runtime without introducing a generic provider factory, a lifecycle state or another provider inventory. Validated configuration is not authentication of arbitrary Python objects or a sandbox for provider code.

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
| Whole-validator/API composition | Combine feature findings and completeness with existing prerequisite failures. Refuse successful validation/admission when a required protection remains unresolved or inconsistent. Preserve the existing ownership of authored-input, unavailable-provider, transient-dependency and internal failures. | Work items 3/11; section 7 defines failure disposition under #628. Its real integration remains pending under #549. |

Runtime selection/default resolution still requires its concrete accepted rules before executable activation. Public failure mapping follows section 7. Section 6 fixes native reservation spelling and completeness; its actual normalization and producer integration remain pending. Naming an owner does not invent a working producer.

## 3. Consistency and incomplete facts

Facts must refer to the same invocation, namespace, authored agent site and selected configured instances as the document being checked. An internally consistent view still depends on trusted producer guarantees; matching names, hashes or local keys do not establish authenticity or current authority. Contradictory or mismatched supplied facts must not authorize successful validation. The concrete internal record shape and consuming composition belong to the implementing plan; section 7 defines public failure disposition, preserving this fail-closed boundary.

Known-empty and unresolved are different states for every feature. A complete empty reservation set can establish no collision for that owner; a missing set cannot. Feature checks retain known failures from one owner for composition when another owner's facts are missing; section 7 determines whether a normal public collection may be returned. A feature with no authored obligations does not prove that its provider is installed, supported or currently authorized. An empty diagnostic collection alone is never evidence that all required checks completed.

The whole validator must not return normal `valid: true` or admit deployment/start using incomplete required protections. Existing IR018, IR019, IR022, IR026 and IR028 meanings remain unchanged. Section 7 assigns otherwise-unreported incompleteness to the existing API010/API011 operation-failure boundary; it introduces no new error code or HTTP status and does not classify unresolved internal state as an authored graph defect. Executable integration remains pending its real producers and consuming composition.

## 4. Separation from execution authority

The same-call view is neither a credential nor a capability to execute. It supplies no persisted deployment interpretation, immutable execution-contract representation, lease, runtime grant, deadline, publication authority or recovery evidence. Deployment and canonical start retain their own saved-contract checks and fresh identity/policy comparisons under the existing contracts; a successful earlier validation cannot replace them.

The saved execution contract in [data-model section 11.1](data-model.md#111-graph-version-contract) remains a separate immutable interpretation. This call-local view is not adopted as its serialized format. Missing saved semantics still refuse new effects without importing today's defaults. Existing history and closure obligations remain unchanged.

## 5. Activation and remaining decisions

[#549](https://github.com/realkarych/kora/issues/549) continues to own the concrete producer/consumer prerequisites: exact selection/default resolution and receiver acquisition, complete runtime-owned environment inventory, real harness declarations and normalization under section 6, native classification and executable policy requirements under section 9, the exact immutable input/result records, and integration of section 7's accepted failure disposition. The earlier issue proposal's detailed DTOs are not approved by these bounded decisions.

Executable activation requires a real production source for every consumed fact and an approved implementation plan for its consumers. Provider registration metadata, testing/conformance composition and a fake harness's empty facts do not establish this prerequisite. No empty producer, fake-only inventory or unused checker skeleton is introduced to satisfy it.

Under #620, `kora.plugins.runtime.local.configuration.LocalRuntimeConfiguration.capabilities()` supplies the existing local capability declaration from the strict immutable configuration. `LocalRuntime.open` retains that original configuration, and the operational runtime delegates `capabilities()` to it. Configuration schema and existing import paths remain compatible. Construction and capability inspection do not resolve the default state root, open files or a registry, start processes or executors, or require an event loop. The declaration does not establish host availability, service readiness, filesystem suitability, isolation or execution permission; normal opening retains those checks and may refuse a configuration whose capabilities can be inspected.

`test_local_capability_source_has_no_execution_effects` observes configuration construction and capability inspection in a fresh interpreter with no operational imports or running loop, unchanged live threads and no observed resource audit events; a real file read verifies the observer outside that phase. Python audit events do not cover every native operation, so source review also checks the complete construction/declaration path. `test_opened_local_runtime_uses_configured_capability_source` and `test_unopenable_local_configuration_preserves_pure_inspection` exercise real opening, closure and refusal; existing native capability and resource-release legs remain applicable. This source activates no environment-reservation accessor, snapshot projection, configured resolver or whole validator.

Under [#623](https://github.com/realkarych/kora/issues/623), the internal local `candidate_environment` helper implements construction from five explicit owners: runtime controls, workload values, admitted workload secrets, admitted harness credentials and adapter assignments. It captures entries once, refuses duplicate names before revealing secrets, and returns a fresh transient environment for its caller. Its real-process handoff test establishes assembly and exclusion of inherited variables, not production candidate-launch or harness conformance. It adds no reservation accessor or complete runtime inventory; the section 6 co-landing requirement remains unchanged. Actual `LaunchContext` integration, native harness declarations, configured selection and consuming validation remain pending.

Under #626, the native Claude configuration constructs its owned launch assignments without importing the operational SDK. A separate SDK integration checks its pinned native declarations and replaces the owned launch fields in real SDK options; tests integrate this with the local assembler and a real recorder process. This is an internal construction prerequisite, without a deployment schema, configured-provider registration, complete harness reservations, policy coverage or same-call projection. SDK connection, runtime launch/conformance, actual configured selection and every activation prerequisite in section 6 remain pending.

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

The shared producer validates and detaches declarations into literal names and prefixes for the same-call view. It receives no values. The configured-instance, namespace, document/site and invocation associations remain the composition's responsibility: a return value alone does not prove selection or freshness. Known authored collisions remain `IR026`; malformed or incomplete native facts cannot establish successful validation, deployment or start. Section 7 defines their public disposition using existing error codes.

Launch-time single-owner collision checks remain mandatory before launch. Early validation does not replace them, persist reservations, grant execution or authorize a changed installation. Saved-contract compatibility, current policy, grants, deadlines and recovery keep their existing owners and guarantees.

### Implementation and activation

The source accessor must land with its actual producer and candidate builder, provider conformance and consuming integration under a linked concrete plan. That plan must establish how validation obtains its configured receiver without opening an execution host merely to obtain facts. Both receiver acquisition and accessor invocation satisfy the no-initialization/no-I/O boundary; the current effectful `LocalRuntime.open` is not a validation construction path. A pure method on an effectfully acquired receiver does not satisfy this prerequisite.

Runtime ownership remains with work item 14 and [#184](https://github.com/realkarych/kora/issues/184)/[#196](https://github.com/realkarych/kora/issues/196)/[#199](https://github.com/realkarych/kora/issues/199) as applicable. Work item 15 owns each real harness declaration. Server selection/projection and whole validation remain under #549 and work items 3/11. This decision introduces no partial provider, unused checker, metadata cache, persisted field, public operation, dependency or new resource owner.

Future executable evidence must cover exact/prefix matching and every required mixed-case proxy spelling; malformed, missing, unavailable and complete-empty declarations; conditional controls; exclusion of forwarded author/adapter values from runtime ownership; configured-instance mismatch; no values in facts or diagnostics; effect-free receiver acquisition and invocation; agreement with actual candidate construction and its duplicate-owner refusal; and no successful validation while a required producer is incomplete. Real implementations and conformance supply that evidence, with applicable failure, cancellation and cleanup cases. No executable evidence is established by this design.

## 7. Public disposition of incomplete trusted facts

The whole validator owns the final completeness decision before constructing a public validation result. An empty diagnostic collection does not prove that required checks ran. Feature checks retain their scoped known findings and explicit completeness; only composition may decide that an accepted prerequisite failure makes a dependent check inapplicable.

A known prerequisite failure uses its existing diagnostic and precedence. An unavailable harness remains IR018; an unsupported or inconsistent saved type/behavior binding remains IR022; an established first-slice feature refusal retains IR028 or its already-defined more specific diagnostic. No missing native fact is automatically converted into one of these findings. Composition must establish that the existing finding actually owns the same failed prerequisite and dependent obligation. An unrelated diagnostic elsewhere in the document does not cover the missing check.

A required fact unavailable without such an established prerequisite finding produces API010/500: message `Internal operation failure.`, `retryable: false`, and empty `details`. This includes a missing accessor or `None` result when its required guarantee is otherwise unresolved. Malformed facts and inconsistent invocation, namespace, document/site or configured-instance association also produce API010. A complete empty native declaration remains distinct from unavailability and is handled according to the feature's existing semantics.

An API010 operation failure takes precedence over collected document findings: no normal partial validation collection is returned. Known findings remain available to feature-level composition, while an uncovered required protection prevents returning them as a normal public collection. Feature-local findings remain part of internal checking, but are not copied into the failure envelope or diagnostic logs. Ordinary invalid documents with established prerequisite refusals and no operation failure continue to return their normal invalid collection.

Only an explicitly recognized transient failure of an otherwise applicable dependency uses API011/503 and its existing fixed message. A generic missing fact, `None`, malformed projection or mismatched binding is not evidence of transience. Retryability follows the existing read, proven-pre-effect and operation-specific safe-retry rules; no new retry exception is introduced. Cancellation remains cancellation under its existing boundary, not a provider-failure response.

For validate, operation failure yields no `valid` result. For deployment, it creates no new graph version. For a new start, it creates no new accepted run or accepted/skipped receipt. Existing authorization and matching replay precedence are preserved: a durable accepted or skipped result is recovered/returned under its existing contract before current validation when that contract requires it. No failure erases or rewrites a durable result, and no uncertain commit is recast as proof that nothing happened.

This disposition carries no provider names, values, policy contents, graph fragments, paths or raw exception details. Existing bounded safe diagnostic ownership applies. It does not resolve provider selection/defaults, policy required-kind classification, native production completeness or saved execution-contract construction, and does not activate public endpoints or provider accessors.

| Combined state | Required disposition |
|---|---|
| An established prerequisite finding owns the dependent obligation; no operation failure exists | Normal invalid collection, preserving existing IR codes and precedence |
| A required fact is missing or unavailable without an owning prerequisite finding | API010/500, fixed message, nonretryable, empty details; no partial collection |
| Required facts are malformed or associated with another invocation, namespace, document/site or configured instance | API010/500 with the same content-free envelope |
| A known authored error coexists with an unrelated unresolved required fact | API010; that authored error does not cover the unresolved obligation |
| A native declaration is complete and empty | Apply existing feature semantics; completeness alone does not prove installation readiness |
| An otherwise applicable dependency has a recognized transient failure | API011/503 with existing safe-retry classification; no partial collection |
| A matching accepted or skipped receipt exists despite current validation failure | Preserve current authorization and return the saved outcome before current validation where the operation contract requires |
| Commit outcome is uncertain | Recover any durable result under the existing operation contract; an exception does not prove absence of effects |

Work items 3/11 and #549 own executable composition of this decision; #593 owns new-start integration. Their real activation tests must cover the cases above, current authorization refusals, cancellation and absence of new writes after a proved pre-effect refusal. Deployment/start writer cases run on both metadata databases. This design provides no passing runtime evidence and adds no verification gate or numerical method.

## 8. Repository trust and mandatory policy coverage

Under [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md), `trust_repo_config: true` does not waive comparison of required tool-policy call kinds against the selected configured harness's complete native declared coverage. An established uncovered namespace requirement remains `IR019`, an established uncovered node-only requirement remains `IR028`, and malformed policy remains `IR013`. Otherwise-unreported missing or inconsistent required facts retain section 7's operation-failure disposition and precedence; a trust flag cannot make those facts complete.

Where repository trust is supported, `allow_trust_repo_config` still controls permission to accept the existing residual: repository code can alter hook inputs, disable hooks or change harness settings. Passing coverage validation does not remove that residual, guarantee namespace denies or establish containment. First-slice installations continue to refuse `trust_repo_config: true`, including when the namespace permits it. Actual hook enforcement and real-interface conformance retain their harness/runner owners and acceptance requirements.

This decision resolves only the earlier trusted-mode coverage exception. Section 9 separately defines the complete-kind and conservative subagent requirements under #636. Complete native declarations and classification, current namespace policy, configured selection, detached records and executable composition remain prerequisites under #549. It introduces no classifier, provider API, public operation or execution authority.

## 9. Complete policy-coverage requirements

Every ordinary tool call exposed or executed by the selected harness, whether or not it reaches the policy hook, belongs to one of five primary operation kinds: `shell`, `file_edit`, `mcp_tool`, `hosted_tool` or `other_tool`. Harness-internal actions that are not tool calls retain their existing repository-trust ownership. The new `other_tool` kind is the residual category for calls established not to belong to the first four, including local read/search and delegation-control calls. It is not a fallback classification for unknown names. The selected harness's native normalization and accepted policy semantics determine classification; a validator-maintained guess about a provider name does not.

`subagent` remains an additional context requirement. A call made inside a subagent requires its primary operation kind and `subagent` coverage. A parent-session call that creates or controls a subagent remains an ordinary call in its own right. Every nonempty namespace or node policy layer requires `subagent` coverage conservatively; empty authored subagent configuration does not remove it. This decision introduces no native-absence exemption.

Required coverage is derived independently for each policy layer from its complete semantics. Every nonempty `allow` array conservatively requires all five primary kinds, as well as `subagent`, including when a pattern is a wildcard or its argument cannot match. For a deny-only layer, require every primary kind that its patterns may govern. Preserve existing name matching, MCP server shorthand and the different allow/deny edit-alias rules. Any file-edit requirement also requires `shell`, because the existing edit guards constrain shell calls. Arguments do not remove a name-level requirement. Existing protected-path checks retain their independently owned obligations, including the same shell guard; this decision supplies no new diagnostic origin for a protected-path refusal.

A pattern that cannot be soundly confined to a narrower set conservatively requires all five primary kinds. This rule concerns uncertainty about authored patterns within the exhaustive operation taxonomy. It does not convert missing or inconsistent native classification or coverage facts into a complete declaration. A native fact required to justify a narrower classification must be complete for the selected configured instance; otherwise use the conservative requirement only where the exhaustive native coverage contract itself is complete. Missing required native guarantees retain the existing incomplete-validation disposition.

The exact existing harness-output exception remains outside ordinary policy evaluation only when all its established conditions hold. An ordinary call named `StructuredOutput`, including a subagent call not qualifying for that exception, does not disappear from coverage. The existing IR013 prohibition on an exact authored `StructuredOutput` policy name and wildcard behavior remain unchanged.

Disabled or unsupported recognized tool surfaces do not erase existing coverage obligations. In particular, disabling Codex hosted search does not grant `hosted_tool` coverage. Under this conservative rule, its specified lack of hosted coverage rejects every nonempty allow array and any other policy requiring all primary kinds. Sufficiently covered deny-only policies remain possible. More precise admission based on impossible or independently blocked calls requires a separate explicit decision and real evidence; it is not inferred from an empty configured tool list.

A provider may declare `other_tool` covered only when its real-interface evidence establishes interception for the complete residual class under the selected supported version and configuration. Examples, a single read-tool test, fake declarations, the new type literal or a partial inventory do not establish that guarantee. Existing declarations do not gain the new kind automatically, and an absent kind is a known lack of coverage only when the declaration is complete under this contract. Legacy or incomplete declarations retain the accepted API010 disposition unless an established prerequisite failure owns that obligation.

Complete declarations missing a required kind produce IR019 for the namespace layer and IR028 for the node layer. Another layer, an empty node policy, permission mode or repository-trust flag cannot waive the requirement. Malformed policy remains IR013. Otherwise-unreported unavailable or inconsistent required facts retain API010/500 without a partial public result; API011 remains limited to recognized transient dependency failures. Current authorization and permanent accepted/skipped replay keep their existing precedence.

### Representation and activation

The shared `CallKind` type gains the single additive `other_tool` literal; the meanings of its existing literals and the `HarnessCapabilities.policy_coverage` set shape remain unchanged. This is a provider-contract extension, not a public HTTP operation, persisted format or automatic compatibility upgrade. No real or fake provider gains a coverage declaration in this change.

The shared validator remains the exclusive coverage-comparison owner. Native owners retain classification, normalization, completeness and real-interface evidence. The same-call projection retains configured-instance association and explicit incompleteness. No per-tool inventory ABI, dynamic discovery, provider factory, cache, dependency, new resource owner, classifier or admission path is introduced by this decision.

Work items 3/11 and #549 own the executable classifier and composition; work item 15 owns native classification and complete interception evidence; #593 owns start integration. The [decision and implementation-scope record](../research/policy-coverage-requirements-2026-11-02.md) distinguishes this shared vocabulary from those pending consumers.

## 10. Native Codex declarations

The project owner approved this declaration boundary on 2026-11-04 under [#677](https://github.com/realkarych/kora/issues/677). It specializes the provider-owned configuration pattern from sections 1 and 5. The [dated evidence matrix](../research/codex-native-validation-facts-2026-11-04.md) records the observed and pending inputs.

### Selected configuration and inspection

Trusted server composition selects the exact provider-owned, validated immutable Codex configuration from trusted installation inputs and reviewed native definitions. Selection binds the intended binary pin, execution installation and platform, and reviewed launch and control profile. The platform is that of the selected execution installation. Provider registration metadata, authored graph fields, candidate inputs and ambient settings cannot establish or alter the binding.

A profile bounds every allowed model, provider, mode, feature and override that can affect a declaration. A complete declaration covers every applicable branch of that profile. Reservations include every applicable reserved name or prefix across those branches. A kind enters the coverage set only if interception is established for that kind in every applicable branch that can exercise it. Section 9 governs disabled and unsupported tools. An unsupported or unverified branch cannot inherit completeness from a different selection. The pin and profile in this configuration describe the intended execution. The launch boundary independently verifies the actual binary and effective controls and refuses drift under its existing contracts.

Receiver acquisition and both reads are synchronous and use only already-validated inputs and the provider's native definitions. They require no execution host or running event loop. They must neither start `app-server` or invoke `codex --version`, nor inspect the process environment, open files or sockets, read host or repository configuration, resolve secrets or allocate runtime resources.

### Separate native results

| Result | Semantic value | Completeness |
| --- | --- | --- |
| `reserved_env` | `tuple[str, ...] \| None` | A tuple contains the complete selected Codex reservation names and literal prefixes using section 6's grammar. |
| `policy_coverage` | `frozenset[CallKind] \| None` | A set identifies the intercepted kinds for the complete selected profile under section 9. The completed native definition also supplies the operational adapter's `HarnessCapabilities.policy_coverage`. |

Each result carries its own availability. `None` means unavailable or incomplete. An empty tuple or set represents a complete declaration with no members and requires the same evidence as a nonempty declaration. A missing, unsupported or mismatched profile yields no complete result for an affected field. These are semantic result shapes for the internal boundary. The exact receiver acquisition, callable signature and shared projection representation belong to their implementing plans. Resume and usage declarations retain their separate evidence requirements.

The Codex provider shares the ownership definitions used by its configured environment and launch construction. Include applicable adapter-owned names and justified native controls, with each required case spelling. Keep runtime-owned controls in the runtime's declaration. Workload values, resolved secrets and forwarded adapter or credential values do not determine native reservation ownership. A reservation grants no authority to assign a name. Admitted harness credentials retain their insertion owner under DD-30, including `CODEX_API_KEY`.

Launch must refuse workload `env` and `secret_env` names that match a reserved literal or prefix before revealing secret values or starting Codex. This includes forbidden controls that the adapter leaves unset. The [environment ownership contract](protocols.md#3-harness-adapter-fr-21-fr-33-fr-66-fr-91-dd-27) separately requires duplicate-owner refusal. Unresolved ownership or applicable native behavior keeps the reservation result incomplete.

A complete coverage set supports conclusions about both present and absent kinds. Native tool classification and normalization remain separately required. Evidence of a particular covered or uncovered call in an incomplete profile cannot establish the complete declaration or supply an uncovered-kind finding under IR019 or IR028. Section 9 governs required kinds and coveredness; section 7 governs unresolved required facts and inconsistent bindings.

### Projection and activation

Server composition copies only the names or kinds into the immutable same-call view and binds each result to the invocation, namespace, agent site and exact configured selection. The projection preserves each result's availability and contains no credentials, environment values, raw configuration or provider objects. Sections 3, 4 and 7 govern consistency, execution authority and failure disposition.

Work item 15 owns the real native source, shared launch-construction definitions and conformance for the selected version, platform and profile. The evidence matrix identifies the current complete Codex reservation and coverage declarations as unavailable. The real source and the launch definitions it relies on must land together. Work items 3 and 11 and [#549](https://github.com/realkarych/kora/issues/549) own configured selection and projection into validation. [#199](https://github.com/realkarych/kora/issues/199) owns session hosting, and [#593](https://github.com/realkarych/kora/issues/593) owns the saved contract and canonical start. This decision introduces no provider ABI member, public operation, persisted format or executable producer.

### Selected first execution profile

The owner-selected [first Codex execution profile](codex-execution-profile.md),
recorded under [#769](https://github.com/realkarych/kora/issues/769) on
2026-11-05, uses API-key authentication and one app-server plus one selected
native exec-server under the existing runtime/runner lifecycle owner. Both
roles share the admitted candidate filesystem/workspace/policy binding.
The first Linux production direction under [#1220](https://github.com/realkarych/kora/issues/1220)
uses a runtime-owned relay between the attested app-server peer and the
private native executor stream. It admits one root turn per attempt.
Identity-preserving live reconnect or attempt failure and actual owner-bounded
listener/session/descendant closure remain implementing obligations.

The profile decision supplies no native receiver, complete inventory,
construction, declaration or consumer activation. The operative coverage and
refusal rules in sections 7–10 still apply. #549 owns a separate admission
amendment for any independently established impossible native subagent
context. That fact would require the exact qualified configured profile and
a separate effect-free source. The real-source, selection and platform gates
remain applicable.
ChatGPT account/OAuth support is deferred under
[#770](https://github.com/realkarych/kora/issues/770) until the first API-key
adapter is integrated and qualified through the common runner.
