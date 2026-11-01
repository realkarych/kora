# Explicit approval predicates in the response context

Date: 2026-10-31. Implementing issue: [#553](https://github.com/realkarych/kora/issues/553). Full capability: [#552](https://github.com/realkarych/kora/issues/552). The [implementation plan](https://github.com/realkarych/kora/issues/553#issuecomment-6538277367) and [actual design results](https://github.com/realkarych/kora/issues/553#issuecomment-6538326441) distinguish the deliberately partial library guarantee from full approval validation. Actual revision-matched source/results and final CI admission belong to the implementing PR; this record defines the methods and remaining boundaries.

## Meaning and activation

The entry point accepts a structurally valid `GraphDocument` whose caller-owned maps remain unchanged during its synchronous call. Root and every stored body are scanned once, including unused/shared definitions; invocation graphs are not expanded. Only authored human-input `approve_when` values are selected. Omitted predicates retain their separate eligibility owner.

The full language still permits `response`, `inputs` and `params`. A local exhaustive iterative AST scanner defers the complete expression when it encounters an identifier or comprehension binder named `inputs` or `params`, including dead branches. This avoids both unknown-identifier misclassification and a shadowing bypass from installing only `response`. Field names, quoted strings and literal string map keys do not become variable names. No fake variables or schemas are introduced.

The terminal partition is syntax, context, schema, typed. Parser failures belong to the existing syntax pass; mapping failures remain unresolved and do not create another `IR022`. Eligible expressions use one validation-local `SchemaTypes`, existing saved custom contracts and exact installed built-ins, the default PLAIN view and `check_typed` with `ResultKind.BOOL`. Only form omission selects `default_approval_form`; `{}` remains custom, and top-level form admission is still object-only. Boolean cases refer to admitted subschemas or resolved targets. Explicit predicates require no `decision` property.

The wrapper preserves the existing mapper/checker policy for references, single-schema `allOf`, enums, optional guards, nullable values and `dyn`. A `dyn` result passes static checking and still needs exact Boolean checking at runtime. A nullable Boolean or literal `null` result does not meet the required result type. No schema-truth or response-value admission is established here.

`predicates_checked` counts actual typed-check attempts, including diagnostic returns. The number seen equals that count plus the three unresolved counts. Scoped completeness requires all unresolved counts and omitted-diagnostic counts to be zero. Scoped validity additionally requires the bounded collection to be valid. Known fully reported type errors can therefore be complete but invalid. Any unresolved category makes both flags false even when diagnostics are empty; this is incomplete checking, not an invalid-DSL finding or an approval.

Full context production remains with IR/compiler work item 3 under #552, coordinated with public integration in work item 11. Closing the partial issue cannot close that capability. No public validator, deployment gate or approval transition may treat this foundation as completed approval validation. There is no native compilation/evaluation, saved rendering/projection contract, default insertion, response coercion, provider/metadata authority or API operation.

## Blocking boundary checks

`tests/ir/test_approval_predicates.py` exercises effective-form selection, explicit custom forms without a decision field, enums, optional guards, plain dynamic/null mapping, nullable-result rejection and exact Boolean checking. Reference variants use local pointers, saved custom contracts, exact built-in resources and single-`allOf` resolution without a new resolver. Separate cases cover unknown prerequisites and continued independent-node checking.

Context cases cover each AST shape, identifier and binder deferral, dead branches, and preservation of field/literal names. Parser refusals do not duplicate syntax diagnostics; a parsed expression whose typed estimate exceeds the existing budget produces owned `IR027`. Unicode code-point offsets, exact operational paths, safe owning-node sources and bounded count/byte omissions exercise the shared diagnostic boundary. Stored/unused bodies and omission ownership retain distinct counter and unchanged-input assertions.

`tests/ir/test_approval_predicate_resources.py` uses real graph, mapper and checker implementations. Caller-owned document/schema/source weak references must clear while the scalar/diagnostic result survives. The existing owned-class baseline method includes raw preflight AST nodes, `Environment`, `Variable`, mapper, check state, `CheckedExpression` and `Record`, across success, type error, syntax failure, context deferral and schema failure. Result and copied diagnostics must also release after their owner drops them. This checks actual ownership without introducing a process heap/RSS or elapsed-time acceptance threshold.

Shared/deep saved-form cases retain instrumented mapper/checker-step assertions. Mapping is memoized within one call; checker preparation may still revisit shared reachable type/bound metadata per expression. These counters do not claim global linear total work, CPU time, latency or a native memory envelope. No separate benchmark or calibration campaign is selected.

The existing installed-core-wheel check imports the entry point and exercises valid, refused and incomplete results without provider/protocol or native-binding imports. Strict checks, architecture/import/no-suppression checks and one complete final-head supported-platform CI remain required; local results are not a substitute for native final-revision admission.

## Diagnostics and threat boundaries

After successful preflight, typed CEL diagnostics, including cost and canonical-rendering refusals, are converted with `from_cel(exact_expression_field=True)`. Their fixed code/message, exact `config.approve_when` path, code-point offset and revalidated node-only source survive the shared copied bounded collection. Raw tokens, schema fields/enum values and type representations never become diagnostic messages or path segments. Unexpected failures remain caller-owned.

The single INFO completion event contains only status and scalar accounting. `test_explicit_approval_logging_excludes_content` exercises forbidden nonsecret content at DEBUG, source/schema/reference/enum/expression canaries and stdout exclusion. `test_explicit_approval_survives_unavailable_sink` uses real blocked/broken pipes, existing queue bounds and actual writer shutdown while preserving returned results.

TM-04/TM-10 cover hostile expressions/schemas, existing parsing/checking capacities and transient ownership. TM-06/TM-07 cover content exclusion. Saved-contract identity and runtime approval authority remain unchanged boundaries; no threat-model actor, trust assumption or control amendment is selected. Independent correctness and security cover these risks. Tests and source review do not establish runtime enforcement or payload validity.

## Audit provenance

The retained master audit [41025696626](https://github.com/realkarych/kora/actions/runs/41025696626), source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`, is partial because of its budget. It executed 165 mutations with zero reuse: 70 killed, 18 survived, 77 test errors and 315 pending. The declared pricing/CEL-path scopes supply no typed-checker/schema coverage for this wrapper. These outcomes are not relabeled passing, and no PR mutation campaign or score gate is added.
