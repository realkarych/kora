# kora MVP design — the YAML intermediate representation (IR)

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: the canonical graph representation that `kora compile` emits, `kora validate` checks and the scheduler consumes (FR-1.2–1.7, FR-2, FR-3.2, FR-6.3–6.6, NFR-6). The machine-readable contract is [ir.schema.json](ir.schema.json), published at a stable `$schema` URL and registered in SchemaStore so editors validate `*.ir.yaml` files; worked examples are in [examples/](examples/). Design decisions taken by the owner on the choices the requirements left open are recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md) and referenced here as **DD-n** ("design decision n").

CEL cost-regression amendment dated 2026-10-23: the project owner approved lightweight validation and a 100 ms target under [issue #167](https://github.com/realkarych/kora/issues/167#issuecomment-6351849696); the corresponding human-facing documentation wording was reviewed line by line and approved by the project owner on 2026-10-23.

The committed schema is generated from the real `GraphDocument` model by `kora.ir.schema.generate_ir_schema` and `render_ir_schema`. The dialect and public `$id` are unchanged. Local `$defs` and references use the real Pydantic model/type names; the unreleased IR 1.0 does not preserve the former handwritten fragment names as compatibility definitions. Scalar and literal constraints may be inlined in model properties. Every generated reference resolves inside the schema; generation reads no committed schema and performs no network or provider lookup. The complete byte-drift test preserves source-owned annotations as well as constraints.

## 1. Design rules

- The scheduler consumes materialised node configuration (FR-3.2) and the expressions in [section 7](#7-expressions-and-templates). The general compiler runs authored Python in the author's process. The [early compiler](dsl.md#early-sequential-json-mode-issue-1918) parses source without running it.
- Edges use stable string IDs and named references (FR-1.5). Node IDs match `^[a-z][a-z0-9_]{0,63}(?![\s\S])` and are unique within the root node map or one stored body's node map. Independent maps can reuse an ID.
- `graph` holds topology and node definitions, `runtime` holds graph-level policy and configuration, and `secrets` holds references only (FR-1.3). Compiler provenance belongs to `meta`.
- Compilation emits deterministic output with schema-ordered keys, source-ordered lists and no generated timestamps (NFR-6.2).
- `ir_version` follows format semver (NFR-6.3). Minor revisions add optional fields, and major revisions can remove or retype fields. `graph.version` is the author's informational version, distinct from control-plane version numbers.

## 2. Document layout

```yaml
$schema: "https://kora.dev/schemas/ir/1.0.json"
ir_version: "1.0"             # format version
meta:                         # written by the compiler, informational; the scheduler never reads it
  compiler: "kora 0.1.0"
  source_digest: "sha256:…"   # digest of the Python sources that produced this file
  presets: { <preset_id>: { … } }     # snapshot of each preset as used at compile time, for diffing and provenance
  defaults: { … global and graph layers … }
type_contracts:
  VcsRef: { builtin_revision: "1" }
  StructuredOutput: { builtin_revision: "1" }
graph:                        # section (a): topology
  id: sql_optimization        # graph id, stable across versions
  version: "3"                # the author's own graph version, informational
  namespace: dwh_core
  description: "…"
  params: { …JSON Schema… }   # run parameters accepted by triggers
  triggers: [ … ]             # section 8
  overlap: allow              # allow | skip | queue (DD-12); when absent: queue for webhook-fired runs, allow for every other trigger kind
  target:                     # optional (FR-6.11, ADR-0014): the business target a run works on and the per-target policy, section 8
    key: { from: find_target, output: target, path: task_id }   # or { param: target_task }: a field of a node output, or a run parameter
    on_active: defer          # allow | defer; when absent: allow
  nodes: { <node_id>: <Node> }
  subgraphs: { <subgraph_id>: <Subgraph> }   # reusable bodies for subgraph and fan_out nodes
runtime:                      # section (b): policy and infrastructure
  runtime: docker             # runtime protocol implementation, by name
  vcs: git
  artifact_storage: s3        # artifact storage implementation: local (standalone, development) | s3 (any S3-compatible service) in MVP (FR-4.4)
  approval_channels: { vcs: { provider: github, accept_merged_as_approval: false, feedback_from: collaborators_write, request_reviewers: ["dwh-reviewers"] }, cli: {} }   # DD-35; request_reviewers notifies, it does not authorise (ADR-0025)
  concurrency: { max_running_nodes: 20 }
  limits: { timeout: "72h", budget_usd: 200 }      # whole run
  stall_after: "15m"          # runtime-level default for agent-loop silence; nodes override in limits (DD-15)
  provider_retry: { max_wait: "6h" }   # model-provider limits: total waiting allowed before the node fails with provider_limited; enabled-feature namespace default when absent; slice 1 resolves omission to max_wait "0s" (ADR-0024)
  retention: { transcripts: "30d", artifact_payloads: "30d" }   # graph-level, namespace default applies when absent (DD-4)
  protected_paths: ["Makefile", "tests/**"]     # repository files no agent node may edit; verified before every command gate (DD-34); gate scripts travel as command resources (section 4)
secrets:                      # section (c): references only
  aliases: { SERVICE_TOKEN: "secret://env/SERVICE_TOKEN" }   # every reference must fall inside the prefixes the namespace is allowed (IR007)
```

`GraphDocument` selects eight node envelopes by `kind`.
`GraphBody` and `Subgraph` carry node maps, and `Graph` owns the stored body registry.
Structural parsing preserves omissions of optional fields and inserts no role outputs, predicates, overlap defaults or scheduling settings.
Timer and branch nodes can carry authored output declarations at this layer, while semantic validation refuses nonempty declarations before execution.

The parser discards unknown root keys without visiting their values.
`inspect_ir_root` counts them before nested validation and excludes bounded root `x-` extensions.
A successful parse retains only the private `unknown_root_key_count`, outside the schema and export.
The shared validator must warn on a nonzero count (FR-1.5).
It owns the public diagnostic codes and collection bounds.
Typed records refuse unknown fields, with bounded `x-` extensions available on root and nested records.
Named maps validate every entry, and embedded schemas and payloads follow their own rules.

Only wire `$schema` binds the optional schema field.
The parser treats Python spelling `schema_` as an unknown root key.
The field preserves its strict string as authored.
`format: uri` is a schema annotation and supplies no normalization, URI admission or resolver authority.

`Provenance` keeps the typed compiler and source digest, raw partial preset and default snapshots, and arbitrary detached JSON members including `x-` names.
A reviewer can compare preset snapshots and a UI can show provenance.
For execution, the scheduler reads the node's resolved `config` and takes no input from `meta`.

### Graph parameter schema omission

The [first-slice parameter evaluation profile](first-slice.md#graph-parameter-evaluation-support) adds an installation support check for the effective schema. The full structural IR, the exact authored schema and the omission rule remain the source of parameter meaning.

The project owner accepted this contract on 2026-11-03 under [issue #642](https://github.com/realkarych/kora/issues/642). When `graph.params` is omitted, its effective parameter schema is exactly:

```json
{"type":"object","properties":{},"additionalProperties":false}
```

A new start therefore accepts only an empty parameter object. An explicitly authored `graph.params: {}` keeps ordinary JSON Schema semantics and the existing plain CEL `dyn` view; it permits arbitrary keys within the API's JSON-object parameter envelope. Other authored schemas keep their existing interpretation. Omission remains absent in the authored IR and canonical export. This rule does not insert the effective schema into that document, equate its identity with an explicitly authored schema, or default, coerce, remove or insert parameter values. It makes no additional decision about defaults in authored schemas.

Root nodes and stored bodies use the same graph-level effective schema. The existing plain mapper maps the closed-empty schema to an empty record without additional fields. Dot access and literal-key indexing of an undeclared field retain `unknown_field` / `IR011`; dynamic-key indexing retains `dyn` and a missing key remains the evaluation error `missing_field`. All other expression, projection and result rules in section 7 remain unchanged. This is not a static prohibition on every expression involving `params`.

The rule belongs to the pinned execution interpretation, not namespace or provider defaults. Under [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md), the project owner accepted the explicit `kora.graph-parameters.v1` compatibility binding within `kora.execution.v1` on 2026-11-03 under [#656](https://github.com/realkarych/kora/issues/656). [Data-model section 11.1](data-model.md#graph-parameter-interpretation) requires `execution_contract.parameters` to save the interpretation and exact effective schema, preserving original reference roots and existing deployment identities. Missing or inconsistent saved bindings cannot acquire current meaning; old history and existing replay/closure obligations remain preserved. The [physical graph-value format](data-model.md#graph-value-physical-encoding) uses these canonical JSON bytes as logical identity: valid surrogate pairs and their astral character coalesce, lone surrogates remain escaped, and colliding decoded names refuse preparation. Future complete validation and contract construction must consume that same logical view. The codec does not establish whole-contract validity or execution authority; complete producers, interpretation readers and consumer activation remain pending under their named owners.

The approval predicate pass consumes this effective schema under [issue #644](https://github.com/realkarych/kora/issues/644), as described in section 10. The partial target-parameter pass still leaves omitted schemas unresolved. Other effective-schema producers and consumer integration remain pending. [API start and rerun rules](api-and-agent-surface.md#admission-state-and-bounded-starts-adr-0020) preserve authorization and durable replay. [The decision and activation record](../research/graph-parameter-omission-2026-11-03.md) names the remaining implementation owners and checks.

### 2.1. Authoritative type contracts (ADR-0018)

IR 1.0 uses `kora.execution.v1`.
The required `type_contracts` map carries every used base type and its complete parent and reference closure.
A graph with no artifact types uses `type_contracts: {}`.
`List[T]` derives its contract from `T` and the fixed list semantics without a separate registry alias.
The offline compiler emits author-defined contracts, explicit `$schema` and installed immutable built-in revisions.

A built-in entry uses `{builtin_revision: "1"}` and resolves to that revision's immutable schema and behavior descriptors.
The [built-in catalogue](artifacts/README.md) publishes the schemas and validation, digest and summary bindings for VcsRef and [StructuredOutput](artifacts/StructuredOutput.md), the execution evidence for [CommandResult](artifacts/CommandResult.md), and the transcript-primary representation, complete logical-output replay and `kora.agent-session-view.v1` policy for [AgentSession](artifacts/AgentSession.md).
Derived `List[CommandResult]` and recursive scheduler collect/export aggregates await work items 3, 9–12 under the generic saved list rules.
Per-result, upload and CEL limits establish no list-wide bound.

A custom entry requires `schema`, `parent` (a type name or null) and `behavior` with exact `validation`, `digest_fields` and `summary` identities.
These identities name immutable behavior declared by providers.
The trusted installation resolves the registered binding and verifies the exact identity, refusing an unavailable or mismatched binding without running author code or fetching an implementation.
The generic identities are `kora.json-schema-2020-12.v1` for validation and `kora.json-payload.v1` for whole-payload digest selection and the identity summary.
Custom summaries bind the provider's immutable identity to the configured entry point.
Selected fields, projections and summaries are stored when produced under [the artifact type protocol](protocols.md).
Identity summaries remain content-bearing payloads.

Schemas use JSON Schema draft 2020-12.
Local references keep their original document root.
Cross-type references and built-in `$id` use `urn:kora:artifact:<name>` with `:` as the namespace separator, as in `urn:kora:artifact:dwh_core:Candidate`.
Deployment builds its in-memory resolver from the saved closure alone.
The resolver uses no runtime network lookup or mutable registry alias.
Disallowed external references, unresolved parents or targets, parent cycles, unsupported dialects and unavailable behavior identities refuse with `IR022` at the affected path.
Schema validity, closure and assignability require the semantic passes in [section 10](#10-validation-rules-kora-validate).

Deployment checks namespace ownership and the whole closure, expands built-in revisions and includes inline output schemas in `graph_version.execution_contract`.
It keeps the embedded definitions by value under [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md).
Registration changes namespace aliases for future authoring only.
The [version identity](data-model.md#graph-version-digest-inputs) is the digest of canonical `{ir, execution_contract}`, with separate `ir_digest` for the compiled document.
The same complete deployment is idempotent within its authorized graph.
Different resolved resource or type behavior requires a different version identity.
`meta` remains informational.

CEL checking, edge assignability, worker output and upload validation, branch and collection evaluation, and content-bound approval digests use the saved closure.
Scheduler-only nodes acquire no physical attempt row.
Execution must support every required saved built-in revision and custom behavior under the selected semantics.
An unavailable interpretation must fail before the affected operation.
Historical reads use saved results.
Declared or observed skill, MCP and resource identities travel with their existing owners under #45 and #48.

Type names are built-in or namespace-qualified base names, with recursive `List[T]` wrappers where needed.
The outer JSON Schema checks spelling without a nesting limit or a bracket-balance guarantee.
Semantic validation checks balance at every depth and reports `IR003`.
Static fan-out uses the saved closure and iterative spelling parser for nominal checks, leaving payload admission and execution to their own activation rules.

#### Exact native builtin effective contracts

The [saved native builtin format](data-model.md#saved-exact-native-builtin-output-contract), accepted under #675, covers exact revision-1 AgentSession, CommandResult and VcsRef direct roles when their output declaration omits `schema`. It preserves complete native validation definitions separately from checker interpretation. AgentSession saves the exact published permanent metadata policy and checker schema by value, with their own original checker root; its full logical schema never grants CEL access to excluded fields. VcsRef/CommandResult save an explicit null checker binding for their unchanged ordinary declared-schema interpretation under `kora.execution.v1`. Missing fields do not inherit defaults. Inline/custom/list/wrapper/export cases retain their separate complete-composition requirements without changing IR validity or losing authored constraints. The format, native package sources, real construction/checking and consumer activation remain distinct obligations.

#### StructuredOutput effective contracts

The accepted [v2 standard-schema contract](schema-validation.md) under #1577 specifies the replacement evaluation policy and complete saved-policy reader dispatch. It leaves original schemas, source roles, canonical identities, checker selection and structural IR representability unchanged. New executable publication requires the complete selected implementation and support certificate; existing saved v1 values keep their original interpretation and do not acquire v2 defaults. The preceding schema-only/generic behavior identity is composed with the exact saved evaluation policy, never treated as permission to run stock float validation first. The complete #1595 graph writer verifies source support and required installed behaviors before saving v2; checking readers preserve the selected saved policy without depending on today's behavior map.

`kora.execution.v1` supports the previously unbound StructuredOutput revision 1 through the immutable compatibility binding `kora.structured-output-view.v1`. Its scope is that exact revision, custom types whose saved ancestry reaches it, and corresponding derived lists. Save the binding, resolved parent/reference closure, every applicable validation schema and its source, selected checker schema, behavior identities and all parameters in the effective output contract. Installations must support that exact binding. A historical record lacking it cannot infer support from current defaults: refuse new effects before allocation/execution while retaining saved historical results. Unrelated supported interpretations remain unchanged.

[ADR-0027](../../docs/decisions/0027-bounded-effective-schema-validation.md) selects the common pure composition and restricted in-process evaluator described in [StructuredOutput section 3](artifacts/StructuredOutput.md#3-saved-effective-schemas-and-cel-view). The preparation-only prerequisite amendment approved under [#498](https://github.com/realkarych/kora/issues/498) permits one pure engine-neutral `kora.ir` operation over an exact resolved native input DTO. Internal work/auxiliary accounting, canonical scalar primitives, sorted indexes and owned flat-arena admission/encoding landed under [#500](https://github.com/realkarych/kora/issues/500). [#501](https://github.com/realkarych/kora/issues/501) implements and exports `prepare_structured_schema_input`, `StructuredSchemaPreparationInput`, `PreparedStructuredSchemaInput`, `StructuredSchemaPreparationAccounting` and `StructuredSchemaPreparationRefusal`. The operation prepares original schemas/sources/descriptors, exact closed original targets, constraint/checker roles and an acyclic potential-call graph for a selected non-list type whose ancestry ends at exact StructuredOutput revision 1. [The normative preparation contract](artifacts/StructuredOutput.md#31-reserved-execution-policy-and-prepared-ownership) fixes the exact input fields, complete normalized representation, ten inclusive capacities, pre-allocation reservations, deterministic phases and three fixed refusal reasons. The caller retains prior schema/declaration validity, saved-record authenticity and installed-behavior authorization. Structural IR subclasses gain no implicit native-input exception. The opaque prepared result exposes only safe accounting, with no payload-validity or extraction capability; no engine or consumer is activated. Its caller-bound DEBUG completion event publishes fixed status flags and safe counts, with fixed zero placeholders rather than actual counters on refusal. [Focused results and pending final verification](../research/structured-schema-preparation-2026-10-30.md#complete-operation-evidence-under-501) remain distinct from the first child's evidence.

Each affected future effective contract must save `schema_validation.policy` and its complete `parameters` map. [#515](https://github.com/realkarych/kora/issues/515#issuecomment-6533958110) approves the exact thirteen-parameter `kora.structured-output-schema-policy.v1` binding and the complete deterministic resource/numerical/handler/adapter schedule in [StructuredOutput sections 3.1–3.3](artifacts/StructuredOutput.md#31-reserved-execution-policy-and-prepared-ownership). The ten preparation capacities and algorithm remain unchanged. The three additional inclusive support capacities are `max_numeric_intermediate_bits: 16384`, `max_evaluation_work: 16777216` and `max_evaluation_depth: 64`; the last counts active schema frames separately from equality frames. These are not time or Python heap/RSS guarantees. The internal numerical foundation implements exact scalar integer classification, ordering/equality and positive-divisor divisibility with a shared monotone evaluation meter and precharged allocation. Its modules are internal `kora.ir` implementation, with no broad public export, schema evaluator, acceptance value, provider entry point, extraction or production consumer. The exact complete binding is checked before explicit projection of its unchanged ten preparation keys; missing/extra/defaulted parameters refuse. The v1 identity remains reserved and unavailable for effective execution until recursive equality, every supported bounded handler, safe pinned-engine adaptation, complete evaluation and common composition meet their named implementation and verification gates. Missing/unsupported policy cannot inherit current defaults. The existing deployment writer includes the exact future policy in existing contract/deployment identities; no second digest or persisted format is introduced. Preparation and numerical primitives change no structural IR admission/export or frozen artifact bytes. [Numerical evidence and remaining checks](../research/structured-schema-numbers-2026-10-31.md) remain distinct from full evaluator readiness.

For corresponding derived lists, structural validity and individually admitted items do not activate execution. The [StructuredOutput aggregate prerequisite](artifacts/StructuredOutput.md#1-payload-limits-and-immutable-identity) requires owner-approved finite, immutable list-wide canonical-byte, depth and item-count semantics saved in the effective contract, plus the [named conformance/resource gate](artifacts/StructuredOutput.md#5-verification-and-activation), before activation. It covers recursively nested lists and scheduler collections/exports that bypass upload; existing CEL projection and upload bounds are insufficient. No numerical aggregate default or whole-list 1 MiB policy is inferred.

Every applicable schema validates the same direct value without coercion, normalization or inserted defaults. For an exact built-in StructuredOutput output, an applicable inline schema supplies its checker view; without one the view is `dyn`. A custom descendant or derived list retains its saved declared/derived checker schema: inline constraints cannot introduce dot-access fields, change their types or make them required. A different static view needs a named type. Do not send a manufactured multi-schema `allOf` to the checker; that would erase declared fields to `dyn`. The effective contract digest binds both validation constraints and checker view ([section 7.4](#74-schema-driven-checker)).

Constraints run in saved parent-to-declared-to-inline order without duplicate source occurrences. Reference-closure membership supplies exact original targets rather than adding top-level assertions; equal document bytes retain distinct local bases/source ownership. The common composition preserves inherited admission and all required bound behavior, uses exact canonical-decimal schema semantics for affected generic validation, and permits effective extraction only after a complete valid result. Unsupported/exhausted evaluation is refusal, never schema-false. Original identity remains separate from prepared views; [the preparation contract](artifacts/StructuredOutput.md#31-reserved-execution-policy-and-prepared-ownership) defines detached ownership and closed target authority.

For the one agent-produced structured output, the owner-approved [source mapping under #669](data-model.md#inline-origin-and-direct-producer-selection) selects `config.output_schema` when present, otherwise authored `outputs.<name>.schema`, otherwise no inline schema. Explicit empty objects count as present. When both fields exist they must be structurally equal as JSON, ignoring object order, or fail with `IR003`; the exact config value, object order and source are retained. The declaration-only case is an explicitly selected immutable compatibility mapping for a previously unspecified case, not an inferred fallback or historical backfill. Future harness/worker composition must use this selected effective producer schema before activation. With no declared structured output, existing harness-only `output_schema` and ReviewBundle narrative handling remain unchanged and grant no artifact authority. Human-input responses retain section 4.2's exact effective-form equality rule; wait-event `outputs.event.schema` supplies its inline schema; command `output_file` validates the declared effective contract. `List[T]` retains its distinct derived semantics and saved item contracts. Descendants inherit StructuredOutput admission and canonical rules; list items inherit those rules relative to each decoded item without inventing independent raw bytes or a new whole-list cap.

The #501 preparation scope supplies no compiler/checker/provider integration or effective validation. The [activation table](artifacts/StructuredOutput.md#5-verification-and-activation) distinguishes primitive evidence from the complete operation's own pending checks and later evaluator/consumer resource bounds.

#### AgentSession derived-list activation

Derived `List[AgentSession]`, recursively nested lists containing it and scheduler collect/export aggregates are unavailable until the [AgentSession aggregate prerequisite](artifacts/AgentSession.md#derived-list-aggregate-activation) is met under unchanged generic saved list semantics. Work items 3/9/10, coordinated with API 11 and artifact/storage/read owner 12, must approve and save finite full-aggregate canonical-byte, depth, item-count and admission semantics and prove bounded materialization, canonicalization, digest/summary generation, persistence and command/public-read delivery before activation. Worker 17 and #216 supply actual transcript/sample retained-owner propagation where applicable. Admission precedes full aggregate allocation, including production that bypasses upload. The per-result 4 MiB/depth-16, upload and CEL projection bounds are not aggregate bounds. No new aggregate numerical default, quota or campaign is selected.

### Structural declarations

`InputRef` requires `type` and preserves omission of the producer, output, path and optional flag.
`OutputDecl.schema` and custom contract schemas require JSON objects and permit nested boolean schemas.
A custom contract requires the literal draft 2020-12 `$schema`, a required nullable `parent` and nonempty behavior identities.
Built-in revision and behavior availability, schema validity, recursive type balance and assignability belong to semantic validation.

`IrSourceLocation` preserves the authored `file` string, positive JSON integer `line` and `x-` metadata without opening a path.
The shared validator owns conversion to safe diagnostic `SourceLocation`.

Patterns for versions, type-contract names, retention periods, secret references and artifact types cover the entire string and refuse trailing newlines.
Secret alias names follow environment-name rules, and approval-channel names are unrestricted strings.
Named maps treat `x-` names as ordinary entries.
Runtime parsing preserves omitted concurrency, retention and retry settings.
`provider_retry` accepts record extensions and preserves omitted `max_wait` for the consumer to interpret under first-slice disabled-policy normalization and provider support rules.

## 3. Nodes

Every node has the common envelope below; `config` is kind-specific (section 4).

```yaml
nodes:
  implement:
    kind: agent
    description: "Implement one hypothesis on an isolated checkout"
    source: { file: "graphs/sql.py", line: 23 }        # NFR-10.3
    inputs:
      hypothesis: { type: Hypothesis, from: $inputs, path: hypothesis }   # inside a fan-out body: the mapped element
      previous_attempt: { type: PreviousAttempt, from: $previous_attempt, optional: true }
    outputs:
      change: { type: VcsRef }
      session: { type: AgentSession }                   # implicit for agent nodes, listed for clarity
    depends_on: []                                      # control-only edges (no artifact)
    trigger_rule: all_success
    retry: { … }                                        # section 6
    limits: { timeout: "2h", max_events: 200, budget_usd: 15, stall_after: "20m" }
    attempt_invariant: false                            # FR-2.7a memoisation flag
    independent_of: []                                  # inputs whose new versions do not invalidate this node (FR-6.8a)
    protected_paths: []                                 # node-level addition to runtime.protected_paths (DD-34)
    config: { … }
```

**Edges** are the union of `inputs[*].from` and `depends_on`. An input names the producer node and one of its declared outputs; the validator checks that the producer's output type is assignable to the input type (the same saved type contract, or a subtype through the graph version's saved parent closure, FR-4.3 and ADR-0018); a subtype keeps the CEL type of every parent property, so it cannot narrow `number` to `integer` (section 7.4). For a whole-artifact connection, list types are covariant: `List[Produced]` is assignable to `List[Expected]` when `Produced` is assignable to `Expected`, recursively. Both endpoints must have equal list nesting and valid saved base ancestry; a list and a non-list type, different depths, reversed ancestry, siblings and unrelated bases are incompatible. Missing or malformed endpoint declarations remain prerequisite failures rather than established incompatibility. This nominal rule does not materialize lists or validate their payloads. Reserved sources: `$params` (run parameters), `$previous_attempt` (section 5), and `$inputs` inside a subgraph body (the subgraph's declared inputs, by `path`; in a fan-out body the mapped input resolves to the current list element). There is exactly one way to feed a body: the `inputs` of the `subgraph` or `fan_out` node. The missing-input rules below decide unavailable required inputs and the permitted explicit absence of an optional input after its current producer finishes unsuccessfully.

**Input independence** (FR-6.8a, ADR-0007): `independent_of` contains names from this node's `inputs`, not producer IDs. Unknown names fail semantic validation with `IR023`. It preserves only an already successful outcome on the affected input edge; any other affected input or control edge still invalidates the node. Consumers without a successful result are reevaluated. Runtime invalidation resolves declared edges even before input binding, through body input forwarding and exports/collection, together with current invariant-reuse bindings ([data-model section 6](data-model.md#6-clear-downstream-fr-68a)). It never waives a branch/control gate.

**Outputs** declare names resolved exclusively through the compiled `type_contracts` and the deployed closure, not current registry aliases (ADR-0018). Built-in names: `StructuredOutput`, `VcsRef`, `AgentSession`, `CommandResult`, `ReviewBundle`, plus `List[<type>]` for fan-out results and user types registered under `namespace/Name`. The `VcsRef` payload follows [revision 1](artifacts/README.md#2-vcsref-publication-facts): immutable publication facts, required pushing principal and optional code-host facts; current status/head observations remain separate. A [StructuredOutput](artifacts/StructuredOutput.md) payload is the direct JSON value, including a scalar or produced null; its schema belongs to the saved effective contract (section 2.1), without a `{schema, data}` wrapper. **Agent output contract** (DD-36): an agent node declares at most one `StructuredOutput`-derived output and at most one `VcsRef` output besides the implicit `session`; the structured output's inline contract schema is selected by section 2.1's four-case source rule, including its equality requirement when both fields are present; the worker publishes the workspace only when a `VcsRef` output is declared, and a declared `VcsRef` with no diff fails with `artifact_invalid`. The [AgentSession revision-1](artifacts/AgentSession.md) primary payload is the opaque harness transcript. Its separately frozen logical value binds session metadata, normalized finalization usage/cost snapshot, denial sample and immutable source/retained transcript descriptor. The schema describes that logical validation value, not a primary metadata-JSON payload or caller-authoritative storage reference. Transcript storage identity, complete logical replay and approval-selected identity remain distinct. A [CommandResult revision-1](artifacts/CommandResult.md) payload is the complete command execution record: observed exit/signal, stop reason and termination, monotonic integer-millisecond duration, exact trusted repository/candidate/base identity, the protected paths and verification result, the complete verified command-resource manifest/set digest and resource protection, and separately hashed stdout/stderr captures with observed counts, EOF/truncation facts and exact bounded binary tails. Protected-path or resource-integrity refusal before launch produces no result; unknown exit/capture facts never become placeholders. Optional JSON from `output_file` is only the separately declared typed output. The complete result enters approval selection; metrics in another artifact need their own explicit binding. A well-formed result never substitutes for trusted provenance or closure evidence. All of them enter the `ReviewBundle` facts.

A **`ReviewBundle`** (DD-33) has two parts: `facts`, written by kora from its own records and read-only to agents (every input artifact of the bundle node with type, digest, producing attempt, worker and image; gate results from `CommandResult` inputs with exit codes, protected paths, base commit and verification result, so that each gate is labelled as verified against its protected paths or unprotected; metrics references; the `VcsRef` with commit; the run's attempt cost), and `narrative`, the only part the agent node writes (`summary`, `rationale`). Approvals bind to the digests of `facts`; `kora web` and the change-request comment render the two parts visibly apart, each gate with its label. There is no node kind for building artifacts (DD-8, ADR-0012): packing a `Candidate` or selecting the best candidate by a fixed formula is a command node over its materialised inputs (section 4), because a copy and a formula need no model and must be reproducible; writing the `ReviewBundle` narrative, or a selection that needs judgement, is an ordinary agent node on a cheap model from its typed inputs. kora itself attaches the aggregated cost of all attempts to the approval request (section 4.2); the agent never computes cost.

**Trigger rules** (FR-1.6 plus DD-1): `all_success` (default), `all_done`, `one_success`, `one_failed`, `none_failed`, `all_skipped`, and `none_failed_min_one_success` (borrowed from Archon: no upstream failed and at least one succeeded, so a node after an all-skipped branch stays skipped). The enum is closed in IR v1.

The terminal-dependency interpretation approved under [#1446](https://github.com/realkarych/kora/issues/1446) on 2026-11-14 waits for **all** concrete predecessors to finish, including for `one_success` and `one_failed`. Count each predecessor once across data and control edges. For a nonempty set of N predecessors, S counts `succeeded`, F counts `failed`, `upstream_failed` and individually `cancelled`, and K counts `skipped`:

| Rule | Satisfied when | Otherwise |
|---|---|---|
| `all_success` | S = N | `upstream_failed` if F > 0, otherwise `skipped` |
| `all_done` | Always, after all finish | Never false |
| `one_success` | S > 0 | `upstream_failed` if F > 0, otherwise `skipped` |
| `one_failed` | F > 0 | `skipped` |
| `none_failed` | F = 0 | `upstream_failed` |
| `none_failed_min_one_success` | F = 0 and S > 0 | `upstream_failed` if F > 0, otherwise `skipped` |
| `all_skipped` | K = N | `skipped` |

A genuine root with no predecessors in the pinned graph satisfies every rule. An absent, foreign or mismatched predecessor instance is unavailable evidence, never an empty root. Individual cancellation can satisfy a failure handler; cancellation or expiry of the enclosing run still prevents advancement. A satisfied rule supplies no accepted artifact, input binding, branch selection or execution authority. A false rule writes `node.skipped` or `node.upstream_failed` with the fixed reason `trigger_rule_unsatisfied`, atomically with the matching current node state and run scheduling marker. It does not create an attempt or certify physical release.

**Missing inputs after a satisfied rule (#1446).** After all concrete predecessors finish and the trigger rule is satisfied, a required input from a current `failed`, `upstream_failed` or individually `cancelled` producer makes the downstream node `upstream_failed`; a required input from a `skipped` producer makes it `skipped`. Among unavailable required inputs, failure takes precedence over skipping. Control-only predecessor failures do not change this input-specific precedence. The matching existing node event uses fixed reason `required_input_unavailable`; state, event and run marker commit atomically under the original locks, with cancellation and deadline priority. No attempt, input binding, wait, clock or physical release is created by this logical settlement. A false trigger rule keeps its original outcome and `trigger_rule_unsatisfied` reason.

An explicitly optional input may bind `absent` when its exact current producer is `skipped`, `failed`, `upstream_failed` or individually `cancelled`, after the same trigger and predecessor checks. A successful producer always requires actual accepted-current-output evidence. Missing/foreign/mismatched producer records, unknown acceptance, expired or unavailable accepted content and failed selector evaluation never become optional absence or this required-input settlement. An available accepted source cannot be omitted to avoid preparation or a Claim budget. A detached absent binding is a conditional proposal with no retained producer-state authority. The final admission writer decides from exact current locked facts. Clear followed by another permitted unsuccessful outcome, or a compatible producer replacement, may satisfy those facts; a current unfinished, successful or incompatible producer cannot. The original run, target instance and graph/contract pin remain fixed. Recovery, capacity and all other admission checks still apply. The [explicit sequential compatibility mapping](data-model.md#11-immutable-contracts-and-execution-records-adr-0018) applies this owner-approved decision to new eligible decisions on existing pinned `kora.execution.v1` graphs while preserving history.

The owner-approved [saved output binding inventory](data-model.md#saved-output-binding-inventory) associates every root-node output, stored-body node output and declared body export with its complete effective contract by value. Exact segmented declaration paths include unused bodies without expanding invocations; missing or unsupported members cannot be dropped from a successful section. This outer association neither supplies missing implicit roles nor replaces declared export interfaces, wider composition, installation validity or execution admission.

### 3.1 Fixed implicit outputs

Each node kind below has one implicit output with a fixed role name and exact type. These entries are required in canonical IR, including directly submitted documents and nodes in nested subgraph bodies.

| Node kind | Output name | Exact type | Payload schema |
|---|---|---|---|
| `agent` | `session` | `AgentSession` | the saved built-in contract |
| `command` | `result` | `CommandResult` | the saved built-in contract |
| `human_input` | `response` | `StructuredOutput` | the effective form, section 4.2 |
| `wait_event` | `event` | `StructuredOutput` | `outputs.event.schema` when declared |

The compiler inserts omitted author declarations before shared validation and includes their types and schema-reference dependencies in `type_contracts`. It preserves conflicting declarations for validation, never overwriting them. The server does not insert missing role outputs into directly submitted IR. A missing role, a wrong type or another name carrying the same implicit type is `IR003`. Human-input and wait-event nodes have only their respective `response` and `event` output. Agent and command nodes retain their other permitted explicit outputs; role names are not reserved globally, so an agent's structured output may be named `result`.

The outer JSON Schema enforces the structural role restrictions; the shared validator maps these failures to `IR003` even when schema validation finds them first. The diagnostic identifies the owning node's `outputs.<role>` path and its source mapping, including when the role is missing. An inconsistent or missing type closure still fails with `IR022` (section 2.1).

## 4. Node kinds and their `config`

| kind | purpose | required config | produces |
|---|---|---|---|
| `agent` | one harness session | `harness`, `prompt`, resolved harness config (section 4.1) | declared outputs + `session: AgentSession` |
| `command` | deterministic gate | `cmd` (argv, no shell), optional `output_file`, `env`, `secret_env`, `network_policy` (enforced by the runtime like an agent node's, ADR-0009), `workspace`, `success_exit_codes` (default `[0]`) | `result: CommandResult` ([complete revision-1 execution evidence](artifacts/CommandResult.md)) and, with `output_file`, one declared output of type `StructuredOutput` or any custom artifact type whose schema the file must satisfy; stdout and stderr lines stream as session events (DD-36) |
| `human_input` | approval gate | `channel`, `timeout`, `on_expiry`; optional `form`, `approve_when` and `binds` (section 4.2) | `response: StructuredOutput` (effective form response) |
| `wait_event` | sensor | `event` or `poll`, `timeout`, `on_expiry`; optional `interval` | `event: StructuredOutput` (event payload) |
| `timer` | durable delay | `duration` or `at` (absolute time) | none |
| `branch` | deterministic routing | `cases: [{when, to}]` | none; durable selection gates its direct targets (section 4.5) |
| `fan_out` | map a list over a subgraph | `body` (subgraph id), `over` (name of the list-typed input) | per-item instances; the node's own outputs are `List[<type>]` collected from the body's outputs (`collect`) |
| `subgraph` | graph as a node | `body` (subgraph id); the node's `inputs` feed the body | the body's declared outputs |

**Command inputs** (issue #106). A command reads its inputs from files under `{input_root}`, one file per present declared input, named `<input name>.json`.

- **Content.** A file holds the complete selected value: the whole bound payload or parameter object after `path` selection, list composition and fan-out element selection, not the saved CEL projection cut. An `AgentSession` input file is exactly its `kora.agent-session-view.v1` metadata view, never the transcript. It is encoded as kora's canonical JSON ([data-model section 13.2.1](data-model.md#1321-manual-request-and-receipt-format-606)). A `List[...]` input is the canonical array of its resolved elements in binding order.
- **Absent inputs.** An unresolved optional input has no file.
- **Scalars in `cmd`.** `cmd` elements may still carry scalar fields through templates (`{{ inputs.target.task_id }}`).
- **Placeholder.** `{input_root}` is the only placeholder that names the directory. The runtime materialises the sealed bytes there, outside the candidate root and read-only for candidate code on runtimes declaring `protected_execution` ([protocols.md](protocols.md) section 2).
- **Derivation and sealing.** The API derives and seals the files before dispatch ([data-model section 11.2](data-model.md#command-input-preparation-issue-106)). Neither the worker nor the command derives them, and `.kora/inputs/` is never written. `.kora/` is kora's own directory in every workspace, excluded from publication ([protocols.md](protocols.md) section 4).
- **Limits.** A command node declares at most 64 inputs (`IR029`). Its materialised files total at most 256 MiB per attempt.

**Command resources** (issue #45). A command node may declare `resources`: a list of `{name, sha256, size, executable}` and a `resource_set_digest`. `name` is a relative path in Unicode NFC with `/` separators, no empty, `.`, `..` or absolute segments, no control characters (U+0000 to U+001F, U+007F) and no backslash, unique within the node. `sha256` is `sha256:` followed by the lowercase hex SHA-256 of the file's bytes. `resource_set_digest` is `sha256:` followed by the lowercase hex SHA-256 of the **manifest bytes**: the node's entries as a JSON array of objects with exactly the keys `executable` (boolean), `name` (string), `sha256` (string) and `size` (integer), sorted by `name` compared as UTF-8 byte strings, serialised by the JSON Canonicalization Scheme (RFC 8785: keys in code-unit order, no whitespace, minimal string escaping), encoded as UTF-8 with no trailing newline. Other mode bits are not part of the identity. Test vector: a file `select_best.py` holding the 11 bytes `print('a')` plus a newline (`sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb`, not executable) and an empty executable file named `bin/run "ñ".sh` (`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`) give the manifest bytes `[{"executable":true,"name":"bin/run \"ñ\".sh","sha256":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","size":0},{"executable":false,"name":"select_best.py","sha256":"sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb","size":11}]` and `resource_set_digest` `sha256:ac238bb1de5db1d8c8cee691626af721119ffc241431f22603ce77c22af1e305`; the compiler, the API and every runtime must reproduce it. Changed content under an unchanged name changes the file digest, the set digest and the IR digest, so it deploys as a new graph version; no package name or version enters the identity. The content is not in the IR: `kora compile` writes it next to the IR as the resource bundle `build/<graph_id>.resources/<hex>`, one file per distinct digest, and the deploy request carries it ([dsl.md](dsl.md) section 2, [api-and-agent-surface.md](api-and-agent-surface.md) section 2). `cmd` refers to a resource as `{resource_root}/<name>`; the runtime materialises the verified bytes there, outside the candidate root and read-only for candidate code on runtimes declaring `protected_execution` ([protocols.md](protocols.md) section 2). The authoritative bytes are the stored, verified content; a repository file with the same name is never consulted. A resource is executed through an interpreter named in `cmd` and present in the runtime image; read-only resources protect file integrity and do not make a gate an independently trusted evaluator (ADR-0009). The `CommandResult` records the `resource_set_digest` and the per-file digests verified immediately before execution, with the runtime's `resource_protection` (`protected` or `none`); the `ReviewBundle` facts show them next to each gate.

The [saved command-resource manifest](data-model.md#saved-command-resource-manifests) defines semantic admission of the resource list and set digest, including empty declarations. Structural parsing preserves the author's list, digest and omissions. The manifest's JCS representation uses the typed integer size and strict UTF-8 names. IR024 still checks namespace and runtime protection.

The branch fields else and else_reason and the fan-out fields max_parallel and skip_failures are optional in structural IR. Omission is preserved; structural models do not insert execution defaults.

### Structural configuration data and canonical export

Configuration records preserve omissions with `MISSING` and retain authored empty strings, maps, lists and false values.
Optional fields refuse explicit null except `human_input.default_value`, where null is an authored JSON value.
Parsing checks structure only.
Provider resolution, CEL and template execution, default insertion, capability admission and wait scheduling belong to consumers.
Structural models and export helpers acquire no operational resources and emit no diagnostics.
The shared validator owns [public error mapping](diagnostics.md).

`agent.hooks`, `agent.output_schema`, `human_input.form` and sensor `config` accept open JSON objects, including nested boolean schemas and arbitrary schema keywords.
`human_input.default_value` accepts any finite JSON value.
An `x-` key in open data is ordinary data and carries no additional authority.
Open fields use iterative IR-data validation without additional field depth, byte or integer-size limits.
API ingress, schema and provider consumers, and record extensions retain their own bounds.

Validation detaches open JSON from caller-owned containers.
It refuses non-string keys, non-finite numbers, runtime objects, cycles and repeated dictionary or list identities within one open field with content-free errors.
This prevents folded input graphs from expanding during copying.
Separate fields can share a mutable input identity, and callers can share immutable scalars and reuse validated configuration instances.
The check imposes no aggregate field limit.

`export_ir` returns a detached JSON object from a validated `IrRecord` or standalone `Provenance`.
`render_ir_json` takes the same roots and renders the detached object as compact JSON.
Both traverse iteratively and recheck mutated open data under the same rules.
They preserve wire aliases, authored field sets, map and list order, exact integers, finite floats and flattened record extensions.
Unauthored defaults, `MISSING` and private metadata stay out of the export.
The writer emits ASCII-escaped strings, including astral characters, controls and materialised lone surrogates, lowercase JSON literals, unquoted integers and the established finite-float representation.
The JSON text has no insignificant whitespace or trailing newline.

These helpers take materialised data and refuse arbitrary Pydantic or sequence roots.
Text decoders own their own bounds.
A YAML decoder must preserve timestamp tokens as raw strings.
[Dated implementation and measurement evidence](../research/ir-document-models-2026-10-23.md) records the model, checker and decoder coverage, including production YAML loader status.

### 4.1 Agent node config (resolved, FR-3.1)

```yaml
config:
  harness: claude                     # claude | codex in MVP; acp:<agent> reserved for the adapter after MVP (DD-27, ADR-0004); the harness version is fixed by the installation, not the node (DD-14)
  model: claude-opus-5-5
  prompt: "Implement hypothesis {{ inputs.hypothesis.title }} …"   # template over inputs and params
  system_prompt_append: "…"
  skills: [sql-optimizer]             # names; deploy resolves each to a content digest recorded on the graph version
  subagents: [ {name: …, description: …, prompt: …, tools: […]} ]
  hooks: {}                           # harness hook config, declared-only
  mcp_servers: { warehouse: { type: stdio, command: "warehouse-mcp", args: ["--cluster", "prod"], digest: "sha256:…", secret_env: { YT_TOKEN: YT_TOKEN }, network_policy: { egress: ["warehouse.internal"] } } }   # pinned by package or image digest like skills; a server's secret_env and network_policy are its own and never reach the agent (ADR-0009); agent nodes never get kora's own MCP server (DD-5)
  tool_policy: { allow: ["Read", "Edit", "Bash(etl *)", "Bash(git *)"], deny: ["Bash(rm -rf *)"] }   # the one tool-call policy, evaluated by the policy protocol at every tool call the harness does not reject itself, the harness's output channel excepted (DD-29)
  permission_mode: dont_ask           # kora vocabulary, mapped per harness
  sandbox: workspace_write            # kora vocabulary, mapped per harness
  trust_repo_config: false            # FR-3.4
  network_policy: { egress: ["api.anthropic.com", "warehouse.internal"] }   # FR-8.6; enforced by the runtime, harness sandbox is a second layer (DD-21)
  workspace: { repo: "…", rev: "{{ inputs.change.commit | default('main') }}" }
  output_schema: { … JSON Schema … }  # for the StructuredOutput output
  env: { ETL_ENV: dev }
  secret_env: { SERVICE_TOKEN: SERVICE_TOKEN }   # env var -> alias in secrets.aliases
  runtime: docker                     # per-node override of runtime.runtime (FR-8.4)
  vcs: git                            # per-node override (FR-5.3)
```

`harness` names a harness adapter registered with the installation through the `kora.provider` entry point (`claude` and `codex` in MVP; the fake harness is never registered and is substituted by injection in tests, [fakes.md](fakes.md)). The schema already admits the form `acp:<agent>`, where the part after the colon selects the ACP-capable agent, so the ACP adapter that follows MVP needs no change to the graph format; until that adapter is installed, `acp:<agent>`, like any other name without an adapter, fails validation with `IR018`.

A subagent is a closed record with required strict string `name`, `description` and `prompt`, optional string-array `tools`, and the common `x-` extension allowance. No nonempty-name, NodeId, tool-uniqueness or vendor-specific rule is implied. Empty/duplicate names are structurally preserved; provider and semantic admission must prevent lossy array-to-map translation. Agent `skills` are unique and preserve source ordering. MCP server map names are unrestricted strings, while agent `env` and `secret_env` retain the environment-name rules. Permission and sandbox vocabularies remain the enums below.

`workspace.from_ref` names an input path holding a `VcsRef`; when that artifact is present the checkout uses its recorded commit, otherwise `rev`, so a gate runs on exactly the commit the evidence names. A `fresh_with_feedback` attempt is the one path that checks out the branch of the previous attempt's `VcsRef` at its current head instead ([scheduler.md](scheduler.md) section 7). `permission_mode` values: `plan`, `accept_edits`, `dont_ask`, `bypass`. `sandbox` values: `read_only`, `workspace_write`, `full_access`. The harness mapping tables in [protocols.md](protocols.md) say which flag each becomes and which fields a harness cannot honour (compile-time warning, FR-3.3). Harness API keys reach the session as environment variables in MVP; a per-attempt credential proxy is the runtime capability `credential_proxy`, implemented post-MVP (DD-23, DD-30).

`tool_policy` is the input to the **policy protocol** ([protocols.md](protocols.md) section 12, DD-29) and the only tool-permission field in IR 1.0: the DSL's `allowed_tools` and `disallowed_tools` compile into it. The harness's policy mechanism asks the session runner before every covered tool call the harness permits, through the Claude hook or [Codex native approval](codex-execution-profile.md#required-tool-policy-activation); the runner evaluates the namespace's `tool_policy` (set by the platform team) and the node's per layer: a deny from either stops the call, every layer with a non-empty `allow` must match, so a node never widens a namespace allow list, and order inside an array has no effect; a denied call is recorded as a `permission_denied` session event on the `AgentSession`. A call a harness permission rule denies is blocked without reaching the runner and, on an adapter that reports such denials, is recorded from the harness's report with `source: harness`; a call a built-in tool's own input validation rejects never runs and is an ordinary failed tool result, not a denial ([protocols.md](protocols.md) sections 3 and 12). The one exception to the policy is the harness's output channel: on `claude` the adapter allows the synthetic `StructuredOutput` tool of a node with `output_schema` without evaluating either policy, under the conditions of [protocols.md](protocols.md) section 3 (ADR-0009, amended 2026-10-19). Patterns use kora's own grammar and bounds, stated in [protocols.md](protocols.md) section 12 with where it differs from each harness; a shell call with a form on section 12's closed indirection list matches every governing `Bash` deny and no allow pattern. The field is inherited through the preset layers. Every file-edit tool of the harness (section 12's file-edit family) is denied on `protected_paths` (DD-34) for every agent node of the graph. MVP ships the `allow_deny` implementation; Cedar or OPA arrive as plugins without an IR change.

**Protected paths** (DD-34): `runtime.protected_paths` plus the node's `protected_paths` list gate scripts, build files, test directories and tooling configuration. Each entry is a path glob (issue #233): a relative path with `/` separators, at most 4,096 characters and 32 segments, with no empty, `.` or `..` segment and no NUL or control character. `*` matches within one segment, and `**` as a whole segment matches zero or more segments; `?`, `[`, `]`, `{`, `}`, `\` and a `**` inside a longer segment are refused. A leading `~`, a searched piece (every piece of a segment but the last) longer than 96 characters in its case-folded form, and a `*` that touches a character of nonzero combining class in the decomposed or the case-folded form (canonical reordering could move another mark of a name across it) are refused too, and the globs of one call hold at most 8,192 pieces, 64 KiB and 16,384 characters outside ASCII together. Every pattern ends in an implicit `**`, so `tests` protects every file under `tests/`. Both sides compare in canonical caseless form (NFD, case folded, then NFD again, so a glob prefix compares in decomposed form and also covers a name whose next character carries a combining mark), on every platform, with the interpreter's Unicode case tables; a name a filesystem treats as equal beyond canonical caseless equivalence (a case pair newer than those tables, an ignorable code point, compatibility normalization) is caught only by the gate-time check below. The combined runtime and node list a call carries is at most 64 globs. Checking the globs and that bound when the IR is validated is pending work item 3; until then an invalid list surfaces at the first tool call as `protected_paths_invalid`, which fails closed. `kora.ir.globs` implements the syntax for the policy and the executor alike. Before a command gate runs, the preparation phase of its attempt compares the paths changed between the base commit and the candidate commit (the `changed_paths` capture of the checkout plan, [protocols.md](protocols.md) section 4) with those paths and fails the attempt with class `protected_path_modified` on a match; no second checkout is made.

### 4.2 Human-input node config (FR-2.2, FR-7)

The [first-slice chain](first-slice.md#refusal-contract) uses direct CLI bindings to its published `VcsRef` and command `CommandResult`, as in [dependency-upgrade.ir.yaml](examples/dependency-upgrade.ir.yaml). A ReviewBundle is an optional context artifact, not a requirement for this CLI path; its fact/narrative ownership remains unchanged when used.

The [local CLI context contract](cli-approval-opening.md#1-source-and-opening) under #1737 defines an installation refusal for unsupported authored input and context selections in the walking skeleton. It retains ordinary IR path checking and the authored field. The installation and manual-start check is implemented under #1851 with `test_cli_approval_rechecks_exact_authored_context`; the fresh locked recheck at opening remains with #1447.

Only an absent `form` selects the built-in default form:

```json
{
  "type": "object",
  "properties": {
    "decision": {"type": "string", "enum": ["approve", "reject"]},
    "comments": {"type": "string"}
  },
  "required": ["decision"],
  "additionalProperties": false
}
```

An explicitly supplied `{}` is a custom form; `null` is invalid. A form default selects a schema, never response values: the response must supply its required fields, and JSON Schema `default` annotations never fill a response. In particular, the built-in form has no default decision.

An explicit `approve_when` is preserved unchanged. When it is absent, the default is `response.decision == 'approve'` only if the effective form declares `decision` and that expression passes the ordinary schema-driven CEL rules of section 7.4. The same `$ref` and single-schema `allOf` resolution applies. A custom form without `decision` must supply an explicit predicate; an optional or incompatible `decision`, or an enum excluding `approve`, does not bypass CEL checking. A missing or invalid predicate is `IR011` at the owning node's `config.approve_when` path. An omitted predicate has no source expression offset, so the offset is null and the node's existing source mapping is retained.

The effective form and predicate are resolved before expression checking and saved execution-contract construction. The `response` output uses that same effective form for payload validation, downstream typing and projections. Its optional explicit `schema` must be structurally equal to the effective form as JSON, ignoring object-key order; disagreement is `IR003`. This is a semantic check, not a claim that the outer JSON Schema compares schemas for logical equivalence.

The following explicit configuration uses the default decision rule:

```yaml
config:
  channel: vcs                         # name in runtime.approval_channels
  form: { type: object, properties: { decision: {enum: [approve, reject]}, comments: {type: string} }, required: [decision] }
  timeout: "3d"
  on_expiry: fail                      # fail | skip | default_value
  default_value: null
  binds:                               # content binding, FR-7.2a; a push during review invalidates the request and fails the node with candidate_moved
    vcs_ref: inputs.bundle.facts.change  # input path whose commit SHA the approval covers
    gate_digests: [inputs.bundle.facts]  # input paths whose digests the approval covers; defaults to the ReviewBundle facts when the context is a bundle
  approve_when: "response.decision == 'approve'"   # CEL expression that makes the node succeed; false fails it with class approval_rejected, an evaluation error with config_error (section 7.7)
```

`kora reject` is shorthand for submitting `decision=reject`. For a valid, authorized, current response under the effective default predicate, this rejects the request and fails the node with `approval_rejected`. An explicit custom predicate remains authoritative and can approve that payload, for example `response.decision == 'reject'`; clients return and display the actual request status rather than inferring rejection from the command name. Custom forms are submitted through the ordinary response surface with their own fields, without invented fields or synthesized required values. No separate rejection override is implied.

Defaults do not bypass authentication, authorization, the current request and wait, expiry, commit and digest checks, or response validation. These checks precede predicate evaluation; a false result rejects, and an evaluation error retains the existing `config_error` semantics (section 7.7). An `approved` VCS-bound request still needs the existing fresh head verification before the node succeeds and downstream execution starts. Existing expiry policies are unchanged.

kora attaches to every approval request the run's aggregated estimated cost so far (the sum of usage records over the run at request time, [data-model.md](data-model.md) section 5), so the approver sees it next to the form without any node computing it (DD-8). Expiry is a `wait` row like every other timed wait (`timeout` becomes `wake_at`).

For the `vcs` channel (DD-35) the approval counts only when the approver's stable principal is neither a recorded publication principal of the change request nor in the binding's automation-principal set ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)) and, when the namespace sets `approver_allowlist`, is on that list; "merged" is a separate opt-in signal (`accept_merged_as_approval` in the channel configuration, default false). Feedback comments passed to `fresh_with_feedback` carry author and association, filtered to collaborators with write access by default (`feedback_from: collaborators_write`).

A push to the branch of the bound `VcsRef` while the request is `pending` moves the request to `invalidated` and fails the node with class `candidate_moved`, for every channel ([scheduler.md](scheduler.md) section 5; [ADR-0008](../../docs/decisions/0008-content-bound-approvals.md)). The request is never made again on the same bundle: `candidate_moved` is not retryable (section 6) and a manual retry of this node is refused with `API002`; the author retries the node that produced the bound `VcsRef`, with `fresh_with_feedback` to continue from the pushed commit, and the gates, the bundle and a new request follow through the normal downstream path. Before this node's downstream starts, the scheduler checks once more that the approved SHA is still the branch head.

### 4.3 Wait-event node config (FR-2.3)

```yaml
config:
  event: { type: webhook, name: deploy.finished, match: "payload.target == inputs.target.id" }
  # or: poll: { sensor: change_request_status, config: { ref: inputs.change }, until: "observation.status == 'merged'" }
  interval: "5m"
  timeout: "2d"
  on_expiry: fail
```

`poll.sensor` names an implementation of the **sensor protocol** ([protocols.md](protocols.md) section 11, DD-2); kora ships `change_request_status` and `ci_status` (through the VCS protocol) and `http` (GET a URL and expose the JSON body). `until` is a CEL expression over `observation`, the object the sensor returns. The `http` sensor only reaches URLs on the namespace's allowlist and never link-local or cloud-metadata addresses. Adopters add sensors through the `kora.provider` entry point without changing the IR; an invalid `config` is `IR014`.

`interval` remains structurally optional even with `poll`; resolving a polling interval or default belongs to the future validator/scheduler. `event` and `poll` are exclusive by field presence, and null does not substitute for omission.

### Timer configuration timestamp profile

Exactly one of `duration` and `at` must be present; both, neither, null, non-string values, ordinary unknown fields and expressions are refused. The common `x-` extension allowance remains. Duration uses the existing grammar, including zero, leading zeros and no magnitude cap. Neither field is normalized.

`at` uses the kora timestamp profile: ASCII Gregorian local years 0001–9999 and valid calendar dates; `T`/`t`; hour 00–23 and minute/second 00–59; an optional dot fraction of 1–6 digits; and a required `Z`/`z` or signed HH:MM zone with offset hours 00–23 and minutes 00–59. Lowercase, signed offsets, `-00:00`, fractional width and trailing zeros are preserved. Commas, leap seconds, whitespace/newlines, year zero, absent zones, numeric epochs and datetime objects are refused. There is no future-relative test. The exact portable pattern is carried in `TimerConfig.at` alongside `format: date-time`; calendar validation and enabled schema format checking reject impossible dates.

The six-digit fraction admission follows the microsecond timestamp representation; it does not silently truncate precision. UTC-conversion overflow near the year boundaries belongs to a later semantic-validator/scheduler refusal before effects. Structural acceptance supplies no durable scheduling behavior.

### 4.4 Fan-out and subgraph

```yaml
candidates:
  kind: fan_out
  inputs:
    hypothesis: { type: "List[Hypothesis]", from: hypotheses, output: result }   # the mapped input, named by `over`
    baseline:   { type: MetricsReport, from: baseline, output: metrics }         # bound to every instance unchanged
  outputs:
    candidates: { type: "List[Candidate]", collect: candidate }                   # collected from the body's `candidate` output
  config: { body: attempt, over: hypothesis, max_parallel: 3, skip_failures: true }
```

A fan-out materialises one **instance** of the body subgraph per element of the `over` input at run time (NFR-3.3), node ids suffixed `[i]`; inside the body, `$inputs.hypothesis` is the element, every other input is the bound value. The fan-out node's own outputs are lists collected from the body outputs named by `collect`; there is no separate fan-in node. `skip_failures: true` collects the successful instances and succeeds when at least one succeeded; `false` fails on the first failed instance. An empty `over` list materialises no instance, and the fan-out succeeds with empty lists whatever `skip_failures` says; a non-empty list in which every instance failed still fails. A consumer of the empty list decides what an empty input means. Nested fan-out inside a body is allowed (NFR-1.4). A `subgraph` node has the same shape without `over`, `max_parallel` and `skip_failures`, and its outputs are the body's outputs unwrapped.

A `subgraphs` entry has `description`, required nonempty `nodes`, `inputs`, `outputs`, `retry`, `limits` and `source`. The optional `retry` and `limits` apply to the body as a unit (FR-2.7). Definitions live only in `graph.subgraphs`; nested `fan_out` and `subgraph` nodes refer to that shared registry by name. A body has no nested definition registry or graph `id`, `version`, `namespace`, `params`, `triggers`, `target` or `overlap`. Reference resolution and cycles remain semantic checks.

Each stored body's `outputs.<name>` names a producer through `from` and `output`, resolved only in that body's nodes. The producer's declared output type must be assignable to the export's declared type: both saved base contracts must have valid ancestry, list depths must match (a scalar has depth zero), and the producer base must be identical to or a saved descendant of the export base. This producer-to-body-export relation is separate from wrapper interface validation. Missing links and invalid type declarations retain their own diagnostic ownership.

Ordinary subgraph interface amendment approved by the project owner on 2026-10-30 under [issue #505](https://github.com/realkarych/kora/issues/505): an ordinary `subgraph` wrapper declares exactly the body's input names and output names, without renaming, omission or additional members. Omitted and empty maps both denote an empty interface. An optional binding still declares its name. For each input name the wrapper's declared type must be assignable to the body's declared input type, using saved ancestry and equal list depth as in section 3. For each output name both types must have valid complete spelling and saved ancestry, then the wrapper's type must equal the body's declared export type exactly. Widening to an ancestor is refused, as is narrowing to an internal producer's type. The internal producer never replaces the declared body interface. These rules do not define fan-out input mapping, collection typing, schema compatibility or execution admission.

For static fan-out declarations, `config.over` selects a name only in that node's own `inputs`; its known declared type must have at least one `List` level. For each `outputs.<name>.collect`, resolve the named export in the referenced body's declared `outputs`. `List[<declared export type>]` must be assignable to the declared fan-out output: both saved bases have valid ancestry, the output's list depth is the export's depth plus one, and the export base is identical to or a saved descendant of the output base. A narrower internal producer does not replace the body's exported interface. Unknown or malformed type prerequisites retain their own diagnostic ownership. This relation neither checks wrapper input mapping nor chooses runtime collection ordering, expansion budgets or settlement; [issue #71](https://github.com/realkarych/kora/issues/71) remains required before fan-out execution is enabled.

### 4.5 Branch ownership and selection

Every ordinary target named by any branch case or `else` must exist in that graph/body scope and have the branch ID directly in its `depends_on`. DSL lowering inserts the edge; direct IR validation requires it (`IR010`). A direct target has exactly one owning branch in MVP; listing it in multiple cases of that same branch is allowed. Multiple distinct owners are `IR010`. Ordinary DAG validation (`IR001`) includes these edges, rejecting self-targets and routing cycles. `$fail_attempt` is a reserved subgraph action and creates no node/control edge.

MVP rejects `attempt_invariant: true` on every `branch` and on every ordinary direct target named by any case or `else` (`IR010`, at the offending node's flag). Apply this semantic rule in every graph/body scope, including nested definitions, regardless of retry count or which case may be selected. A branch that only routes to `$fail_attempt` is still a branch and cannot be invariant; the reserved action itself is not a node to validate. Omitted/false flags remain valid, and other nodes retain ordinary invariant reuse. The shared validator used by compile and deployment enforces the rule; no runtime cached-route mechanism is introduced.

Selection is an additional readiness gate, not a substitute for ordinary dependencies or trigger/input checks. Every evaluation uses the current concrete owner instance's compact persisted selection ([data-model section 5.1](data-model.md#51-current-branch-selection)). An unresolved owner blocks readiness; a skipped owner or a successful owner that excludes the target skips it; failed/upstream-failed owner produces `upstream_failed`. A selected target still waits for all ordinary upstreams to be terminal and must satisfy its trigger rule and required inputs. The owning branch's control edge participates normally: its success counts for `one_success` and prevents `all_skipped`. Input independence cannot remove this gate.

Only direct targets have this ownership constraint. Untargeted reconvergent joins and failure handlers retain ordinary trigger semantics; no recursive blanket skipping occurs. Target-only clear cannot erase a retained exclusion, and owner clear removes its old selection before reevaluation. No new IR field or hidden runtime edge category is introduced.

The contextual branch pass, `kora.ir.validate_branch_expressions`, is implemented under [issue #739](https://github.com/realkarych/kora/issues/739) for a structurally checked, unchanged snapshot. It visits root nodes and every stored body's nodes once without expanding invocations, including unused or recursive definitions. Every authored `cases[].when`, `cases[].reason` and `else_reason` is checked, even when its route is dormant or cannot fail the attempt. Conditions require the existing Boolean result rule and reasons the string rule, including existing dynamic-result behavior. The environment contains only demanded `inputs` and `params`, using the same invocation-local direct-producer views, body-local lookup, optional membership, schema mapping and effective parameter schema as approval checking. Unsupported or unavailable input sources retain that producer's explicit incompleteness; no synthetic dynamic or empty context replaces them. Literal-only fields do not map unavailable inputs or parameters. Routing legality and `IR010` remain separate.

Parsing failure leaves the field syntax-unresolved for the syntax pass. Input-context and schema failures have separate unresolved counts; input resolution precedes parameter mapping. Independent fields continue. Established checker errors, including cost `IR027`, use the existing bounded collector, exact owning field path, code-point offset and only that node's safe source. `BranchExpressionResult` retains diagnostics and scalar node/body/branch/seen/checked/unresolved/schema/checker counts. Seen expressions equal checked attempts plus the three unresolved counts. `branch_expressions_complete` requires no unresolved or omitted diagnostics; `branch_expressions_valid` additionally requires no errors. This diagnostic result retains no checked trees, schema contexts or caller snapshot and grants no branch-selection authority.

The graph CEL check under [issue #773](https://github.com/realkarych/kora/issues/773) collects branch sites from this pass on the same snapshot. Before constructing contexts or enumerating case fields, it charges one caller-supplied declaration allowance for the root body, type contracts, stored bodies, every node and every branch case by tuple length. Each case has at most two expression fields, plus one `else_reason` per branch, bounding enumeration and retained-site count. The allowance does not bound arbitrary schema/name bytes or whole-validator work; upstream snapshot acquisition/admission remains required. Only a completely successful typed pass supplies genuine authored sites, including dormant reasons and unused or recursive stored bodies; paths keep their original integer case indices. A static refusal or incomplete pass returns its ordinary diagnostic/count result before native admission, with no partial checked inventory. `tests/ir/test_checked_cel_sites.py` owns the branch inventory evidence. No expression is evaluated and no route is selected. Saved CEL facts, broader input views and public graph validation/deployment/start remain pending under [#593](https://github.com/realkarych/kora/issues/593) and [#552](https://github.com/realkarych/kora/issues/552). First-slice installations still refuse branch execution.

## 5. Sub-graph attempt semantics (FR-2.7a)

A loop is a subgraph with `retry.max_attempts > 1`. Rules materialised in the IR:

- A node inside the body can end the attempt with `fail_attempt`: a `branch` case whose `to` is the reserved target `$fail_attempt` with a `reason` expression. The attempt ends `failed` with error class `attempt_failed`.
- Sub-graph attempt N+1 receives the subgraph's inputs plus `$previous_attempt`, a built-in `PreviousAttempt` artifact: `{subgraph_attempt: N, reason, error_class, artifacts: {<node_id>.<output>: <artifact ref>}}`. A node receives it only by declaring an input with `from: $previous_attempt`, so the dependency is visible in the graph. In the control plane a sub-graph attempt is the `subgraph_attempt` integer on the node instances; the word "attempt" alone always means a runtime submission ([data-model.md](data-model.md) section 5).
- [PreviousAttempt revision 1](artifacts/PreviousAttempt.md) fixes the detached value, canonical identity and reference format under [#1860](https://github.com/realkarych/kora/issues/1860). The live declaration, saved CEL routes and execution consumers require their separately qualified support. The first-slice refusal remains in force.
- Nodes permitted by section 4.5 to use `attempt_invariant: true` are not re-run in sub-graph attempt N+1; their earlier artifact version is reused and lineage records the reuse.
- `limits` on the subgraph (timeout, budget) span all sub-graph attempts; `limits` on body nodes apply per node attempt. `retry.max_attempts` on the subgraph counts sub-graph attempts; body nodes keep their own node-level retries inside each.
- Artifacts of every sub-graph attempt are retained; the previous ones are marked `superseded` when a new sub-graph attempt starts (FR-4.5).
- VCS side effects of failed attempts are never touched (FR-2.7, FR-6.1b).

## 6. Retry policy and strategy (FR-6.3, FR-6.4)

For first-slice installations, omitted `runtime.provider_retry`, an empty object and any schema-valid zero `max_wait` resolve to `{max_wait: "0s"}`, the disabled policy. This built-in default is chosen before the enabled-feature namespace/six-hour fallback. An explicit positive value or a positive default in namespace, binding, standalone or `kora.toml` configuration is refused before effects, never silently replaced. Malformed/null values retain schema errors. Save the normalized zero in the graph version's required [`execution_contract.retry_policies` binding](data-model.md#saved-disabled-retry-policies); its contract and deployment digests bind these values so a later installation upgrade cannot reinterpret omission. Numeric budget zero remains refused independently. Zero disables provider-pause/deadline/backoff and new-attempt scheduling, not observations, interruption, same-attempt recovery or closure. The future enabled behavior below applies only to a positive resolved provider wait.

```yaml
retry:
  max_attempts: 3
  initial_interval: "30s"
  backoff_coefficient: 2.0
  max_interval: "10m"
  retry_on: [harness_error, timeout, events_exhausted, stalled, artifact_invalid]
  non_retryable: [budget_exhausted, approval_rejected]
  predicate: "has(outputs.verdict) && outputs.verdict.approved == false"   # optional extra condition on artifacts; outputs.* reads need has() (section 7.7)
  strategy: fresh_with_feedback                       # agent nodes only: fresh | resume | fresh_with_feedback
```

The first-slice [disabled binding](data-model.md#saved-disabled-retry-policies) also saves each ordinary node and body retry with explicit `max_attempts: 1` and its authored dormant fields. The pinned IR supplies those values after preset lowering. Validation still checks dormant syntax and types, including CEL predicates, before deployment.

Ordinary-node retry delay arithmetic follows the [canonical numerical contract](scheduler.md#ordinary-node-backoff-numerics). It consumes already resolved policy facts; it neither produces normalized saved policies nor enables first-slice retries.

For first-slice `runtime.infrastructure_retry`, omission, `{}` and a valid object with omitted or explicit-one `max_attempts` resolve to a saved explicit `max_attempts: 1`: one initial attempt and zero replacements. Select this disabled built-in before enabled-feature fallback. Refuse any explicit or operator-configured count above one, including namespace, runtime/binding, standalone and `kora.toml` defaults, even when an author override, empty retry list or false predicate would hide it. Never clamp an enabling default silently. Preserve schema/type errors and static checking of every dormant field, including CEL predicates, and retain validated authored fields without allowing them to reinterpret the saved disabled policy after restart or feature activation. Store this normalized value with the node's ordinary and provider policies in the [saved disabled binding](data-model.md#saved-disabled-retry-policies). Run, attempt, admission and recovery consumers use that binding, including before the first attempt after an upgrade; missing saved policies fail closed under the graph-contract compatibility rules. No infrastructure predicate, backoff or counter increment can authorize a replacement in this slice.

Error classes (closed enum, used by `retry_on`, `non_retryable` and the data model): `harness_error`, `command_failed`, `timeout`, `events_exhausted`, `budget_exhausted`, `artifact_invalid`, `attempt_failed`, `approval_rejected`, `approval_expired`, `candidate_moved`, `wait_expired`, `stalled`, `protected_path_modified`, `secret_leak`, `provider_limited`, `integrity_error`, `infrastructure`, `cancelled`, `config_error`. Execution uncertainty is not an error class or an IR retry option: lease expiry preserves the original non-terminal attempt and reservations. ADR-0017 blocks all new runtime admissions in its run while execution/publication recovery is unresolved. Recovery can settle by affirmative adoption of the same live execution or its authoritative immutable prepared/completed checkpoint once every earlier grant/operation is closed; direct replacement still requires runtime termination and every dispatcher/platform publication closure. Manual retry records clear/cancel intent while blocked; a linked rerun is refused with `API002` for any replacement-blocking condition. A separately triggered run follows the ordinary target policy. `infrastructure` and `provider_limited` never consume **agent** attempts: `infrastructure` is retried only when the feature is enabled and the saved separate `infrastructure_retry` policy at `runtime` level permits another attempt (NFR-2.4); the first-slice saved count of one forbids replacements, and `provider_limited` under `runtime.provider_retry`, which waits until the provider's reset (or with back-off from 1 to 30 minutes when none is reported) and fails the node only after `max_wait` of total waiting; neither class may appear in a node's `retry_on` (ADR-0024, NFR-2.6). `stalled`, `protected_path_modified` and `secret_leak` are ordinary classes decided by the node's policy. `config_error`, `cancelled`, `candidate_moved` and `integrity_error` are never retryable; `integrity_error` is a command resource whose bytes do not match their pinned digest before execution, or a command input whose sealed file or stored source bytes differ from their recorded size or digest before execution (issue #106) (Archon's "never retry config errors"; a retried approval node would request again on the bundle built for the old commit, [scheduler.md](scheduler.md) section 5).

`strategy: resume` requires the runtime capability `supports_resume`; when the runtime lacks it, when the session handle is gone or when the installed harness version changed, the attempt falls back to `fresh_with_feedback` and records `strategy_fallback` (FR-6.4). In MVP neither reference runtime declares it (`local` and `docker` release the workspace that holds the harness checkpoint, [protocols.md](protocols.md) section 2), so `kora validate` warns `IR009` for `strategy: resume` on them and every such retry takes the fallback; `fresh_with_feedback` is the strategy that works on every runtime and the documented default where the next attempt needs the previous attempt's work.

## 7. Expressions and templates

Branch conditions and reasons, retry predicates, `approve_when`, event `match` and sensor `until` are **CEL** (Common Expression Language) expressions (DD-3). kora parses and checks every expression with its own parser and schema-driven checker, then evaluates it through the Rust-backed `common-expression-language` binding, which the `kora[cel]` extra installs ([ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md) as amended under issues #126 and #167). Other SDKs use any conforming CEL implementation. Only CEL's standard functions and macros are available, narrowed by the rules below. There are no custom functions and no user code. The parser, limits, checker rules, projections, conversions, data bounds and cost model in this section belong to the execution semantics `kora.execution.v1` ([data-model.md](data-model.md) section 11). Every installation applies the same rules, and changing one of them needs a new semantics identity, except the explicitly provisional initial settings before any deployment stores an estimate (section 7.6) or the explicitly supported, narrowly scoped `kora.structured-output-view.v1` compatibility binding for previously unbound StructuredOutput revision-1 contracts (section 2.1). A new binding pin repeats functional conformance, estimate-golden, compact cost-regression, evaluator-lifetime and applicable security checks against those rules. A semantic incompatibility requires a new semantics identity. A newly exposed workload gap needs an explicitly approved measurement method; pin changes do not require exhaustive coefficient fitting or crash-threshold searches. Measurements of the binding cited here are dated evidence from the [CEL binding probe](../research/cel-binding-probe-2026-10-19.md), not guarantees.

### 7.1 Expression kinds and environments

| Field | Variables | Result | Evaluation owner | Evaluation error |
|---|---|---|---|---|
| Branch `cases[].when` | `inputs`, `params` | `bool` | Scheduler branch pass | `config_error` on the branch and `upstream_failed` on its direct targets (section 4.5). |
| Branch `cases[].reason`, `else_reason` | `inputs`, `params` | `string` | Scheduler branch pass, only on a `$fail_attempt` route | `config_error` on the branch, with no `attempt_failed` outcome for the iteration. |
| Node or subgraph `retry.predicate` | `outputs`, `inputs`, `params` | `bool` | Scheduler retry handling | No retry. The node keeps its original error class. |
| `runtime.infrastructure_retry.predicate` | `params` | `bool` | Static check only in the first slice | Inapplicable while dormant. |
| `human_input` `approve_when` | `response`, `inputs`, `params` | `bool` | Shared approval transition for a direct API response or channel observation | The request becomes `failed` and the node fails with `config_error`. |
| `wait_event` `event.match` | `payload`, `inputs`, `params` | `bool` | Scheduler wake-ups | The event does not match this wait. |
| `wait_event` `poll.until` | `observation`, `inputs`, `params` | `bool` | Scheduler prepared-observation apply | The wait keeps polling. |
| Sensor trigger `poll.until` | `observation` | `bool` | Scheduler start-sensor apply | No run starts. |

[Evaluation errors](#77-evaluation-errors) defines the exceptions for unavailable projections and automatic CLI approval expiry.

- `inputs` binds each declared input from the consumed version's stored projection (section 7.5).
  Inputs declared `optional: true` and `$previous_attempt` are non-required members.
- `outputs` includes validated, in-bounds outputs of a failed attempt or exports of a failed subgraph attempt.
  Every member is non-required.
- `params` uses the [effective graph parameter schema](#graph-parameter-schema-omission) and the projection cut at run creation.
- `response` uses the approval `form` type and is projected and checked against the bounds on receipt.
- `payload` and `observation` are the inbound event body and sensor result, respectively.
  The checker binds both as `dyn`.
  Their own ingress contracts govern value shape and conversion.

Apply the same static and native validation to every applicable authored or configured infrastructure retry predicate, including settings masked by an override.
The only external binding is `params`, with its effective parameter type, checker view and reference roots.
The [static result and shadowing rules](#74-schema-driven-checker) apply, including `dyn` as a result and demand for the parameter environment when a comprehension binder is named `params`.
Unavailable schema prerequisites leave dependent checking incomplete.
Omission leaves the predicate absent.
Authored diagnostics identify `runtime.infrastructure_retry.predicate` and keep the original code-point offset when available.
The trusted configuration owner handles configured failures through its safe refusal boundary.
Author-facing diagnostics and logs exclude configured text, private paths and offsets.
The [disabled first-slice policy](first-slice.md#refusal-contract) keeps the predicate dormant and admits zero infrastructure replacements.

### 7.2 Parser, limits and canonical rendering

The kora lexer and recursive-descent parser follow the cel-spec grammar without backtracking.
Offsets start at zero and count Unicode code points in the decoded field.

#### Characters and literals

These character and literal refusals use `IR011`.

- Outside literals and comments, the grammar uses ASCII.
  Identifiers match `[_a-zA-Z][_a-zA-Z0-9]*`, digits are ASCII, and whitespace is space, tab, line feed, form feed or carriage return.
  An input character that CEL rejects is `IR011` even if rendering could remove or rewrite it.
- Refuse the following anywhere, including literals and comments, with `IR011`.
  - C0 controls except tab, line feed, form feed and carriage return, plus DEL, C1 controls, U+2028, U+2029 and surrogates.
  - The pinned Unicode 17 Cf ranges and points U+00AD, U+0600–U+0605, U+061C, U+06DD, U+070F, U+0890–U+0891, U+08E2, U+180E, U+200B–U+200F, U+202A–U+202E, U+2060–U+206F, U+FEFF, U+FFF9–U+FFFB, U+110BD, U+110CD, U+13430–U+1343F, U+1BCA0–U+1BCA3, U+1D173–U+1D17A, U+E0001 and U+E0020–U+E007F.
    The running interpreter's Unicode database does not determine this set.
    Variation selectors remain allowed combining marks.
    Use valid `\u` or `\U` escapes to represent excluded characters in strings.
- A `//` comment ends at line feed.
  A carriage return in a comment requires an immediately following line feed, and form feed does not end a comment.
  Refuse raw carriage return in every string and bytes literal, including raw and triple-quoted forms, since the binding reads it as line feed.
  Use the `\r` escape for a carriage return.
- Escapes follow cel-spec.
  Only strings accept `\u` and `\U`, and escapes for surrogates or values above U+10FFFF are refused.
  Hexadecimal integers use the `0x` prefix.
- An `int` must fit signed 64 bits, a `uint` must fit unsigned 64 bits, and a `double` must be finite.
  An even run of unary `!` or `-` disappears and an odd run keeps one operator.
  A single `-` immediately before an `int` or `double`, including after a run of `!`, is part of the literal unless selection or indexing follows it.
  Thus `-9223372036854775808` and `!-1` contain signed literals, and `-1.f` negates `1.f`.
  A `-` from a longer odd run remains an operator, so `---1` is `-(1)`.
  Both `--9223372036854775808` and `---9223372036854775808` are invalid because their positive literal exceeds the signed range.
  The depth check counts all source operators.

#### Constructs and standard functions

These construct refusals use `IR011`.
Refuse message construction, optional syntax (`.?`, `[?`, `{?`), leading-dot qualified names and backtick field selection.
Refuse reserved words as identifiers and allow them as field names after a dot.
Refuse two-variable comprehensions, including a third argument to `all` or `exists`.
A third argument to `filter` or `exists_one` is malformed.
`has()` requires field selection and macro iteration variables must be simple identifiers.

| Form | Allowed functions |
|---|---|
| One-argument global | `size`, `int`, `uint`, `double`, `string`, `bytes`, `type`, `dyn`, `duration`, `timestamp` |
| Receiver, no explicit arguments | `size`, `getDate`, `getDayOfMonth`, `getDayOfWeek`, `getDayOfYear`, `getFullYear`, `getHours`, `getMilliseconds`, `getMinutes`, `getMonth`, `getSeconds` |
| Receiver, one explicit argument | `contains`, `startsWith`, `endsWith`, `matches` |

Ordinary functions and arities outside this table are `IR011`, including `bool()`, global `matches(s, p)` and time-zone arguments to accessors.
The [schema-driven checker](#74-schema-driven-checker) owns receiver and argument type restrictions such as `int(timestamp)`, `timestamp(int)` and date accessors on durations.
Conformance evaluates every table entry in the pinned binding and repeats that check for every pin change.

#### Static limits

Return `IR027` for an excessive expression or path, including a schema-detected `maxLength` violation.
Check the **4,096-code-point** limit before lexing.
The generated IR schema carries the same `maxLength`.

Syntax-tree depth is at most **32**.
A leaf has depth zero, and each explicit parenthesis and parent-to-child edge adds one.
This includes each operand, conditional branch and condition, selection and indexed value and index, call receiver and argument, list element, map key and value, and macro range and body.
Count each macro body once.
Check depth before every recursive descent and while building nodes, including prefix runs and chains built in a loop.

An expression may use at most **four** comprehension macros (`all`, `exists`, `exists_one`, `map`, `filter`) with at most **two** levels of nesting.
A macro in another macro's body counts as nested.
A macro in the receiver of another macro does not, as in `l.map(…).map(…)`.

Lexing, parsing, checking and estimating cost are linear in tree size.
Checking stops at the expression's first error.
Recheck functional bounds when the binding pin changes.
Full native crash-threshold searches are outside this gate.
The binding probe establishes no universal crash margin, including on the 1 MiB evaluation thread or across supported platforms.
An admitted expression can still stall or crash the shared API or Scheduler process. [Dated binding probe](../research/cel-binding-probe-2026-10-19.md#untrusted-expressions)

#### Canonical rendering

For typed expressions, replace unary minus on an `int` operand with subtraction from integer zero.
The binding checks subtraction overflow and wraps unary negation of the minimum integer.
Render, reparse, hash and estimate the rewritten tree.
Keep unary minus on a `double` operand so `-0.0` retains its sign.
Untyped checking for binding agreement and conformance uses the parsed tree.

Parenthesize operators, use single spaces and omit comments.
Render string literals as double-quoted and non-raw.
Use the short escapes `\\`, `\"`, `\n`, `\r` and `\t`.
Encode other control and non-ASCII characters with `\u` or `\U`.
Render bytes as non-raw `b"..."` literals with the shared short escapes and use `\xHH` for other bytes outside printable ASCII.
Render integers in decimal with the `u` suffix for unsigned literals.
Render doubles in their shortest round-trip form.

The kora parser must recover the same tree, ignoring grouping parentheses, or validation returns `IR011`.
The local compiler runs this check without the binding.
Each node adds at most one pair of brackets around its children.
Keep an additional pair around a numeric literal used as a unary operand, as in `(-(1))`, to prevent signed-literal folding.
Parenthesize a negative literal when selecting, indexing or calling a method on it.

The rendering may exceed the source limits.
Its length is at most 16 times the source length, since a source code point in a bytes literal can require four `\xHH` escapes.
Reparsing has a separate depth limit of `2 × 32 + 1` checked before recursion.
Boundary tests compile the maximal rendering of each shape in the binding.
The binding compiles only the checked canonical rendering, and the IR retains the author's text.

#### Local and server validation

The author-side `kora compile` uses the kora parser and checker without importing the binding.
The general stdio `kora_compile` compiles locally and forwards only compiled IR to `/validate`. The [early JSON mode](dsl.md#early-sequential-json-mode-issue-1918) shares the authored IR checks without making an API request. Installation and executable binding checks run at deploy. [DSL compilation](dsl.md#2-compile-step-kora-compile) · [MCP operations](api-and-agent-surface.md#4-mcp-servers-fr-01-dd-28)

For complete server validation, including `/validate`, `/plan` and full deployment validation, compile every distinct canonical rendering and each distinct decoded `matches()` pattern once.
A proven rejection under the complete public validation rules is reported as `IR011` without the native message.

The [walking-skeleton deployment route](api-and-agent-surface.md#walking-skeleton-graph-deployment-issue-1436) uses the bounded graph check in [section 10](#10-validation-rules-kora-validate).
That check compiles once per field, including equal renderings at distinct fields, and probes each distinct decoded pattern once.
The route preserves established authored diagnostics and maps incomplete rendering verification to `API010`.
Operational refusals retain the route's defined disposition.

Deployment stores the rendering, projection path sets (section 7.5) and cost estimate with the graph version.
Evaluation reuses those saved facts.

#### Native pattern validation

A decoded literal longer than 4,096 code points leaves the check incomplete.
Use an admitted literal as the sole argument to `"".matches()`.
Render the probe under the canonical and regex rules and require the expected parsed tree.
Compile the checked probe in the binding and run it without variables.
Either exact Boolean confirms successful pattern compilation.
Only the pinned binding's `matches` function error during execution establishes pattern rejection.
Static probe refusal, compiler failure and unclassified results leave the check incomplete.
The closed result excludes pattern text and native diagnostics.

The graph validator owns per-call deduplication, declaration paths and public errors.
A trusted producer supplies a bounded tuple of checked sites with typed origin, authored or generated classification and safe exact field paths.
Preflight visits every child, including unevaluated branches.
Its caller allowance charges sites, path segments and characters, visited nodes and each decoded literal occurrence.
Release checked trees before probing each distinct pattern once and keep the associations only for the call.

A complete phase reports at most one `IR011` per authored field with a proven rejection, using a fixed message and the earliest rejected literal offset in the shared collector.
A shared pattern retains each site's authored or generated classification.
An invalid inventory, exhausted allowance, incomplete native check or rejected generated pattern returns a closed incomplete result without partial diagnostics.
Operational refusal and cancellation propagate.
The phase establishes neither whole-graph coverage nor rendering compilation, public validation or deployment authority.

### 7.3 Paths

`inputs[*].path`, `binds.vcs_ref`, `binds.gate_digests`, `workspace.from_ref`, human-input `context` and `graph.target.key.path` use the same IR path notation.
A path is a chain of field selections and literal indexes, such as `inputs.bundle.facts.change` or `artifacts['implement.change']`.
Operators and calls are invalid.

Paths use the CEL lexer and its length and depth limits.
Syntax errors are `IR011` and limit violations are `IR027`.
An index is a non-negative decimal integer in signed 64-bit range or a string literal.
Negative, hexadecimal, unsigned or computed indexes are `IR011`.
A relative path starts with a non-reserved field identifier.

`inputs[*].path` and `graph.target.key.path` start at their source values.
`binds.*`, `workspace.from_ref` and `context` start at the field `inputs` and then name an input declared by that node.
Dot notation and decoded string indexes match exact names with case preserved.
Caller, body and sibling inputs grant no visibility.
A bare root, integer second step, other root or undeclared input gives `IR005` for `binds.*` and `IR002` for workspace and context.
Optional declarations qualify for local binding, which establishes no runtime value or type.
Other resolution errors keep their existing `IR002`, `IR003`, `IR005` or `IR020` codes.

The kora walker resolves the chain without the binding.
A missing key, out-of-range list index, or field or index applied to an incompatible JSON type is absent.
The walker never uses negative indexing.

A `timer` takes a `duration` or absolute `at` time without expression evaluation.

### 7.4 Schema-driven checker

#### Schema types and bounds

Resolve types lazily from the saved closure (section 2.1) and memoize them within the validation.
Type each input at its call site under the producer's effective saved output contract.
Follow local JSON pointers within their document and cross-type references only through the saved closure.
A `$ref` or single-schema `allOf` takes the target's type, independent of sibling keywords.

The type comes from `type` or, when absent, the applicable schema keywords.
`properties` can infer an object, `items` or `prefixItems` an array, and `enum` or `const` the JSON type of their values.
Apply the first matching row.

| JSON Schema | CEL type |
|---|---|
| Pure `type: null`, `const: null` or `enum: [null]` under `kora.structured-output-view.v1` | Existing `NULL` / `null_type`. Produced null stays distinct from absence. |
| `type: [T, "null"]`, `anyOf` or `oneOf` of `T` and `null`, or an `enum` of one type plus `null` | Nullable `T` under CEL wrapper types. An already nullable `T` stays the same. |
| Object with `properties` | Record of declared fields and their `required` flags. Additional properties type index reads. |
| Object without `properties`, including an object with only `patternProperties` | `map(string, V)`. Determine `V` by the undeclared-key rules below. |
| String, or `enum` or `const` of strings | `string` with its allowed values |
| Integer | `int` |
| Number, or `enum` or `const` of numbers | `double`, or `int` under `type: integer` |
| Boolean | `bool` |
| Array | `list(T)` with `items`. With `prefixItems` or without `items`, use `list(dyn)`. |
| Several non-null `type` members, other `anyOf`, `oneOf` or multi-schema `allOf`, mixed `enum`, Boolean schemas, pure null outside the saved view, or no type, keyword or `$ref` | `dyn` |

An object with `additionalProperties: false` and no `patternProperties` admits no undeclared key.
When `patternProperties` is absent, an undeclared key uses the `additionalProperties` type.
With exactly one pattern and `additionalProperties: false`, use that pattern's type.
All other open cases use `dyn`.
Apply the same rules to the value type of a map without `properties`.

A nested schema object's `$id` is a closure fault, including on a JSON pointer's route.
A map member named `$id` inside `properties`, `$defs` or a similar map does not declare a schema resource.
Malformed keywords, unresolved references and noncanonical pointer indexes are also `IR022` before CEL checking.
Visit each schema object at most once per root and view, resolving chains without interpreter recursion.
Recursive, shared and deeply nested closures use counted linear steps.
A failed mapping or failed caller resolver leaves no partial types.

`SchemaTypes.variable` yields a type and its value bound.
A list's element limit is the smaller of `maxItems` and 1,000.
A string's UTF-8 limit is the smaller of four bytes per `maxLength` code point and 16 KiB.
Derived element bounds follow `items` and the additional-property type, and field bounds follow `properties`, under the same reference, single-`allOf`, nullable and view rules as type mapping.
Other kinds' size keywords, siblings of `$ref`, `prefixItems` positions and multi-type schemas tighten nothing.
Every declared bound retains 4,096 bytes and 256 values.
Declare a child bound only if it tightens something below the parent.
Revisiting an open recursive mapping declares no bound there, so recursive bounds stay acyclic and can only loosen.
When a record has a tightened additional-property bound, declare a bound for every property, using data defaults for untightened or recursive properties.
Otherwise an undeclared field would use the additional-element bound.
A read `maxItems` or `maxLength` that is not a non-negative integer is a schema closure fault (`IR022`).
Keywords that the mapper never reads are outside this check.

Binding verifies that each JSON kind matches its declared CEL type and otherwise returns `invalid_value`.
Numbers feed `int`, `uint` and `double`, strings feed `string`, booleans feed `bool`, and null feeds `null`.
Arrays feed lists and objects feed maps or records.
A nullable type also accepts `null`, and `dyn` accepts every JSON kind.
These checks enforce types inferred by keyword even when the JSON Schema admits other kinds.
A child type preserves the CEL type of every parent property, so `number` to `integer` narrowing is `IR003` (section 2.1).

#### Environments, selections and guards

The checker receives the variable types, the required result type and the roots that require field selection.
It rejects unknown identifiers and a comprehension variable that shadows an environment variable.
`inputs` and `outputs` appear only under field selection.
`payload` and `observation` are `dyn`.
Dot selection on `dyn` needs no guard and a missing key gives `missing_field` at evaluation.
A `dyn` operand satisfies the same-type equality rule.
A `dyn` result is checked for exact `bool` or `string` when evaluated.

On a record, dot selection requires a declared field.
Open records expose undeclared keys only by index, with the undeclared-key type.
A literal read of an undeclared key on a closed record is an unknown field.
Read maps only by index.
Dot selection on a map, including a literal map, gives `unknown_field`.
A list index is `int` or `dyn`, and a map index matches the map's key type.
A literal-key map read needs the `in` guard used for an open record's extra keys.
A dynamic-key read of a map or object projects the complete subtree and needs no guard.
A missing key in such a read gives `missing_field`.

A non-required read needs a guard on the same path.
A guard matches field selections and literal indexes from the same variable or comprehension binding, with dot and equivalent string-key steps treated alike.
Thus `has(x.f)` and `'f' in x` guard both `x.f` and `x['f']`.
An operand without a path, such as a list literal, cannot be guarded.
A `has()` parent must be present or already guarded, as in `has(a.b) && has(a.b.c) && a.b.c`.

The true facts of `has(p)` are `p`.
The true facts of a literal-key `'k' in x` are `x['k']`.
The true facts of `a && b` and false facts of `a || b` are their respective unions.
Negation exchanges true and false facts.
The right operand of `&&` sees true facts of the left and the right of `||` sees false facts.
The true branch of `?:` sees the condition's true facts.
Facts remain valid inside comprehensions.

#### Type and overload rules

- An `enum` string literal used with an enum-typed value must be allowed by that enum.
  Check literals in `==`, `!=` and the list-literal elements of `in`.
- Equality compares scalars of the same kind, lists with lists, and maps or objects with maps or objects.
  A string with allowed values compares with any string, and `dyn` compares with any kind.
  Ordering can mix `int`, `uint` and `double`.
  Arithmetic across numeric types needs an explicit `double()` or `int()` conversion.
- `==` and `!=` with `null` yield `bool` and require a nullable, `null` or `dyn` operand.
  Check presence with `has()`.
  Check selection, ordering, arithmetic and calls against the inner type of a nullable operand.
  At evaluation these operations on a null value fail, with two admitted calls.
  `type(null)` returns `null_type` and `dyn(null)` returns `null`.
  A nullable result does not satisfy a required `bool` or `string`.
- Map literal keys and the left operand of map `in` are `int`, `uint`, `bool`, `string` or `dyn`.
  A repeated literal key gives `IR011`.
  Heterogeneous list and map elements and `?:` branches join to `dyn`.
  `T` joined with `null` yields nullable `T`.
- `size()` rejects `string` and `dyn` while the binding sizes strings in UTF-8 bytes.
  Operators and functions use the supported cel-spec overloads.
  Exclude `int(timestamp)`, `timestamp(int)`, `string(bool)`, `duration + timestamp`, duration date accessors and duration `getMilliseconds()`.
  Unary minus and `getMilliseconds()` reject a `dyn` operand because of native Boolean negation and total duration milliseconds.
  These exclusions are `IR011`.
  Time strings, time zones, invalid UTF-8 in `string(bytes)`, time ranges, error absorption, iteration order, repeated computed map keys and mixed `int`/`uint` keys reached through `dyn` retain their [conformance rules](#78-conformance).
- The result must satisfy the field type in [section 7.1](#71-expression-kinds-and-environments).

#### Literal regular expressions

`matches()` takes a string literal under the [parser and native check](#72-parser-limits-and-canonical-rendering).
The static pattern grammar allows these constructs.

- Literal characters and escaped ASCII punctuation except `<` and `>`.
- `\n`, `\t`, `\r`, `\f`, `\v`, `\a`, `\xHH`, `\x{H…}`, and dot.
- `\d`, `\D`, `\w`, `\W`, `\s`, `\S`, plus `\p{…}` and `\P{…}` and their one-letter forms.
  The class-size table covers `L`, `Lu`, `Ll`, `N`, `Nd`, `P`, `S`, `Z`, `Greek`, `Latin`, `Han` and `Cyrillic`.
- Bracketed classes, including negation, ranges and nested classes.
- Anchors `^`, `$`, `\b`, `\B`, `\A` and `\z`.
- Capturing, non-capturing and named groups, with names unique within the pattern and beginning with a letter or underscore.
- Flags `i`, `m` and `s`, with at most one `-` followed by a flag.
- Alternation and repetitions `*`, `+`, `?`, `{n}`, `{n,}` and `{n,m}`, each optionally lazy.

All other constructs are `IR011`, including `(?x)`, flags `U`, `u` and `R`, look-around, backreferences, `\Q…\E`, `\<` and `\>`, POSIX classes, class set operations (`&&`, `--`, `~~`), ranges with a class escape endpoint and Unicode classes outside the table.
Groups and bracketed classes share a nesting limit of 32, checked before recursion.

Estimate pattern units as a fixed base plus the larger of atom/class/repetition units and the syntax-character floor.
The base and syntax-character coefficients are both 1, so empty patterns, sibling empty captures, empty alternatives and flag-only groups carry nonzero charges.
Use the provisional class-size tables pinned under `kora.execution.v1`.
Any `i` flag selects case-insensitive weights for the entire pattern.
Charge a bracketed class by the sum of member weights, with a dot-weight minimum for a negated class.
A range extending beyond ASCII and a negated Unicode class also have that minimum.
A bounded repetition multiplies by the greater of its upper bound and one, and then adds fixed overhead.
An unbounded one uses lower bound plus one and the same overhead.
For example, `+` multiplies by two and `*` by one.
Exceeding the 32 nesting limit or the provisional 1,048,576-unit cap is `IR027`.
The tables, base, syntax floor and cap carry over under the lightweight validation of section 7.6.
The estimate does not establish native memory or compiled size.

#### Prepared environments and diagnostics

`prepare(environment, meter)` supplies a sealed opaque `PreparedEnvironment` to `check_typed`, which also accepts the unprepared `Environment`.
Validate every variable, including unused variables, and validate shared type nodes and convert source bounds once under the meter.
Snapshot the outer mapping into a private dictionary.
Nested types and source-bound graphs are trusted kora-builder data and must remain unchanged after preparation.
Malformed preparation raises `BoundError`.
Prepared and plain checks have identical diagnostics, integer-negation rewriting, renderings, digests and complete estimates.

Static typing refuses with `IR011` at the expression path and the failing node's code-point offset.
The fixed reasons are `unknown_identifier`, `root_not_selected`, `unknown_field`, `shadowed_variable`, `unknown_enum_value`, `no_matching_overload`, `unguarded_optional`, `string_size`, `repeated_map_key` and `wrong_result_type`.
Wrong result type uses offset 0.
Schema field names and enum values in CEL types stay outside logs and diagnostics.
Report one finding per expression under the [shared diagnostic contract](diagnostics.md), with optional source locations and an offset only while the final safe segmented path still identifies the exact field.
The enclosing collector owns deterministic path/offset order, error and warning bounds and their separate omitted counts.

### 7.5 Projections and typed conversion

#### Saved projection paths

Freeze a normalized [path set](data-model.md#saved-cel-projection-path-sets) at deployment for run parameters and for each saved output declaration.
Save an explicit empty set for an unread declaration.
Keep checker-local reads in [saved expression facts](data-model.md#saved-cel-expression-facts).
Under the disabled first-slice retry binding, authored retry reads remain local facts and contribute no runtime projection paths.

Expression-local `payload`, `observation` and approval `response` reads keep their own consumers and lifetimes.
Form-response ingress checks the condition's local reads and the complete frozen response-output path set, which represents separate reads through artifact routes.

A path set has sorted `whole` and `presence` lists relative to a variable.
`.name` selects a field, `['k']` a map key or non-identifier field, `[*]` each list element, and the empty string the whole variable.
Use single-quoted keys with canonical escapes, including brackets for keyword and reserved names.
Generated canonical paths, including escaped non-BMP keys, round-trip under the rendering-length bound.
A whole path subsumes presence there and all deeper paths.
A presence ancestor can coexist with deeper reads and preserves them.

Compose paths through `inputs[*].path`, body forwarding via `$inputs`, a fan-out element as `[*]` on the mapped list, `collect`, and subgraph exports.
`$params` reads use the validated run parameters.
`$previous_attempt` reads of `artifacts['n.o']` compose into the earlier iteration's output.

Resolve each producer through the exact pinned graph binding.
Remove the selected input or output name before prefixing the supported selector.
Keep root presence for a presence-only read of that value.
An ordinary retry's `outputs.<name>` resolves to the node's own output or the body's declared export.
Follow all matching caller declarations of a shared body and internal routes of unused bodies.
Propagate collection and export requirements to their applicable children, with wildcard steps where required.
The synthetic `PreviousAttempt` owns its metadata projection.
Earlier artifact reads contribute to the exact earlier output declarations, and whole or dynamic previous-attempt reads require the complete applicable route set.

The initial path composer supports direct `params` reads and same-body direct artifact input bindings.
Selected paths, list-position translation, reserved sources, wrappers, collection/export and previous-attempt routes require complete supported derivation before contributing to a path set.
Refuse uncovered routes before new effects.
Use the pinned producer view before each cut.

#### Extracting and cutting projections

A dynamic index, comprehension range or whole-value use reads the whole subtree at its path.
A literal integer index into a typed list projects `[*]` followed by the remaining path.
An integer index into `dyn` projects its operand whole.
A path read only through `has()` contributes presence.
A literal-string key tested by `in` contributes presence for a map or record.
A `dyn` operand of `in` contributes its whole subtree.
Selection and index receivers alone leave the ancestor without a whole read.
Keep the paths for index expressions and external variables read inside comprehensions.
The whole range covers reads of the local comprehension binding.

Each typed check exposes an immutable per-variable `projections` map of frozen `PathSet(whole, presence)` values.
Untyped `check` returns an empty map, and prepared and unprepared environments yield the same typed paths.
Keep exact integer-index guard identities separate from wildcard projection steps.
Intern and read tables belong to one check, and normalized tries are temporary.
Sort canonical ASCII paths by character distribution with a bounded alphabet.
Count key, prefix, rendering and sorting work.

`cut(value, path_set, meter)` walks JSON iteratively.
It copies whole reads, skips absent keys and cuts `[*]` elements without changing list positions or cardinality.
A presence-only terminal uses the empty value of the original JSON kind (`""`, integer `0`, float `0.0`, `[]` or `{}`).
Booleans and `null` stay unchanged.
If a kind cannot support the next step, keep its empty value so a `dyn` selection still fails.
Whole numeric reads retain original kind and signed zero, and the result never grows relative to the source.

A validated producer may exceed the evaluation bounds if the selected projection fits.
Enforce canonical bytes, value counts, depth, strings and containers as the cut grows.
Iterator frames keep the pending frontier for whole and partial cuts independent of source size.
Exceeding a cut bound, a cycle in copied data or a non-JSON scalar raises a content-free `BoundError`.
Refuse unsupported JSON kinds and subclasses before walking them.
Unused source descendants stay outside this cut and evaluation quota and receive no validation from it.
Existing equivalence coverage compares consistently converted full and cut values in the admitted native domain, including missing and wrong-kind `dyn` selections, and separately checks a large original with a small cut.
Freezing, graph composition, producer cutting, stored binding, `AgentSession` policy and the storage codec remain with their own owners.

#### Producer and storage rules

The owner producing a value cuts its projection once.
Upload cuts during validation, including staged target-source outputs (ADR-0019).
The Scheduler producer owns the cuts for a `human_input` response, `wait_event` payload output, `PreviousAttempt`, fan-out collection or subgraph export.

For a collection or export, re-cut each child's stored original value under the destination's frozen view and path set before converting.
The result must equal conversion of the admitted full value under that view, including whole-subtree and presence reads.
An integer view can convert original `-0.0` to `0`.
A destination with `dyn` items must still see double `-0.0`.
A number view can round integer `9007199254740993` to double `9007199254740992.0`.
An integer-view export still receives the exact original integer.
Keep the existing full-versus-cut and composition checks for both cases.
Re-cutting uses only the stored original cut and saved views, without recovering pre-admission tokens or reading live schemas.

Each child's original and converted logical cuts independently fit 4 KiB and 256 values.
One complete [version-1 record](data-model.md#lossless-artifact-cel-projection-storage) fits 8,515 bytes and at most 1,000 children contribute up to 8,515,000 encoded bytes.
Acquire, check and release one child at a time, holding only the bounded destination, fixed child references and current child state.
Stop at the first original or converted destination bound.
Charge visited encoded bytes, checking, conversion and source metadata.
A re-cut counts as progress under the existing allowance.
Acquisition requires its existing aggregate caller admission.
The scheduler transaction performs no storage I/O or full JSON work.
Before applying, recheck the exact current transition owner and all input identities.
The accounting neither activates a route nor supplies the required owner, native acquisition and overhead evidence of work items 4/8/10.

Run creation cuts validated parameters under the pinned path set.
For [versioned run inputs](data-model.md#1323-run-input-storage), save the original unconverted cut with the run after checking required conversions and original and converted bounds.
Bind only that saved cut under its verified types and conversion rules, with the same result each expression would obtain from the admitted full value.
For a nonempty detached manual-start path set, use the static parameter mapper to check the pinned view's original and converted bounds.

Artifact preparation cuts from the validated value, converts under the pinned view and checks section 7.6's bounds.
The [version-1 encoding](data-model.md#lossless-artifact-cel-projection-storage) stores the original and converted cuts together as native bytes in the CEL derived owner.
The legacy projection column holds a marker.
Affected StructuredOutput cuts retain original and converted numeric kind, binary64 signed zero and NUL-bearing strings and keys on both databases.
Complete effective validation and cutting use the same checked output binding in one preparation.
Canonical payload bytes, standalone model validity or detached unassociated validation results cannot prove projection fidelity.
The detached checker validates the value and encoding under the existing view. [Detached preparation evidence](../research/artifact-cel-projection-producer-2026-11-15.md)
Existing [StructuredOutput activation checks](artifacts/StructuredOutput.md#5-verification-and-activation) retain actual acquisition and producer evidence, including round-trip numeric kinds, native ownership and source integration.

Projections follow the matching artifact's retention class (data-model section 10).
The artifact-payload retention applies to payloads, reference-only types retain indefinitely, and `AgentSession` retains for the lifetime of its session metadata.
Listings never load projections.

#### AgentSession projection

Core constructs [`kora.agent-session-view.v1`](artifacts/AgentSession.cel-policy.json) from the validated logical value before applying deployment-frozen paths.
The saved effective contract binds its exact fields, nested exclusions and checker schema.
It allows harness name/version, session ID, billing class, normalized usage with completeness and derived cost/basis, turn count, integer millisecond duration, stop reason and worker/image provenance.
It excludes denials, transcript content, references, descriptors, capture and availability, opaque vendor evidence and observation/price identities.
Whole, dynamic, forwarded, collected, exported and previous-attempt reads retain those exclusions.
An excluded field is `IR011`.
No `transcripts` capability is needed for the metadata projection.
It outlives transcript expiry and stays distinct from the saved `summary`.

#### Binding and conversion

Evaluation binds only stored projections for saved values.
A missing key remains absent.
A missing, expired or mismatched projection yields `projection_unavailable`.
For version 1 the exact matching CEL derived owner controls availability.
A present primary payload or legacy marker cannot revive the projection or permit recomputation through current behavior.
Artifact `summary` serves API and console, outside CEL. [Artifact type protocol](protocols.md#8-artifact-type-fr-4143)

Project `payload` and `observation` once per event or observation and frozen path-set digest.
A bounded walker checks that set's bounds, independent of other open waits.
Keep a disposable entry-count-bounded cache, release it when the event's batches finish and recompute after a restart.

Use the producer's saved contract for conversion.
`integer` yields `int`, including whole input `1.0` as `1`.
`number` yields `double`.
Every JSON number in schema-free `payload`, `observation` or a `dyn` subtree yields `double`.
Strings remain strings for every `format`, and schema defaults leave values unchanged.

Required conversion refuses out-of-range signed 64-bit integers, nonfinite `number` or `dyn` results and section 7.6 bound violations.
Standalone artifact admission under `kora.structured-output-view.v1` separately permits the [standalone numerical domain](artifacts/StructuredOutput.md#2-numerical-values-and-canonical-bytes), including large integers outside the projection.
Required conversions of those values still fail without truncation.
Other contracts retain their entry rules.
Direct and polled approvals and run-parameter ingress keep their stricter entry checks, including unused integer fields.

- An output fails with `artifact_invalid`.
- A direct form response or a run parameter at trigger, webhook start or rerun is refused with `API003`.
- A static cron, sensor or graph-trigger `params` refusal at deployment is `IR027`.
- A refused polled response is unapplied, retains `pending` and records a diagnostic ([Scheduler](scheduler.md) section 16.3).

Delayed event, observation and approval producers preserve StructuredOutput numbers from the selected typed snapshot admitted from retention.
The earlier `inbound_event.payload`, `wait.observation` and `approval_request.response` normalization stays in force.
The snapshot cannot reconstruct source numeric tokens or a sign of zero already normalized.
Freeze the admitted artifact once, independent of current providers.
Verify the source-to-output legs on both databases.
The separate harness gate covers absence versus explicit null. [StructuredOutput activation](artifacts/StructuredOutput.md#5-verification-and-activation)

**FR-2.5 example.**
In `inputs.review.verdict == "approved" && inputs.bench.cpu < inputs.baseline.cpu * 0.85`, a stored `100` becomes `100.0` for a `number` schema and both multiplication operands are `double`.
With `integer`, `int * double` is refused and the author uses `double(inputs.baseline.cpu) * 0.85`.
Without typed conversion, whole JSON numbers raised `TypeError` in the probe. [Binding probe](../research/cel-binding-probe-2026-10-19.md)

### 7.6 Data bounds, cost and evaluation

#### Data admission

Apply the bounds to each stored projection and to the aggregate of referenced variables before and after typed conversion.
Count all referenced roots and each scalar or container occurrence.
Map keys count toward canonical bytes, with no extra value count.

| Bound | Limit |
|---|---|
| Aggregate size and each stored projection | 4 KiB (4,096 bytes) of canonical JSON |
| Aggregate scalar and container values, and values in each projection | 256 |
| List length | 1,000 elements |
| Map size | 1,000 keys |
| String length | 16 KiB of UTF-8 |
| Nesting depth | 32 |

Check all bounds when cutting a stored projection (section 7.5), including the entire fan-out `over` list and each element.
Apply the following outcomes when a bound is exceeded.

- Producers fail with `artifact_invalid`, including the nodes producing a scheduler collection, export, wait payload output or `PreviousAttempt`.
- A form response must fit the paths read by `approve_when` and by every consumer of the output.
  Refuse a direct response with `API003` and keep a polled request `pending`.
- Refuse run parameters with `API003` at trigger, webhook start or rerun.
  Refuse static `params` for cron, sensor and graph triggers with `IR027` at deployment.
- Check `payload` and `observation` at projection.
  A bound violation is an evaluation error under section 7.7.

Before conversion, iteratively check exact JSON types, finite numbers, Unicode scalar values and the data bounds.
Bind and charge only referenced roots.
Refuse the original aggregate before copying a root that would cross its bound, even if conversion would shrink it.
Check aggregate bytes and values again after conversion.
The caller supplies the saved, validated projection bounds and schema types.
Integer schema values bind as signed 64-bit integers, including integral JSON floats, and number and schema-less numeric values bind as finite doubles.
`Record.extra` types undeclared keys.
Bind missing `outputs` as an empty map and return `projection_unavailable` for any other missing referenced projection.

A checked expression carries the declared bounds used by its estimate.
First check each value's JSON kind against the declared type so a mismatch is `invalid_value`.
Then validate declared element counts and UTF-8 string lengths at every depth covered by the bound.
A violation is `bound_exceeded`.
Only the aggregate data bounds enforce declared byte and value counts.
The schema mapper does not tighten those counts, and environment builders keep them at the data bounds to preserve the estimate.

#### Estimated work and memory

Validation estimates each expression's worst case from its tree and variable bounds.
A smaller `maxItems` or `maxLength` tightens the applicable bound under section 7.4.
Steps charge work, and memory estimates converted data and peak live intermediate sizes with a fixed provisional allowance and multiplier.
An expression over either budget is `IR027`.
The estimates do not measure native representation, allocator overhead or RSS.

| Charge | Proportional to |
|---|---|
| Evaluation | Fixed base plus bound sizes and value counts for the referenced top-level variables, which the binding converts on each call |
| Node visit | One weighted visit |
| `all`, `exists`, `exists_one` | Range bound length times body charge, multiplying for nested macros |
| `map`, `filter` | Square of the range bound length for binding 0.10.0, plus range length times body charge |
| `+` if either operand may be a string, bytes, list or `dyn`, and list/map literals | Result size and value count summed over operands and carried to consumers |
| `map` and `filter` results | Range length for the element count. `map` also multiplies body result size by range length. `filter` preserves a list's bounds. Over a map, record or `dyn`, its list of iterated keys or elements uses range total size, value count and the iterated element bound. |
| `==`, `!=`, `<`, `<=`, `>`, `>=`, `in` | Operand sizes and value counts |
| Conversions and string functions | Operand size |
| `matches()` | Literal pattern estimate for compilation on each call, plus that estimate times subject length for matching. The compiled pattern also counts toward memory. |
| Compile on a cache miss | Canonical rendering length |
| Scheduler transaction projection, including collection re-cut and event projection | Bytes read and written. Charge only to the transaction allowance. |

Every comprehension also charges its iteration weight times the range bound length.

Each variable bound carries canonical bytes, value count, element count, string length and any declared element and field bounds.
An undeclared child inherits the parent's bytes and value count and the global element and string ceilings.
A cycle, including a self-cycle, discards all declared children of its member bounds, which then use inheritance.
Convert from leaves up and discard children of a converted bound whose subtree would exceed depth 32.
These rules depend only on the bounds.
A declared field selection or literal-string index uses the field bound.
Other selections use the element bound.
An index other than a literal declared field, when fields exist, inherits the parent bound.
A join's element inherits from a parent with declared fields, because its element bound need not cover those fields.

The environment is trusted builder data in a plain `dict` with string names.
Each referenced entry is an exact `Variable` with a closed graph of exact CEL type instances.
A record uses a plain `dict` of strings and `RecordField` values with Boolean `required`.
Type cycles are allowed, and nullable types cannot wrap nullable types.
Read each bound once before deriving anything from it.
It is an exact `ValueBound` with non-negative integer sizes and a plain string-keyed `dict` of child bounds.
A child cannot exceed its parent's bytes or value count or the global element and string ceilings.
The estimate examines the entire type and bound of each referenced variable and treats a reached malformed bound as the builder error `BoundError`.
The default variable bound uses 4,096 canonical bytes and 256 values alongside the independent container and string limits, which aggregate limits may dominate.

The element bound of a list literal joins the bounds of its whole element values.
Concatenation and conditionals join operand element bounds, using inheritance for an operand that lacks an element bound or declares fields, regardless of its static type.
A map or record range iterates over keys.
A `dyn` range iterates over the join of its key and element bounds, with inheritance for an operand with declared fields.
A type join keeps a type only for the same object or primitive kind and otherwise gives `dyn`.
It does not compare structures.
The checker reuses one type object per schema node in a closure.

Empty list and map literals and zero-length comprehension results use two canonical bytes.
`string()` allows six canonical bytes per input byte plus the scalar allowance, including escaped controls and invalid-UTF-8 replacements for `dyn`.
This bound flows into collections, concatenations, comprehensions, comparisons and pattern subjects.
A comprehension size is affine in the iteration variable's size, so the range's own total bounds the sum over iterations, as in `l.map(x, x)`.
Adding two typed numbers costs a node visit.
Other additions that may concatenate charge the result as such.
Unless both operands are typed strings, bytes or lists, charge at least a scalar so `dyn` cannot reduce the corresponding typed charge.

Estimate once in post-order with exact integer arithmetic.
Convert each bound object once, iteratively, for work linear in the expression, bound objects and declared fields.
Check memory before steps to determine the reported dimension.
Apply `memory_base` once and `memory_byte` to converted data once and to each value allocated by an operation.
Referencing a variable or part of one allocates nothing.
Provisional `memory_base` is 0 and `memory_byte` is 1.
These coefficients have no resident-memory calibration.

#### Provisional budget and qualification

The steps budget is **100,000,000,000,000 logical charge units**.
The memory budget remains **16 MiB (16,777,216 logical estimated bytes)**.
All fourteen time weights and the provisional regex tables belong to `kora.execution.v1` under section 7's pin rule.
The budgets give neither elapsed-time nor RSS guarantees.

The observed coarse regression target is **100 ms per evaluation interval** on the reference CI runner.
The [compact protocol](../research/cel-cost-calibration-2026-10-22.md#current-compact-regression-protocol) runs ten fixed cases, each in a fresh child, through one cold cache miss and three warm cache hits in `Evaluator.evaluate`.
Checking and evaluator startup precede each child's measurements, and result assertions follow each interval.
The interval covers binding, queueing, compilation on a miss, native execution and delivery.
The original comparison said each interval must be at most 100,000,000 ns.
Record every observation and its exceedance flag as a diagnostic under the [approved verification classification](../research/approved-verification-audit-2026-10-29.md#numerical-observations).
Keep forty calls with exact result checks, logical and resource checks, ten-second child deadlines and the 240-second suite watchdog.
The timing observations establish no interruption limit or latency guarantee.

The provisional exception ends with the first successful durable production graph-version commit containing a [saved estimate](data-model.md#saved-cel-expression-facts), including a dormant authored field.
An empty inventory or uncommitted result does not cross that boundary.
Later data-bound or cost-rule changes require a new execution semantics identity.
The [conversion evidence](../research/cel-cost-calibration-2026-10-22.md#qualified-smaller-bound-result) records the initial bound qualification.

The golden corpus pins provisional estimates.
Required compact functional and admission checks and the golden checks supply bounded pre-consumer qualification.
The 100 ms comparison is diagnostic, including where older coverage descriptions refer to its former gate.
Author expressions still require the schema-driven checker, projections, minimum-int negation correction and actual consumer integration.
Evaluator infrastructure alone grants no consumer activation.

Logical memory catches compounded concatenation within sequential macros.
For example, four `map` calls each joining sixteen copies of a 2 KiB element from a list with `maxItems: 1` denote 128 MiB while fitting static and input-data limits.

#### Worker, cache and closure

For runtime consumers, evaluation runs inside the API and Scheduler's existing short transactions. [Scheduler](scheduler.md)
One process-owned long-lived daemon thread with at least 1 MiB of stack serves all in-process standalone instances.
During startup, the evaluator holds its own lock while setting the process-wide default stack size, starting the worker and restoring the previous default.
The worker starts in an empty context and the process owner loads the binding without inheriting request context.
It runs one job at a time.
Transaction evaluations take precedence over queued server compiles and probes.
Submit compile work one expression at a time so an evaluation waits for at most one in-flight compile, whose source and pattern sizes are statically bounded.

The worker owns a least-recently-used cache keyed by rendering digest and expression YAML path.
By default it allows 128 entries and 131,072 summed rendering characters.
A hit verifies the rendering, and a miss reparses it before `Binding.compile(checked)`.
Charge compilation on a miss to the allowance.
Validation compiles return a small outcome without entering the cache, and pattern probes never cache their programs.

An `Evaluator` belongs to its creating event loop.
Stopping one instance or closing a handle rejects only its own queued jobs, waits for its in-flight and undelivered work and leaves other handles and the worker usable.
The default one-second close deadline returns `close_timed_out`.
Queued cancellations prevent execution, and in-flight cancellations discard the result.
After cancellation, event-loop closure or `close_timed_out`, a running job stays charged until all its native work settles.
Closed loops release work that is no longer running, and stopped open loops remain charged until delivery or close.
An unmapped worker fault permanently closes admission, settles waiting callers and releases the cache.

Embedded standalone shutdown (`kora test`, `run_scenario`, `kora_test`) follows handle closure without exiting the host.
The daemon belongs to the process until it exits.
The native binding holds the GIL during compile and evaluation and cannot be interrupted.
A supervisor kill is the hard limit.
Static admission, logical budgets and compact timing observations do not establish GIL dwell, API response or transaction duration limits.
An admitted expression can stall or crash the shared process.

#### Outstanding ownership

| Per-process admission | Limit |
|---|---|
| Evaluation jobs | 32 |
| Validation compiles, including native probes | 32 |
| Evaluations per batch | 16 |
| Outstanding charge | 8 MiB |
| Handles | 128 |
| Pending lifecycle reports | Required bound of twice the configured handle capacity, 256 by default |
| YAML path, event ID and checked rendering | 1,024, 128 and 65,536 characters respectively |

Job counts include in-flight work and completed results waiting for delivery.
Charge detached values, renderings, bounded metadata and result retention to the shared owner.
Result growth that would exceed the shared outstanding charge limit returns operational `busy`.
Pending lifecycle reports count toward new-handle admission, with up to one timeout and one completed-close report per handle.
The evaluation and validation job, outstanding charge, batch and handle limits listed above are `Settings` defaults.
The YAML path, event ID and rendering maxima are fixed.
The [lifetime method](../research/cel-evaluator-lifetime-2026-10-21.md#method-amendment-dated-2026-10-28-process-wide-rss-spans-and-hop-latency-become-diagnostics) governs native validation-compile qualification at its recorded trigger.

Validation and evaluation charge the checked expression's effective bound snapshot.
`bound_bytes` counts 256 fixed bytes, 1,024 per distinct bound node, 128 per declared field, 256 per referenced variable and eight per retained name code point.
The immutable snapshot uses bounds after cycle and depth normalization and clamps scalar limits to the global data bounds.
Its field maps are read-only and it retains no source type, original bound graph or converter.
Builder dictionary changes cannot alter admission.
Construction is iterative, memoized and counted without reconverting the source.
Preparation constructs normalized snapshots once, preserving distinct per-variable roots and shared descendants.
Each expression retains its referenced roots and charges its exact reachable metadata union once per expression.
Each submission pays independently, including when snapshots share nodes.

Under the [approved preparation bound](https://github.com/realkarych/kora/issues/376#issuecomment-6475511099), total work is `O(T + B + L + sum(U_i))`.
`T` covers enumeration and shared type validation, `B` source-bound conversion and normalization, `L` expression checking and `U_i` the distinct reachable normalized nodes, field edges and name code points for expression `i`.
Repeated expressions can still cost their count times reachable metadata size.
Validator-scale admission and its required evidence remain with work item 3.

Both operation families also charge `projection_bytes` separately without altering `bound_bytes`.
The projection charge is zero if empty.
Otherwise charge 256 fixed bytes, 256 bytes plus eight per name code point for each variable and 128 bytes plus eight per canonical path character for each whole or presence path.
Retain only immutable mappings, path tuples and strings.
Extraction and cutting tries are temporary.
Every submission releases its own charge through delivery, cancellation or close.
These are logical ownership charges and establish no native-memory or RSS measure.

A native pattern probe takes one validation slot and retains only its checked rendering at admission.
Charge the greater of 1,024 reply bytes and four times rendering length.
Each decoded code point takes at most ten rendering characters and the fixed call takes 14.
Evaluation keeps priority and delivery, cancellation and close use the shared rules.

#### Scheduler allowance

Consumer integration is pending.
The installation limits logical CEL work per Scheduler transaction, defaulting to 100,000,000,000,000 charge units and never below one expression's steps budget.
This is installation configuration and establishes no wall-time limit.
Apply it to branch, retry, wake-up and prepared-observation evaluation.
Each transaction evaluates at least one expression.
At exhaustion, commit decisions, keep `needs_scheduling` for the rest and rotate across runs and namespaces.
Match an inbound event in batches under each run's lock.
The allowance affects order, and existing expiry rules decide waits whose work is deferred past expiry.
The API admits `approve_when` under the per-expression logical budget independently of the allowance.
That budget establishes no response deadline.

### 7.7 Evaluation errors

#### Classification

Catch the binding's errors only around binding calls.
Conversion runs outside those handlers.
The process resolves the binding's exact panic class at startup and refuses startup if it cannot do so.
That class derives from `BaseException`.
`CancelledError`, `KeyboardInterrupt` and `SystemExit` propagate.

| Binding exception at execution | Error kind |
|---|---|
| `KeyError` | `missing_field` |
| `TypeError` | `no_overload`, including selection on `null` |
| `IndexError` | `index_out_of_range` |
| `ZeroDivisionError` | `division_by_zero` |
| `OverflowError` | `overflow` |
| `RuntimeError` | `invalid_argument`, for instance an unparseable conversion string or a `dyn` value of an unsupported kind |

kora also returns `bound_exceeded`, `number_out_of_range`, `invalid_value`, `wrong_result_type` and `projection_unavailable`.
`invalid_value` covers non-exact JSON types, non-scalar strings and JSON kinds inconsistent with the declared CEL type.
The required result is exactly `bool`, or `string` for reasons, without coercing Python truthiness.

An `internal_fault` is logged at ERROR.
Its fault is `value_error` for native `ValueError` at compilation or execution, `runtime_error` for compile `RuntimeError`, `panic` for native panic at either stage, and `unmapped` for checked-tree re-parse failure or cache collision.
An undeclared identifier or native pattern rejection could also raise execution `RuntimeError`, which still maps to `invalid_argument`.
The checker, required projections and server-side pattern validation exclude those inputs before evaluation.

#### Record

The evaluator's internal error record has these fields.

| Field | Content |
|---|---|
| `kind` | An error kind above |
| `field` | The expression's YAML path |
| `offset` | The location only when kora finds it itself, null for native errors |
| `event_id` | The inbound event ID for a `match` error |
| `observed_at` | The observation time for an `until` error |
| `at` | Record time from the injected clock |
| `fault` | Null except for `internal_fault`, where it is `value_error`, `runtime_error`, `panic` or `unmapped` |

A node failed by an evaluation error has class `config_error`, reason `cel_evaluation_error` and an evaluation error in its failure details.

#### Effect on consumers

The effect rules below extend [section 7.1](#71-expression-kinds-and-environments).

- Evaluate `approve_when` only after the response passes staleness, commit, digest and approver checks (section 4.2).
  On evaluation error, retain the response, set the request to `failed` and record `applied: true` with the error in `approval.responded`.
  Fail the node with `config_error`.
  Close the channel with `notify(resolved)` linking the canonical failed status.
  The failed request gives no approval and a subsequent response is recorded with `applied: false`.
  The direct API returns HTTP 200 with the request's status.
  The [automatic expiry path](cli-approval-opening.md#3-expiry-and-retained-response) preserves an expired request and scheduler audit, attaching the same safe CEL error to the node failure.
  Its request evaluation-error field stays empty, and ordinary human response and legacy error fields retain their behavior.
- Evaluate `retry.predicate` only with attempts remaining and a retryable class listed in `retry_on`.
  `infrastructure` and `provider_limited` consume no attempts and never evaluate it.
  Every `outputs.*` read needs a `has()` guard.
  Record a predicate error on the node's failure event.
- Evaluate each event and wait pair at most once, including false results and errors.
  Apply the existing event eligibility rules.
  Under the run lock, pass 6 verifies the exact open wait before writing a `match` error.
  The evaluating transaction records `event_evaluation` together with any consumption.
  An open wait with an eligible, unevaluated event keeps `needs_scheduling` set.
- An `until` error counts as a successful dispatch observation, consumes the token and uses the normal next interval without degrading the integration.
  A start binding consumes no occurrence or cursor and writes no `trigger.*` event.
  Write the error only from the current claim. [Scheduler](scheduler.md)
- Store the last `match` or `until` error on the wait or start binding for API and console reads.
- `projection_unavailable` fails the owning node with `config_error`, including a wait, because a missing or expired projection cannot become available again.
  For `retry.predicate`, it prevents a retry and retains the original class.

#### Safe content

Stored evaluation errors use only the field set defined for the owning record in the data model, including node failures, waits, start bindings, approval requests and events. [Data model](data-model.md)
Public evaluation errors carry only the fields allowed by the applicable API contract. [Public evaluation errors](api-and-agent-surface.md#external-observation-and-delivery-diagnostics-adr-0021)
The evaluator returns frozen error values across its thread boundary.
It raises operational refusals on the caller loop with cause and context cleared, including inside another exception handler.
Errors and diagnostics exclude expression text, values, native messages, exception chains and panic payloads, including at DEBUG.
The API or Scheduler component that evaluates emits diagnostics and deduplicates them by wait, binding or node, field and kind. [Observability](observability.md)

### 7.8 Conformance

CI runs the binding against the vendored cel-spec textproto subset without a protobuf dependency.
The corpus and licence remain byte-identical to the pinned upstream commit under a SHA-256 manifest validated in CI.
The [vendoring record](../research/cel-conformance-data-2026-10-20.md) identifies the source, full commit and digests.

The [selection manifest](../../tests/cel/conformance/selection.py) names every included and excluded file.
It excludes `dynamic`, `namespace`, two-variable `macros2`, message-type `proto2`, `proto3`, `wrappers` and `enums`, `optionals`, `unknowns`, `type_deduction` and all extension files.
For included files, each exclusion entry identifies a case or a section, its required feature and exact refusal reason.
Features include message types and qualified names, backtick fields, raw control characters in literals, unavailable narrowed functions and functions outside the standard library.

The reader parses each whole file with closed generic textproto syntax.
A typed case model rejects fields outside the supported subset.
Every included case passes through kora's parser and canonical renderer.
Unlisted cases must be accepted, listed refusals must produce the exact reason and each exclusion entry must match a case.

Each accepted fixture runs its upstream text and bindings in the native evaluator and compares the typed result.
This is a conformance path. Production uses checked renderings and the fixed pattern probe under section 7.2.
Decode bound and expected durations to microseconds from seconds and nanoseconds.
The binding returns `int` and `uint` as Python integers, so compare their values alike.
Check timestamps by type, type values by name and expected map keys by Python equality, which does not distinguish `bool`, `int` and `uint` key kinds.
Accept any error for an expected error and do not compare a zero double's sign.

For the pinned binding 0.10.0, record deviations from cel-spec in the [deviation manifest](../../tests/cel/conformance/deviations.py) with their category and exact binding outcome.
Run the cases and fail if a recorded deviation disappears or changes.
The [dated binding evidence](../research/cel-stack-2026-10-21.md) records the measured corpus and deviation counts.

| Category | Binding behavior |
|---|---|
| String size | `size()` of a string counts UTF-8 bytes. |
| Mixed numeric keys | Equal `int` and `uint` keys miss each other's entries, and maps with those keys compare unequal. A `double` map key, list index or membership test raises a type error. |
| Error absorption | `all()` returns an earlier error even if a later value is false. |
| Negated boolean | `-false` is true, which gives a silently wrong result on `dyn` without the checker. |
| Wrapping negation | Negating the minimum signed 64-bit integer wraps. Typed integer negation uses subtraction and overflows as required (section 7.2). |
| Invalid UTF-8 | `string()` substitutes replacement characters for invalid bytes. |
| Time ranges | `duration()` from a string clamps to the signed 64-bit nanosecond range. For example, `duration('9300000000s') == duration('9400000000s')` is true. Duration sums and timestamp differences can exceed the range unchecked. A timestamp string can reach past years 1–9999 via its UTC offset, while other out-of-range timestamps are refused. |
| Time formatting | `string()` of a timestamp or duration uses a different format from cel-spec. |
| Duration accessor | `getMilliseconds()` returns total duration milliseconds. |
| Missing overloads | `int(timestamp)`, `timestamp(int)` and `duration + timestamp` fail. The nested timestamp conversion and timestamp/null comparison cases use `timestamp(int)` to build their timestamp. |
| Qualified names | Dotted variable names and qualified type names such as `google.protobuf.Timestamp` are unresolved. |
| Repeated keys | A map literal with repeated keys keeps the last value. |

Map key order varies between evaluations, so `map()` and `filter()` over a map return elements in arbitrary order.
Timestamp accessors read calendar fields in the offset given in the timestamp string.
For example, `timestamp('2020-12-31T23:00:00-05:00').getFullYear()` returns 2020 for a UTC instant in 2021.
The included corpus has no case for that behavior.
These binding limits inform the checker and evaluator rules above.

### 7.9 Future contracts

The parser, checker, cost estimate and textproto reader are unit-tested, as AGENTS.md allows for parsers, checkers and arithmetic. Every test that touches the metadata database runs on PostgreSQL and SQLite. All are deterministic; anything that measures time or memory is a measurement below, not a test.

**Status.** The parser, limits, rendering, paths, pattern subset, cost estimate and conformance reader landed under [issue #161](https://github.com/realkarych/kora/issues/161) in `kora.cel` and `tests/cel/`. The binding, its loader and every comparison of `kora.cel` with the binding landed under [issue #165](https://github.com/realkarych/kora/issues/165) in `kora.cel_eval.binding`, `tests/cel_eval/` and `tests/cel/`, and their rows below are active, the two entry-point tests in their loader legs only. These tests are active in full: `test_cel_rejects_expression_over_depth_limit` (its 4,096-code-point leg is `test_cel_prefix_floods_stop_at_the_depth_limit`), `test_cel_rejects_comprehension_over_limits`, `test_cel_paths_obey_limits_and_never_reach_binding` (its binding leg is `test_cel_paths_never_load_the_binding`, in a fresh process) and the rows added below for `kora.cel` other than the two binding rows. These are active in part:

- `test_cel_canonical_rendering_matches_author_text`: the re-parse leg over the corpus, the included conformance cases and expressions generated from a fixed seed; its binding leg is `test_cel_canonical_rendering_matches_author_text_in_binding`.
- `test_cel_rejects_expression_over_length_limit`: the expression leg; the schema-validation leg waits for the `ir` validator.
- `test_cel_rejects_non_literal_regex_pattern`: the literal rule; the backreference leg at server-side validation waits for the API.
- `test_cel_regex_compile_cost_bounded`: the nesting and cap legs in `kora.cel`; the `kora compile` and `/validate` legs and compiling each distinct pattern once wait for the DSL and the API.

Under [issue #373](https://github.com/realkarych/kora/issues/373) a `RuntimeError` at execution maps to `invalid_argument`, as covered by `test_cel_maps_only_documented_native_errors`. The evaluator conversion, native error mapping, exact result types, thread priority/cancellation/close, bounded cache, safe diagnostics and resource-lifetime legs are active under [issue #166](https://github.com/realkarych/kora/issues/166). Under [issue #167](https://github.com/realkarych/kora/issues/167), `tests/cel/test_cel_cost_regression.py` adds the active provisional golden corpus and budget/specification consistency checks, and `tests/measurements/test_cel_cost_regression.py` adds the compact 100 ms measurement gate. They establish no full calibration or crash margin. The projection, API, scheduler and DSL legs stay `pending implementation` with the work items that own them in the implementation plan.

The schema-driven checker of section 7.4 landed under [issue #337](https://github.com/realkarych/kora/issues/337) (`kora.cel.schema`, the schema-to-CEL-type mapping) and [issue #330](https://github.com/realkarych/kora/issues/330) (`kora.cel.checker` and `check_typed`), applied to an environment its caller builds. These rows are active: `test_cel_rejects_unknown_field`, `test_cel_rejects_non_boolean_approve_when`, `test_cel_rejects_unknown_enum_literal`, `test_cel_rejects_mixed_int_double_equality`, `test_cel_rejects_unguarded_optional_field`, `test_cel_rejects_string_size`, `test_cel_recursive_closure_checked_in_bounded_time` and the rows added below for the schema mapping and the checker. The checker legs of `test_cel_converts_json_numbers_to_schema_types`, `test_cel_dyn_selection_needs_no_guard` and `test_cel_dyn_result_requires_exact_bool` are in `tests/cel/test_cel_checker.py` under the same names, beside their evaluator legs in `tests/cel_eval/`. The untyped `check` remains the syntax and cost entry of binding agreement and conformance; every author-expression consumer uses `check_typed`. Building environments from a graph document (the type closure at each call site, StructuredOutput effective views, `AgentSession` summary fields and the `inputs`, `outputs`, `params` and `response` roots), `test_cel_child_type_keeps_parent_cel_types` and `test_cel_examples_pass_checker` stay with work item 3 and the remaining #138 contracts; projections stay pending. Schema-tightened estimate bounds and their binding check landed under [issue #374](https://github.com/realkarych/kora/issues/374): `test_cel_cost_estimate_rejects_compounding_concatenation` is active in full, its `maxItems` leg building the variable from a schema, and the rows added below for schema bounds are active. Under [issue #372](https://github.com/realkarych/kora/issues/372) typed checks rewrite integer negation (section 7.2): `test_cel_typed_negation_of_minimum_int_overflows`, `test_cel_typed_rendering_rewrites_integer_negation`, `test_cel_typed_estimate_is_of_the_rewritten_tree`, `test_cel_typed_rendering_stays_within_the_reparse_bounds` and `test_cel_widest_typed_negation_compiles_in_binding` are active, `test_cel_typed_check_is_linear_in_steps` covers integer negations, and the checker leg of `test_cel_limits_at_boundary_compile_in_binding` is `test_cel_typed_limits_at_boundary_compile_in_binding`.

Under [issue #375](https://github.com/realkarych/kora/issues/375), the library legs of projection extraction, notation, cutting and full-binding equivalence are active. `test_cel_projection_path_sets_follow_reads`, `test_cel_projection_notation_round_trips`, `test_cel_projection_cut_keeps_only_read_paths`, `test_cel_projection_cut_is_linear_and_never_grows` and the library leg of `test_cel_projection_matches_full_binding` cover this scope. `test_cel_admission_charges_retained_projection_metadata` covers the unchanged evaluator ownership boundary. Producer composition, persistence, graph binding and consumer-scale acceptance remain pending with work items 3 and 8–11; the earlier pending projection references in this section refer to those integration legs.

| Test | Required result |
|---|---|
| `test_cel_rejects_unknown_field` | `response.decison`, a dot selection of an undeclared field on an open object and a dot selection on a schema-typed map are `IR011` at the right offset |
| `test_cel_rejects_non_boolean_approve_when` | `approve_when: "response.decision"` is `IR011` |
| `test_cel_converts_json_numbers_to_schema_types` | stored `100` under `number` binds as `double` and the FR-2.5 example evaluates; under `integer` the checker rejects `int * double`; schema-less numbers bind as `double` |
| `test_cel_rejects_expression_over_depth_limit` | 33 levels of each node kind (`+`, `&&` and selection chains, `?:`, `!`, `-`, parentheses, call arguments, list literals) are `IR027`. 4,096 code points of prefix or conditional nesting are `IR027` without a recursion error |
| `test_cel_rejects_expression_over_length_limit` | 4,097 code points are `IR027`, also when found by schema validation |
| `test_cel_rejects_comprehension_over_limits` | a fifth macro, and a macro in the body of a macro in the body of a macro, are `IR027`; four chained macros are accepted |
| `test_cel_limits_at_boundary_compile_in_binding` | the rendering of the depth-32 expression of every nesting shape, parenthesized unary chains included, compiles in the binding, and evaluating it raises no panic; the checker leg, `test_cel_typed_limits_at_boundary_compile_in_binding`, accepts a well-typed expression of every nesting shape exactly at the depth limit with `check_typed` and compiles and evaluates its rendering |
| `test_cel_unary_runs_kora_rejects_are_refused_by_the_binding` | `--9223372036854775808`, `---9223372036854775808` and `---0x8000000000000000` are `IR011` for an invalid literal in `kora.cel`, and the binding refuses them too |
| `test_cel_unary_runs_reduce_as_the_binding_does` | runs of unary `!` and `-` before variables bound to an integer, a boolean and a string, and before literals, give the same typed result or error class as author text and as rendering |
| `test_cel_limits_at_boundary_evaluate_in_binding` | an expression of 4,096 code points, four macros, macros nested two deep, the widest bytes rendering and the densest escapes compile and evaluate in the binding to a value of the expected type |
| `test_cel_cost_estimate_rejects_compounding_concatenation` | four sequential `map` operations, each joining eight copies of a 16 KiB element of a list with `maxItems: 1`, are `IR027` because the memory dimension is exceeded, not for a static limit |
| `test_cel_cost_estimate_golden_corpus` | the provisional step, logical-memory and compile-charge estimates of every corpus expression are unchanged |
| `test_cel_cost_constants_match_specification` | the steps budget and the logical-memory budget, in MiB and in bytes, stated in the provisional-budget bullet of section 7.6 equal the provisional budget of `kora.cel`; the 100 ms regression target stated there equals its per-interval nanosecond limit and the limit of the compact measurement |
| `test_cel_larger_default_budget_accepts_more_estimated_work` | the 100,000,000,000,000-unit default admits work refused for steps by the explicit 10,000,000,000,000-unit test budget |
| `test_cel_regex_compile_cost_bounded` | literal patterns over the size-estimate cap or the group-nesting limit, such as `a{1000}{1000}`, `\w{500}`, `\p{L}{500}` and case-insensitive repetitions, together with `\W{500}`, `\P{L}{500}` and `[\p{L}\d]{500}`, are `IR027` in `kora compile` and at `/validate` alike; a pattern nested deeper than 32 groups stops before recursion; a request compiles each distinct pattern once |
| `test_cel_rejects_non_literal_regex_pattern` | `x.matches(y)` with a non-literal `y` is `IR011`; a literal with a backreference is `IR011` at server-side validation |
| `test_cel_binding_compiles_canonical_rendering` | the binding receives only kora's rendering; a lexically ambiguous author text cannot reach it |
| `test_cel_canonical_rendering_matches_author_text_in_binding` | over the corpus, the included conformance cases and 2,000 seeded expressions, with one fixed context bound, author text and rendering give the same typed result or the same error class in the binding, and at least 1,300 of them give values; lists compare as multisets, because map iteration order varies, and `int` and `uint` results compare alike, because the binding returns both as Python integers |
| `test_cel_canonical_rendering_matches_author_text` | every rendering parses back to the same tree, ignoring grouping; over a corpus and expressions generated from a fixed seed, author text and rendering give equal results in the binding |
| `test_cel_paths_obey_limits_and_never_reach_binding` | an over-long or over-deep path is `IR027`; resolution never calls the binding |
| `test_deploy_rejects_hand_written_ir_over_cel_limit` | `/validate`, `/plan` and deployment reject a hand-written IR over each limit and over the budget |
| `test_cel_validation_work_bounded` | a 4 MB request of maximal expressions compiles each distinct rendering once and returns a bounded diagnostic list with its omitted count |
| `test_cel_rejects_unknown_enum_literal` | `response.decision == 'aprove'` is `IR011`, also for an `enum` without `type` |
| `test_cel_rejects_mixed_int_double_equality` | `inputs.same.exit_code == 0.0` is `IR011`; `inputs.metrics.cpu_seconds < 100` is accepted |
| `test_cel_rejects_unguarded_optional_field` | an unguarded optional read, and an unguarded `outputs.*` read in a predicate, are `IR011`; each guard form is accepted, including inside conjunction and disjunction chains and nested `has()` chains |
| `test_cel_dyn_selection_needs_no_guard` | `observation.body.ready == true` and `payload.commit == inputs.bundle.facts.change.commit` pass the checker; a missing key at run time is `missing_field` |
| `test_cel_rejects_string_size` | `size()` on a string or a `dyn` value is `IR011`; on lists, maps and bytes it is accepted |
| `test_cel_recursive_closure_checked_in_bounded_time` | recursive and doubly referenced `$ref` closures are checked in a number of checker steps linear in their size, counted deterministically |
| `test_cel_child_type_keeps_parent_cel_types` | a child type narrowing `number` to `integer` is `IR003` |
| `test_cel_examples_pass_checker` | every expression in the example IRs and in dsl.md section 1 passes the checker against published built-in schemas |
| `test_cel_schema_types_follow_the_mapping_table` | every row of the section 7.4 table and every settled mapping case gives its CEL type |
| `test_cel_schema_types_resolve_references_only_through_the_closure` | local JSON pointers and `urn:kora:artifact:` references resolve through the saved closure to one memoized type |
| `test_cel_schema_types_refuse_a_closure_the_validator_must_reject` | unresolved references, anchors, nested `$id` resources, malformed or JSON-null keywords, non-canonical or over-long pointer indexes and non-schemas raise a closure fault, never a CEL type |
| `test_cel_schema_types_map_the_published_vcs_ref` | the published VcsRef revision-1 schema maps to a closed record with its required fields and one shared principal type |
| `test_cel_schema_types_map_deep_closures_without_recursion` | 5,000 nested arrays and a chain of 5,000 referenced types map without interpreter recursion in linear counted steps |
| `test_cel_converts_additional_properties_to_their_schema_type` | undeclared keys of an open object convert to its `additionalProperties` type |
| `test_cel_schema_types_map_shared_reference_chains_in_linear_steps` | reference chains shared by many properties, also behind nullable pairs, map in counted steps linear in the closure size |
| `test_cel_schema_types_refuse_again_after_a_closure_fault` | a mapping that failed with a closure fault leaves no partial types: repeating it fails again and other schemas still map |
| `test_cel_schema_types_forget_partial_types_after_a_resolver_failure` | a mapping interrupted by the caller's resolver leaves no partial types, so repeating it fails the same way |
| `test_cel_schema_types_resolve_each_document_against_its_own_root` | a schema mapped as its own document resolves local references against itself, not against a document that contained it earlier |
| `test_cel_conversion_refuses_values_of_another_json_kind` | a value whose JSON kind differs from its declared CEL type, including a string under a keyword-inferred object, is `invalid_value` at binding; `null` under a nullable type and any kind under `dyn` convert |
| `test_cel_schema_bounds_follow_max_items_and_max_length` | in both views, `maxItems` and `maxLength` declare element counts and UTF-8 lengths through lists, records, maps, single patterns, nullable types and pairs, references and single `allOf`; keywords on other types, siblings of `$ref`, several types and limits above the data bounds tighten nothing; a recursive closure declares only its non-recursive children unless an additional-properties bound is tightened, when every declared property, recursive ones included, is declared at the data bounds; `test_cel_schema_bounds_follow_references_through_the_resolver` covers cross-type references |
| `test_cel_schema_bounds_admit_every_valid_value` | every value generated from a fixed seed that is valid under a bounded schema is within the derived bound, and some invalid value is refused |
| `test_cel_schema_bounds_refuse_malformed_keywords` | a negative, fractional, boolean, null, string or infinite `maxItems` or `maxLength` raises `SchemaError` |
| `test_cel_schema_bounds_are_linear_in_steps` | deriving bounds takes exactly the counted steps of the type mapping on shared and long reference chains, linearly, and a 5,000-deep bounded closure derives without recursion |
| `test_cel_binding_refuses_data_over_declared_bounds` | at binding and through the evaluator, with the bounds the checked expression carries, a root or nested list over its declared element count, a root, nested or field string over its declared UTF-8 length and an additional property over its declared bound are `bound_exceeded`, values within them and untightened declared properties evaluate, and an expression checked without declared bounds admits them all |
| `test_cel_binding_checks_json_kinds_before_declared_bounds` | an object bound where a schema-tightened list is declared is `invalid_value`, not `bound_exceeded` |
| `test_cel_cost_estimate_uses_schema_max_items` | a nested `map` over a schema list is refused for steps without `maxItems` and accepted with `maxItems: 10` |
| `test_cel_rejects_unknown_identifiers_and_whole_roots` | undeclared identifiers, and `inputs` or `outputs` outside field selection, are `IR011` at their offset |
| `test_cel_rejects_shadowing_comprehension_variable` | a comprehension variable named like an environment variable is `IR011`; nested comprehensions may reuse a variable name |
| `test_cel_rejects_operand_overloads` | operators, functions, indexes, selections, map-literal keys, null comparisons and macro predicates outside the overload table are `IR011` |
| `test_cel_rejects_overloads_the_binding_lacks` | `int(timestamp)`, `timestamp(int)`, `string(bool)`, `duration + timestamp`, the date accessors and `getMilliseconds()` of durations, unary `-` and `getMilliseconds()` on `dyn` and repeated map-literal keys are `IR011` |
| `test_cel_accepts_well_typed_expressions` | representative typed expressions over every operator family, macro, conversion, time value, null comparison and string result are accepted |
| `test_cel_typed_check_is_linear_in_steps` | typed checking, guard facts included, takes at most a fixed number of counted steps per code point on hostile and maximal input up to the length limit |
| `test_cel_typed_negation_of_minimum_int_overflows` | through the evaluator, a typed negation of the minimum `int`, also computed (`-(x - 1)`), as a literal (`-(-9223372036854775808)`) or inside a macro, is `overflow`; other integers negate exactly, a `double` negation keeps the sign of zero and a null nullable integer is `no_overload` |
| `test_cel_typed_rendering_rewrites_integer_negation` | typed renderings write `int` negation as a subtraction from `0`, leave `double` negation unchanged and re-parse to the rewritten tree; the untyped check keeps the parsed tree |
| `test_cel_typed_rendering_stays_within_the_reparse_bounds` | a 4,096-code-point expression with the most integer negations renders within 16 times its length and within the re-parse depth bound |
| `test_cel_typed_estimate_is_of_the_rewritten_tree` | the estimate of a typed integer negation equals that of the same subtraction from `0` written by the author |
| `test_cel_widest_typed_negation_compiles_in_binding` | the widest 4,096-code-point list of integer negations compiles in the binding, evaluates for an ordinary integer and overflows for the minimum |
| `test_cel_checker_diagnostics_exclude_author_text` | no checker diagnostic carries an identifier, field name or literal from the expression |
| `test_cel_schema_typed_fr_2_5_example_evaluates` | the FR-2.5 example checked against mapped `number` schemas converts stored integers and evaluates in the binding |
| `test_cel_checker_overloads_evaluate_in_binding` | every function and arithmetic overload the checker accepts, every accepted ordering pair and the `size()` forms evaluate in the binding with typed literals to the expected value |
| `test_cel_parser_agrees_with_binding_compile` | every corpus expression, and every expression generated from a fixed seed, that kora accepts compiles in the binding |
| `test_cel_server_validation_compiles_with_binding` | an expression the binding rejects is `IR011` at server-side validation without the binding's message, never a server error |
| `test_kora_compile_and_validate_report_same_location` | for faults kora's own parser, checker and estimate find, `kora compile` and `/validate` report the same code, segmented document path and code-point offset under the [shared diagnostic contract](diagnostics.md), with a character outside the Basic Multilingual Plane before the fault |
| `test_kora_compile_does_not_import_cel_binding` | a fresh `kora compile` process never loads the binding |
| `test_cel_projection_matches_full_binding` | evaluating over the stored projection gives the result of evaluating over the full value; a read of an `AgentSession` field outside its summary fields is `IR011` |
| `test_cel_projection_absent_optional_is_absent` | an optional field absent from an available projection is false to `has()`; an expired, never-cut or differently cut projection is `config_error`, and a manual retry needing it is refused with `API002` |
| `test_cel_projection_composes_through_collect_and_exports` | projections through `inputs[*].path`, the fan-out element, `collect`, exports, `$inputs`, `$params` and `PreviousAttempt.artifacts` carry every read path |
| `test_cel_scheduler_produced_outputs_are_projected` | a form response, a wait payload output, a `PreviousAttempt`, a collection and an export are cut in their producing transition; over a bound a wait payload output, `PreviousAttempt`, collection or export fails its producing node with `artifact_invalid`, while a form response is refused at receipt |
| `test_cel_projection_number_kinds_round_trip` | stored `int` and `double` values keep their kind on both databases |
| `test_cel_oversized_projection_fails_producer_with_artifact_invalid` | an over-bound projection fails its producer, including a staged target source, atomically |
| `test_cel_rejects_over_bound_data_at_entry` | an `integer` beyond 64 bits, or data over any bound, fails an output, is `API003` for a direct response and for parameters at trigger, webhook start and rerun, is `IR027` at deployment for static trigger `params`, and leaves a polled response `pending` |
| `test_cel_external_projection_over_bound_is_no_match` | an over-bound `payload` is no match and an over-bound `observation` is "not yet"; with two waits of different graph versions, one wait's wider path set does not change the other's result |
| `test_cel_retry_predicate_ignores_invalid_outputs` | an output that failed validation is absent to `has()` |
| `test_cel_branch_error_fails_with_config_error` | a `when` or `reason` error fails the branch with `config_error`, reason `cel_evaluation_error`, and direct targets become `upstream_failed` |
| `test_cel_approve_when_error_marks_request_failed` | on the API and the channel path the request becomes `failed`, the response is kept, the channel closes and the node fails with `config_error`. The direct response is HTTP 200 with status `failed`, `?status=failed` lists the request, and a later response is `applied: false` |
| `test_cel_approve_when_not_evaluated_without_authority` | responses refused for `API005`, `API006`, approver rules or a stale token are never evaluated; the request stays `pending` and the node is not failed, on both paths |
| `test_cel_retry_predicate_error_keeps_original_class` | a predicate error, including `projection_unavailable`, prevents the retry and keeps the attempt's class; `infrastructure` and `provider_limited` never evaluate the predicate |
| `test_cel_match_evaluated_once_per_wait` | each eligible event and wait pair is evaluated exactly once, and an error is no match and is visible. This holds across ticks and a restart, and when an event with a lower identifier commits late (a PostgreSQL barrier, and the serialized SQLite equivalent) |
| `test_cel_until_error_keeps_waiting_and_is_visible` | an `until` error keeps the wait open and the start binding idle, and records the error from the current claim only |
| `test_cel_until_error_does_not_degrade_integration` | repeated `until` errors leave the integration's failure count and interval unchanged |
| `test_cel_dyn_result_requires_exact_bool` | `observation.body.ready` returning `"false"` is `wrong_result_type`, never true |
| `test_cel_evaluation_allowance_yields_between_batches` | at the allowance, the pass commits, leaves `needs_scheduling` set and serves another run of another namespace; each transaction evaluates at least one expression; the rest continues after a restart |
| `test_cel_binding_exceptions_map_to_error_kinds` | each documented binding exception maps to its kind, with a null `offset`; unparseable strings in the conversion functions and `dyn` arguments of another kind are `invalid_argument` |
| `test_cel_invalid_argument_is_not_an_internal_fault` | a `RuntimeError` or `ValueError` raised during compilation is `internal_fault` with fault `runtime_error` or `value_error`, respectively. The error omits the native message. |
| `test_cel_maps_only_documented_native_errors` | native execution exceptions map to their documented kinds and faults, including `RuntimeError` to `invalid_argument` with a null fault and `ValueError` to `internal_fault` with fault `value_error`. A parser panic maps to `internal_fault` with fault `panic`. The error omits the native message and its cause. |
| `test_cel_cancellation_propagates_through_evaluation` | `asyncio.CancelledError`, `KeyboardInterrupt` and `SystemExit` raised during evaluation still propagate |
| `test_cel_persisted_errors_exclude_values` | no stored, returned or logged error carries expression text, values or binding messages. This includes binding messages that contain payload keys, DEBUG output and exception chains, in every sink: wait, binding, approval, event, API, CLI, MCP and logs |
| `test_cel_evaluation_error_reads_parity` | the error record appears in node, trigger-binding and approval reads, the Python client, CLI `--json`, MCP output and the TypeScript client |
| `test_cel_evaluating_entry_points_refuse_without_binding` | the API, scheduler and standalone refuse to start without `kora[cel]`, and `kora test` and `kora_test` return a structured error naming the extra, through an injected binding loader; `kora doctor` reports the missing extra and a binding at another version as a failed `cel` check, tested in a child process whose site-packages lack the binding or hold another version ([#1420](https://github.com/realkarych/kora/issues/1420)) |
| `test_cel_evaluating_entry_points_refuse_mismatched_binding_version` | a binding other than the pinned version, or a `cel` module from another distribution, prevents start; in the loader leg, another version, two matching distributions (also two metadata directories of the same or another version in one site), a RECORD hash other than SHA-256, a recorded file missing from disk or unreadable, a loaded module outside RECORD, without a file or naming another recorded file, a failing import and zero or two new panic classes are each refused with their reason, a refusal carries no cause or context, a second load in the same process repeats the first refusal, and a pip bytecode cache recorded without hashes passes the file check |
| `test_cel_loader_refuses_unchecked_code_before_it_runs` | a file differing from its RECORD hash, an unrecorded package directory, a directory that cannot be listed, and a symbolic link to a file, to a directory or to bytecode inside the package, or to the package itself, are refused before anything of the package runs, while importing the same package without the loader runs it |
| `test_cel_loader_refuses_a_shadowing_module_before_it_runs` | a `cel` package that `import cel` would load from outside the distribution is refused before it runs, while importing it without the loader runs it |
| `test_cel_panic_class_is_the_binding_class` | the panic class the loader resolves is the class of a real parser panic, not a decoy defined before loading, and a later load returns the same binding |
| `test_cel_binding_logger_never_carries_content` | a failing context function makes the binding log its message on the `cel` logger, and after loading no such record reaches a handler, also after `dictConfig` re-enables existing loggers |
| `test_cel_binding_loaded_event_is_safe` | in a process configured under another component, loading emits one `cel.binding.loaded` record at INFO, under the component `cel` and the logger `kora.cel_eval`, with the version as its only field, and leaves the binding's `cel` logger disabled; a load refused before or after the import emits none |
| `test_cel_conformance_suite` | every case `kora.cel` accepts runs through the binding as author text with its own bindings and agrees with cel-spec, except exactly the listed deviations, each asserted as the binding's exact outcome |
| `test_cel_depth_rule_matches_conformance_boundaries` | the cel-spec `nest` and `repeat` cases at the limit are accepted, and mixed parenthesis and operator nesting counts both |
| `test_cel_lexer_rejects_what_cel_rejects` | non-ASCII digits, letters and spaces and ASCII characters outside the grammar, the rejected control and format code points anywhere, raw carriage returns in comments and literals, surrogates, out-of-range and malformed escapes, `\u` and `\U` in bytes, the `0X` prefix, digit separators, out-of-range literals, empty hexadecimal literals and unterminated literals are `IR011` with their reason |
| `test_cel_rejects_unsupported_constructs` | message construction, optional syntax, leading-dot names, backtick fields, reserved identifiers and malformed syntax (unexpected input and unexpected end) are `IR011` with their reason; two-variable comprehensions are the three-argument legs of `test_cel_accepts_every_macro_form` |
| `test_cel_rejects_unknown_functions_and_malformed_macros` | functions outside the table and malformed `has()` and comprehension calls are `IR011` |
| `test_cel_rejects_functions_the_binding_lacks` | each narrowed standard function is `IR011` |
| `test_cel_accepts_every_table_entry` | every entry of the standard function table is accepted in its form and arity |
| `test_cel_accepts_every_macro_form` | every macro is accepted with two arguments and `map` also with three; `all` and `exists` with three are two-variable comprehensions, and `exists_one` and `filter` with three are malformed macros |
| `test_cel_prefix_floods_stop_at_the_depth_limit` | 4,096 code points of prefix or conditional nesting are `IR027` early, without a recursion error |
| `test_cel_reports_code_point_offsets` | offsets count code points, also after a character outside the Basic Multilingual Plane |
| `test_cel_work_is_linear_in_steps` | lexing, parsing, the pattern parser and the estimate take at most a fixed number of counted steps per code point on hostile and maximal input up to the length limit |
| `test_cel_rendering_reparse_is_bounded` | the rendering of every nesting shape at depth 32 re-parses under the bound of 2 × 32 + 1 levels |
| `test_cel_rendering_beyond_the_reparse_bound_is_refused` | a rendering deeper than 2 × 32 + 1 levels is `IR027` |
| `test_cel_rendering_expansion_is_at_most_sixteen` | a maximal bytes literal of non-ASCII characters renders within 16 times its length |
| `test_cel_paths_walker_follows_cel_indexing` | the walker resolves fields and non-negative positions and treats missing keys, positions beyond the list and type mismatches as absent |
| `test_cel_paths_never_load_the_binding` | parsing and resolving paths in a fresh process never imports the binding |
| `test_cel_regex_rejects_constructs_outside_subset` | every construct outside the supported pattern subset is `IR011` |
| `test_cel_regex_accepts_the_supported_subset` | every supported construct is accepted |
| `test_cel_regex_estimate_floors_wide_classes` | wide ranges and negated Unicode classes weigh at least `.`, and `+` weighs as `{1,}` |
| `test_cel_regex_subset_agrees_with_binding` | every pattern kora accepts compiles in the binding, and `''.matches(p)` evaluates over the pattern corpus |
| `test_cel_function_table_executes_in_binding` | every entry of the standard function table evaluates in the binding |
| `test_cel_cost_estimate_bounds_comprehension_results` | the result of `map` and `filter` is bounded by its range's total size |
| `test_cel_cost_estimate_closes_every_comprehension_scope` | a value carrying an iteration variable out of its comprehension leaves no unbounded term, and generated expressions never fail the estimate with an exception |
| `test_cel_cost_estimate_keeps_element_bounds_through_literals` | under an explicit 10,000,000,000,000-unit test budget, ranges built by `dyn()`, concatenation and conditionals over a large list keep its element bounds, so a nested quadratic macro over them is `IR027` |
| `test_cel_cost_estimate_shapes_filter_results_as_lists_of_keys` | `filter` over a typed map or record is estimated exactly as over `dyn`, both for concatenating its results and for indexing them, and indexing a filtered map's keys takes at least the memory of the same keys produced by `map`, less the bracket bytes of the two result lists |
| `test_cel_cost_estimate_counts_filtered_keys_as_range_elements` | under an explicit 10,000,000,000,000-unit test budget, a filtered map or `dyn` value of 300 entries is accepted alone, and iterating over it with a nested comprehension over the same value is `IR027` for steps, so the filtered result counts the range's entries |
| `test_cel_cost_estimate_reads_declared_fields_through_indexes_and_joins` | for `dyn`, map and record types whose element bound is smaller than a declared field, indexing the field by a literal or a variable key, through `dyn()`, inside a comprehension over the keys, and after a two-element list literal or a conditional joins the value takes at least the memory of selecting the field |
| `test_cel_estimate_is_linear_in_the_environment` | the estimate's counted steps grow by at least one per declared field and linearly with the number of declared fields, and stay small for shared bounds nested 32 deep |
| `test_cel_cost_estimate_charges_follow_the_table` | with one weight set at a time, each weight contributes exactly its formula: the evaluation base, conversion per byte and per value, node visits, iterations, the `map` and `filter` square, concatenation and collection literals per byte and per value, comparisons and `in` per byte and per value, conversions and string functions, pattern compile and match, the compile on a miss, and memory including a compiled pattern; only top-level variables are charged for conversion |
| `test_cel_cost_estimate_rejects_quadratic_macros_over_unbounded_lists` | under an explicit 10,000,000,000,000-unit test budget, a `map` over a `dyn` list at the default bounds is `IR027` for steps, and over a small typed list it is accepted |
| `test_cel_cost_estimate_survives_deep_shared_and_cyclic_environments` | an environment bound nested 3,000 deep, a bound that contains itself, distinct shared record types nested 32 deep and cyclic record types return an accepted result, and 16 chained shared bounds each 31 deep return a result rather than an exception |
| `test_cel_cost_estimate_does_not_depend_on_operand_order` | swapping the operands of `&&` around a variable whose declared bounds nest deeper than 32 leaves the step estimate unchanged |
| `test_cel_cost_estimate_cuts_whole_cycles_regardless_of_order` | two bounds that contain each other give the same estimate for an iteration over each, whichever is reached first |
| `test_cel_cost_estimate_refuses_inconsistent_bounds` | a child bound larger than its parent, a child over the data bounds, a negative size and a size that is not an integer raise `BoundError` |
| `test_cel_cost_estimate_refuses_malformed_bounds` | an element, a field value or a root that is not a value bound, a bound subclass at the root or under a field, a non-string field name, fields that are not a plain `dict`, including a mapping proxy, an environment entry that is not a `Variable`, an environment that is not a plain `dict` or has a non-string name, a variable type that is not a CEL type (including a broken record), a nullable type wrapping another nullable type, an empty mapping proxy passed to `check` as the environment, and a non-integer size inside a cycle, reached from either end, raise `BoundError`, each as its own case |
| `test_cel_cost_estimate_does_not_examine_unreferenced_variables` | an inconsistent bound of a variable the expression does not reference is not examined |
| `test_cel_cost_estimate_never_charges_dyn_additions_below_typed_ones` | a list of `dyn` additions is charged at least the memory of the same list of typed additions |
| `test_cel_lexer_accepts_cel_literals` | literals, escapes, comments, whitespace including a bare carriage return, folded negative literals and runs of unary operators, reduced as cel-go reduces them, render as stated |
| `test_cel_paths_accept_selection_and_literal_indexes` | paths of fields, string keys and positions parse, up to depth 32 (a root and 32 selections) |
| `test_cel_regex_rejects_invalid_patterns` | malformed patterns, dangling flag dashes, misnamed or repeated group names and ranges at class escapes are `IR011` |
| `test_cel_textproto_scalars_keep_their_kind` | the generic reader keeps numbers, words and strings apart |
| `test_cel_diagnostics_exclude_author_text` | no diagnostic message, field or exception carries an identifier, literal, pattern or invalid token from the expression |
| `test_cel_textproto_reader` | the reader parses the syntax the vendored files use |
| `test_cel_textproto_reader_refuses_unknown_input` | unknown fields, malformed syntax, integers outside their type's range or grammar, invalid UTF-8, untyped descriptions, unnamed objects and repeated bindings are refused |
| `test_cel_conformance_cases_decode_their_values` | vendored values, bindings and declarations decode to their typed form |
| `test_cel_conformance_data_matches_manifest` | every vendored file and the licence match their recorded SHA-256 |
| `test_cel_conformance_cases_parse` | every included case parses and renders with kora, or gives exactly the reason of its listed rejection; each listed rejection matches a case, each rejection's reason belongs to its feature, a control-character rejection points at a rejected character, a quoted-field rejection holds a backtick, and a rejection for a narrowed or undeclared function names a function inside or outside the cel-spec standard library accordingly |

The compact cost regression of section 7.6 and the evaluator lifetime suite are active; the parser's linearity is a counted-steps test above. The owner-approved [CEL numerical calibration exclusion](../reviews/ai-development-setup.md#cel-numerical-calibration-exclusion) removes exhaustive coefficient fitting, the 16 MiB RSS-envelope claim, GIL calibration and native crash-threshold searches as current gates. These are removed, not passing measurements or pending pre-consumer calibration. The compact suite measures only its fixed cases; the existing lifetime suite retains its native-memory and resource-release checks.

The following consumer measurements remain `pending implementation`, owned by the work items in the [implementation plan](../../docs/implementation-plan.md#8-observability-threat-model-evidence-and-measurements). Neither the compact regression nor the logical allowance satisfies them:

- **Validation:**
  - parse, check and estimate time on hostile input, including class-heavy regular-expression literals;
  - validation of a 1,000-node IR;
  - a maximal 4 MB `/validate` request;
  - compile time of maximal canonical renderings in the real validation path.
- **Allowance and fan-in:**
  - transaction duration at the allowance with cheap expressions over 4 KiB / 256-value bindings;
  - one event against 1, 100 and 1,000 waits;
  - projecting a 4 MB event;
  - a 1,000-body fan-out with branches, and collecting 1,000 child projections;
  - NFR-3.1 latency of an unrelated run while evaluations are at the allowance;
  - approval and start latency under concurrent maximal `/validate` requests;
  - event-loop lag in standalone.
- **Lifetime:**
  - real consumer integration with compile/evaluate errors, cancellation and shutdown with queued jobs;
  - repeated in-process standalone start and stop cycles, as `kora test` runs them: threads, caches and RSS;
  - growth of `event_evaluation` rows (open waits times eligible events), the duration of closing and cancelling transactions with maximal rows, their batched background release, and the NFR-3.1 latency of unrelated runs meanwhile, on both databases;
  - a collection re-cut over 1,000 maximal child projections with narrow collection paths;
  - `kora_test` cancellation with a queued and an in-flight job, and host exit;
  - the compiled-program cache with more versions than its bound;
  - metadata growth and listing latency with maximal inline projections on both databases.

Before the first author-expression consumer, the compact regression and golden checks (#167), schema-driven checking (#337, #330), projections and the correction of native minimum-int unary negation (including dynamically computed minimum integers, #372) must land, together with that consumer's real integration evidence. The binding keeps its documented deviations; typed checks avoid the wrapping negation by rewriting integer negation (section 7.2). Persistence, scheduler/API decisions, surface parity and consumer diagnostic deduplication remain pending their implementing work items. The 1,000-node and five-second scheduling requirements, fairness and transaction measurements remain mandatory when those consumers activate. The existing `cel` diagnostic activation row and future API/scheduler error ownership in [observability.md](observability.md#7-component-coverage-and-activation) are unchanged. The local/CI compact and lifetime commands are in [development](../../docs/development.md), with lifetime evidence in the [evaluator research record](../research/cel-evaluator-lifetime-2026-10-21.md).

### 7.10 Prompt templates

Run full Jinja2 in a sandbox for `prompt`, `system_prompt_append` and each `cmd` element (DD-11).
Use the variable names in [section 7.1](#71-expression-kinds-and-environments) without applying CEL projections.
The sandbox disallows filesystem loaders, imports, includes and Python attribute access beyond the bound data.

Seal the authoritative rendered content once in the nonsecret execution request before preparation or execution effects.
The Worker prepares a proposal and the API independently derives and compares the complete result before upload readiness or the first inline seal.
A first seal of a ready upload uses its checked identity.
The attempt owns the saved content and availability, and seal replay and recovery reuse the recorded bytes.
The [execution request contract](execution-request.md#3-trusted-derivation-and-finite-admission) owns renderer admission and actual stop controls.
Seal activation belongs to #1453, and upload-to-ready rendering belongs to #1640. [ADR-0018](../../docs/decisions/0018-immutable-attempt-history.md)

A graph with a webhook trigger may use run parameters in prompts.
Using them in a `cmd` element or `workspace.rev` is `IR016` for that graph.

## 8. Triggers (FR-6.2)

```yaml
triggers:
  - { kind: manual }
  - { kind: cron, expr: "0 6 * * 1", timezone: Europe/Moscow }
  - { kind: webhook, name: optimize_target, auth: { kind: hmac, secret: WEBHOOK_SECRET }, params_from: payload }
  - { kind: webhook, name: optimize_from_ci, auth: { kind: bearer, secret: CI_TOKEN } }   # bearer deliveries also carry a timestamp and a nonce
  - { kind: sensor, poll: { sensor: http, config: { url: "…" }, until: "observation.body.ready == true" }, interval: "15m" }
  - { kind: graph, graph: upstream_graph, on: run_succeeded }
```

Each trigger variant requires its operational fields: cron `expr`; webhook `name` and `auth`; sensor `poll` and `interval`; graph `graph` and `on`. Manual triggers require only `kind`. Fields belonging to a different variant are errors. `params` and `x-` metadata are common fields; `timezone` is cron-only and `params_from` is webhook-only. Semantic validation still checks cron syntax, timezone names, sensor-specific configuration and secret alias resolution.

Every accepted start produces a pinned pending run or a durable no-run overlap-skip outcome, with an idempotency key (section 13 of [data-model.md](data-model.md), ADR-0020). There is no acknowledged unconverted trigger queue. Matching accepted replay precedes current graph/schema/target validation and quotas; full outstanding capacity is a structured retryable refusal, not an accepted delivery. Overlap handling is `graph.overlap`: `allow`, `skip` or `queue` (DD-12). When the field is absent, runs fired by a webhook trigger use `queue` and runs from every other trigger kind use `allow` (FR-6.2, ADR-0006). Skip considers accepted pending and running graph runs; queue promotion selects the oldest pending run under the graph lock only when no graph run is running. Save resolved overlap on each accepted run. Explicit allow and same-run retry retain their semantics. Cron fires forward only; there is no catch-up of missed fire times in v1 (DD-37). Webhook authentication is either an HMAC body signature in the Standard Webhooks shape or a bearer token with a timestamp and nonce inside a replay window, chosen per trigger; the secret is a reference into `secrets.aliases` (DD-17); the delivery's idempotency key is derived from signature and timestamp. Webhook payload fields map only into run parameters validated against `graph.params` (current contract [protocols](protocols.md)).

**Target** (FR-6.11, [ADR-0014](../../docs/decisions/0014-target-identity.md)). `graph.target` is optional and names the business target a run works on, so that two correct triggers for one target follow a declared policy and the target's history is readable across runs ([api-and-agent-surface.md](api-and-agent-surface.md) section 2). `key` takes one of two forms: `{ param: package }` binds `run.target_key` when the run is created, from the named run parameter, which must be a string in `graph.params`; `{ from: find_target, output: target, path: task_id }` binds it when the complete source result is successfully accepted and promoted, from the field the path names in its payload, which must resolve to one string. An absent optional parameter leaves the run unbound with no target policy; an invalid present key is validation failure, not absence. The source resolves to one top-level `graph.nodes` ID, not a dynamic body instance. All its outputs, including implicit results, remain staged until successful atomic acceptance of the complete result, its key and source success (ADR-0019). Once bound, the key never changes or clears, including after hold release; same-key rediscovery is accepted and a different valid key rejects the whole production with non-retryable `config_error`, reason `target_identity_conflict`. Missing/malformed key data or incomplete results are invalid output, never a partial binding. The key is opaque: it matches `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`, is written to the run row only and is not a template variable; the parameter or output field it is read from stays under the existing rules. `on_active` says what a new run does while another run of the namespace holds the same key: `allow` (when absent) proceeds, `defer` waits at admission until the holder leaves `running` and all blocking execution/publication recovery is settled; acquired holds survive cancellation during an observation gap ([scheduler.md](scheduler.md) section 6); `defer` cancels nothing and closes nothing. `overlap` is evaluated first, at trigger time and per graph, so a graph that declares `defer` sets `overlap: allow`. A declaration whose parameter, top-level node, output or scalar path does not resolve to a string is `IR020`. Equality is case-sensitive with no normalization. A wrapper/collector source checks its exported complete result, not each body instance; its conflict cannot be ignored by skip_failures or converted into an automatically retried body failure. A selected collection path must resolve to one string; missing/out-of-range values fail rather than selecting another element. A rerun has independent discovery, with reliable selection supplied by the graph. Target identity does not certify business-object correctness or roll back already produced effects; ordinary pre-binding/approval-only target admission remains separate. ADR-0020 governs execution occupancy and bounded acceptance without changing these target boundaries.

## 9. Limits (FR-6.5, FR-6.6, FR-9.4)

**Concurrency is live admission policy** (FR-6.10, [ADR-0020](../../docs/decisions/0020-execution-capacity-and-bounded-run-acceptance.md)). `runtime.concurrency.max_running_nodes` counts non-terminal physical attempts and `max_running_runs` distinct runs having any such attempt, including historical/unknown work; no-attempt waits reserve neither. Counts span all versions of a graph. Caps come from its highest-numbered active version, falling back to its newest deployed version if none is active, and apply to older pinned runs for admission only. An absent cap adds no graph restriction; other scopes still apply. Public state names the cap-source version. Execution topology, inputs, target declaration and attempt semantics stay pinned. Lowering caps does not evict work, and another attempt in an already counted run adds no run slot but still obeys attempt caps. Namespace `max_pending_runs` is a separate positive finite outstanding-acceptance bound, default 1000, not an IR run-status counter. The complete state table and same-database contracts are in data-model section 13.

| field | level | kind | enforced by |
|---|---|---|---|
| `timeout` | physical node attempt, subgraph instance, run | hard policy | earliest configured inherited UTC deadline; API/scheduler guard authority and runtime/common executors interrupt/escalate locally through all managed phases without API connectivity; nominal grace/fault model in ADR-0022 |
| `max_events` | agent node | hard | session runner, counting turns, tool calls and tool results in the SDK event stream; at most 100,000, the ceiling the runner also tracks when the field is absent ([protocols.md](protocols.md) section 3); class `events_exhausted` |
| `budget_usd` | agent node, subgraph, run | soft | agent node: session runner from streamed usage, also passed to the harness where supported; subgraph and run: the scheduler from the sum of usage records and live counters, admitting no further attempt in the scope while running ones finish ([scheduler.md](scheduler.md) section 4); class `budget_exhausted` |
| `budget_headroom_pct` | `runtime` section | – | tolerated overshoot before termination (NFR-8.2) |
| `stall_after` | agent node; runtime default `15m` | hard | session runner, from silence in the SDK event stream; class `stalled` (DD-15) |

The session runner inside the runtime is the only enforcer of the attempt-level limits `max_events`, `budget_usd` and `stall_after` (current contract [ADR-0022](../../docs/decisions/0022-inherited-execution-deadlines.md)); the worker relays its counters to the control plane and never enforces; the scheduler owns aggregate run/subgraph budget decisions, which no single session can calculate. Timeouts instead propagate the earliest already-known scope deadline into every managed phase (ADR-0022). Run time starts at promotion, subgraph time at first ready activation, node-attempt time at first executing claim before preparation. Existing parent clocks survive retries/clear/adoption. Missing optional limits add no deadline. Interruption and forced-stop escalation have nominal thresholds at `D` and `D + 30s`; grace never restarts on recovery, and actual stopping/remote effects are not a hard-real-time promise. See [scheduler section 17](scheduler.md#17-inherited-deadline-enforcement-adr-0022).

## 10. Validation rules (`kora validate`)

Installation-aware IR026 and IR019/IR028 checks share the [same-call trusted validation-facts boundary](validation-facts.md), selected under #596. Server composition owns configured selection, native harness/runtime owners supply complete facts, and IR consumes a detached immutable view without provider loading. Incomplete required protections cannot establish successful validation. The [public failure disposition](validation-facts.md#7-public-disposition-of-incomplete-trusted-facts), accepted under #628, preserves established prerequisite findings and uses API010/API011 for operation failures without a partial public collection. The [mandatory coverage rule under #630](validation-facts.md#8-repository-trust-and-mandatory-policy-coverage) also applies when repository configuration is trusted, without removing its bypass residual or first-slice refusal. The [complete policy-coverage requirements under #636](validation-facts.md#9-complete-policy-coverage-requirements) add residual `other_tool`, preserve orthogonal subagent coverage and account for whole-layer allow exclusions, edit-shell guards and unconfined patterns. Codex nonempty allow arrays are refused under the conservative requirements, including its uncovered hosted-tool kind. The [#1224 rule](validation-facts.md#selected-linux-codex-admission) limits each effective nonempty layer of the selected Linux profile to deny-only policies whose fully derived primary kinds are covered and confined to shell and file edits. A separate qualified fact for the exact configured profile can discharge only the additional subagent requirement. An otherwise-unreported required absence fact follows section 7, preserving known scoped failures and matching receipt precedence. Concrete producers, configured selection, native classification/normalization, executable requirements, records and whole-validator integration remain under #549; these design decisions do not activate the checks or move HTTP error ownership into IR.

The declared-secret-alias pass, `kora.ir.validate_secret_aliases`, is implemented under [issue #525](https://github.com/realkarych/kora/issues/525). It accepts a structurally validated `GraphDocument` whose caller-owned maps remain unchanged during the synchronous call. Exact alias-map membership is checked for agent and command `config.secret_env`, each agent MCP server's own `secret_env`, and graph webhook `auth.secret`. Root and all stored bodies, including unused/shared definitions, are visited once without expanding invocations. Missing `secrets`, omitted `aliases` and an empty alias map declare no aliases. Reusing a declared alias is valid; declaration values are never read.

Each undeclared reference produces fixed `IR007` at its referring safe segmented path with null offset. Typed node/body/environment identifiers remain path metadata; unrestricted MCP server names and MCP environment keys always use `<field>`. Node references use only revalidated node source, webhook references only graph source; unsafe source is omitted without ancestor fallback. Independent errors continue through the existing bounded collector and omission counts. `SecretAliasResult.secret_aliases_valid` describes membership only, with counts for visited nodes, stored definitions, every graph trigger, every MCP server and checked references. Results retain no document, alias map or traversal state.

No provider is resolved or listed and no process environment is read. Opaque metadata/payloads and ordinary `env` values are not scanned. Namespace-prefix authorization, provider availability, reserved environment names, secret egress/isolation policy, value-like `env` detection, installation admission and public graph execution remain separate. Existing provider configuration remains authoritative for secret-prefix policy. [Evidence and limits](../research/secret-alias-membership-evidence-2026-10-31.md) name the owning checks and observability activation.

`kora.ir.validate_graph_links` requires a structurally validated `GraphDocument` whose caller-owned maps stay unchanged during the call.
`GraphLinkResult.links_valid` covers local producer/output and control links, body definitions and exports, fan-out names, branch ownership, dependency and definition cycles, target producer/output names and `independent_of` input membership.
It checks each stored body once, including unused bodies, without expanding call instances.
Raw-document admission, reserved-source paths, retry, types and contracts, expressions, provider and installation checks retain their own passes.
The scoped result confers no execution or public-operation authority. [Graph-link scope](https://github.com/realkarych/kora/issues/463)

The known declared target-parameter type pass, `kora.ir.validate_known_target_parameter_types`, is implemented under [issue #554](https://github.com/realkarych/kora/issues/554), with the structurally validated, unchanged-snapshot precondition. It owns only a root `ParameterTarget` and compares the exact declared property of an authored `graph.params` schema mapped by the existing PLAIN `SchemaTypes` to a `Record`. A known STRING primitive, including string enum/const metadata, is string; another known non-Dyn type produces fixed `IR020` at `graph.target.key.param`. Nullable is not unwrapped, and optional string declarations remain legal. Only revalidated graph source survives, with null node and offset; no parameter/type/schema/reference text enters the finding or scalar completion event.

Absent parameters schema, mapper `SchemaError`, a non-Record root, an undeclared property or Dyn leaves this narrow comparison unresolved without a secondary diagnostic. It does not infer missing-name refusal, open-map lookup, omitted-schema defaults, parameter values or full target validity. The [omission contract](#graph-parameter-schema-omission) is settled; this partial implementation does not yet consume its effective schema. No target and OutputTarget have zero obligations; nodes, bodies and triggers are not visited. `KnownTargetParameterTypeResult` retains copied bounded diagnostics, checked/resolved/unresolved target counts and existing mapper step counts; checked equals resolved plus unresolved. `known_target_parameter_types_valid` requires no errors and zero unresolved, proving only this scoped comparison. The result retains no input/mapper/type state. Full IR020 composition remains with work item 3, public validate/deploy aggregation with work item 11; first-slice graph.target IR028 and target-runtime/admission ownership remain unchanged. [Evidence and limits](../research/target-parameter-types-evidence-2026-10-31.md).

The root previous-attempt pass, `kora.ir.validate_root_previous_attempt_refs`, is implemented under [issue #546](https://github.com/realkarych/kora/issues/546), with the same structurally validated, unchanged-snapshot precondition. It checks only authored inputs of root `graph.nodes`, for all node kinds. Every exact `from: $previous_attempt` receives fixed `IR006` at `graph.nodes.<node>.inputs.<input>.from`, with the owning node ID, only its revalidated source and null offset. Optional inputs, node retries and retries of a referenced body cannot place a root declaration inside an enclosing subgraph iteration. Independent failures continue through the existing bounded collector and omission counts.

`RootPreviousAttemptResult.root_previous_attempt_refs_valid` proves only this root exclusion; `root_nodes_checked` counts every root node and `root_inputs_checked` every authored root input. Stored bodies are not traversed, including unused/shared/recursive definitions. A true scoped result establishes no body retry eligibility, nested/default interpretation, PreviousAttempt payload/type, installation admission or public execution authority. The pass retains no document or traversal state and changes no graph-link behavior. [Evidence and limits](../research/root-previous-attempt-evidence-2026-10-31.md).

The declared tool-policy pass, `kora.ir.validate_tool_policies`, is implemented under [issue #518](https://github.com/realkarych/kora/issues/518), with the same structurally validated, unchanged-snapshot precondition. It checks authored agent `config.tool_policy` in root and every stored body once, including unused/shared definitions, without invocation expansion. The single pure compiler in `kora.ir.tool_patterns` preserves section 4.1 / protocols section 12 grammar and existing capacities; `tool_policy_refusals` adds the accepted kora-owned exact `StructuredOutput` name prohibition for node or future namespace callers. Provider validation/envelopes, runtime matching and coverage ownership are unchanged.

Fixed IR013 uses the owning `config.tool_policy` root for aggregate limits, allow/deny for array limits and an indexed array path for pattern refusals. Only safe owning-node source and node identity survive, with null offset and no ancestor fallback. Oversized arrays suppress only their member compilation; independent array/layer checks continue. `ToolPolicyResult` retains bounded copied diagnostics and node/definition/policy/pattern counts; patterns count all authored entries, including overlarge arrays. `tool_policies_valid` establishes only these static checks. No tool matching, filesystem resolution, namespace consumer, capability/installation/public admission or execution authority is added. [Evidence and limits](../research/tool-policy-validation-evidence-2026-10-31.md).

The type-declaration, saved-schema and inline-schema passes require the same structurally validated snapshot to remain unchanged during the call.
Each visit count describes only that pass, and an invalid prerequisite suppresses only dependent checks.
These results confer no execution admission or public-operation authority.

`kora.ir.validate_type_declarations` checks all supplied contracts, node input/output types and stored-body interface types, including unused definitions.
`TypeDeclarationResult.declarations_valid` covers balanced `List[T]` spelling (`IR003`) and declared bases, qualified custom names, saved parents, parent cycles and published built-in revisions (`IR022`).
Cycle findings identify members rather than descendants.
The result counts declarations and contracts. [Declaration evidence](https://github.com/realkarych/kora/issues/468)
Published recognition covers revision 1 of VcsRef, StructuredOutput, AgentSession and CommandResult.
Schema/reference closure, behavior binding, effective compatibility, edge assignability, CEL and execution admission require their own checks.
There is no installed behavior or list-materialization authority in this result.

`kora.ir.validate_type_schemas` checks the draft 2020-12 metaschema and offline reference closure of every saved contract, including unused entries.
`TypeSchemaResult.schemas_valid` covers those two checks.
The four published revision-1 schemas ship as immutable package resources, and even a built-in reference requires the supplied contract entry.
The result counts contracts, schema positions (including boolean schemas) and references.
The structural layer admits the root dialect, and nested unsupported dialects yield `IR022`.

An offline reference has an empty base for the current schema or an exact artifact URN from the supplied saved closure, with an optional empty or JSON Pointer fragment.
Fragments use percent-encoded UTF-8 and RFC 6901 escapes.
Array indices use canonical nonnegative decimal spelling.
The target must be a recognized schema position, including compatibility `definitions` and schema-valued `dependencies`.
Annotations, enum, default, const and unknown extension data supply no positions.
External URLs, relative paths, anchors, unresolved targets and nested `$id` resources are refused.
Root `$id` creates no alias and anchor declarations create no lookup authority.
Reference cycles are legal and never expanded.
`$dynamicRef` has the same initial-target restriction, so admitted targets resolve as static references.
`$recursiveRef` receives the same closure check without enabling an older dialect.
These offline rules retain the [checker boundary for nested resources](#74-schema-driven-checker).

Typed keyword preflight keeps malformed nested values out of native error rendering.
The pinned metaschema checks bounded-depth, fixed-representation keyword views and continues across independent entries after refusal.
The metadata format checker checks regex syntax independently of optional URI-format packages.
Regex compiler capacity failures yield fixed `IR022`.
Dialect and URI authority follow the rules above, and URI annotations do not add format guarantees.
Payload values and their `format` interpretation are outside this pass.
The pass adds no depth or capacity quota.
Effective compatibility, installed behavior, edge assignability, CEL, admission and aggregates remain separate.
[Saved-schema scope](https://github.com/realkarych/kora/issues/473) and [evidence](../research/type-schema-validation-evidence-2026-10-30.md) record resource and diagnostic ownership.

`kora.ir.validate_inline_schemas` visits the root and each stored body once, including unused bodies, without expanding calls.
It checks each authored `graph.params`, node `outputs.*.schema`, subgraph `inputs.*.schema`, agent `config.output_schema` and human-input `config.form` as a separate schema root.
Missing fields stay missing and an authored `{}` is checked.
An omitted `$schema` selects draft 2020-12 without modifying the document, and a different explicit dialect fails.

`InlineSchemaResult.inline_schemas_valid` covers metaschema validity and offline reference targets.
Local references and cache keys use the owning field's root.
Only available supplied saved contracts define artifact URNs, and inline `$id` values create no aliases.
An unavailable built-in fails when referenced, while rules for unrelated saved definitions remain with the type passes.
The result counts nodes, definitions, inline roots, schema positions and references.
Effective schema equality, approval defaults and predicates, inheritance, payload validation, providers, CEL and execution admission have their own checks.
[Inline-schema scope](https://github.com/realkarych/kora/issues/476) and [evidence](../research/inline-schema-validation-evidence-2026-10-30.md) record the scoped verification.

The separately planned [StructuredOutput execution profile](artifacts/StructuredOutput.md#32-initial-executable-schema-surface) is an additional support-admission boundary, not a change to these structural/metaschema/closure results. It excludes regex and unevaluated-item/property keywords in relevant schema positions, cyclic potential validation calls and lone surrogates in retained schema/input text; structurally valid IR using them remains representable. The complete accounting/parameters/charging specification is approved under #515, while the reserved policy supplies no executable effective-schema support until all named implementation and verification gates pass. Work items 3/8 retain the [named preparation/evaluation/composition gates](artifacts/StructuredOutput.md#5-verification-and-activation), including fixed refusal distinct from genuine schema mismatch. Existing recursive reference closure and schema syntax checks retain their meanings.

The output-consistency pass, `kora.ir.validate_output_schemas`, is implemented under [issue #479](https://github.com/realkarych/kora/issues/479), with the same structurally validated, unchanged-snapshot precondition. It checks every agent and human-input node in the root and all stored bodies once, including unused bodies. An agent may have at most one non-list `StructuredOutput` or saved descendant, and at most one exact `VcsRef`. List wrappers are not stripped, and custom VcsRef descendants do not occupy the exact VcsRef role. Known structured outputs are compared with `config.output_schema` only when both that field and the output declaration's schema are authored. Neither omission nor a harness-only schema establishes another artifact role or inserts a schema.

An explicit human-input response schema is compared with the authored form, or with the published approval form only when `config.form` is absent. An authored `{}` remains custom. Comparison never inserts form, predicate or response defaults. The shared fresh-value default-form factory also supplies the unchanged generated IR-schema metadata.

Schema equality is structural JSON-value equality: object-key order is ignored, array order is significant, and booleans, numbers, strings and null retain distinct JSON kinds. Numerically equal integers and finite floats compare equal, including `1` and `1.0` and signed zero, without coercing large integers through floating point. References, annotations and explicit dialect fields are compared literally; no resolution, normalization or semantic schema equivalence is attempted. An iterative paired traversal stops at the first difference, retaining one iterator per active container without copying its children.

`OutputSchemaResult.output_schemas_valid` covers only established role cardinality and these authored comparisons. Missing, cyclic, unqualified or unpublished saved ancestry suppresses dependent StructuredOutput checks; existing declaration/schema passes still own their `IR022` findings. Independent exact-VcsRef and form checks continue. Per-call ancestry memoization includes unknown results and every visited prefix. `ancestry_steps` counts first contract-map lookups, including missing names, rather than cached uses; `values_checked` counts compared JSON-value pairs rather than keys or bytes. Other counters expose nodes, stored definitions, applicable output declarations and comparisons. This result can be valid while prerequisite passes fail. It does not establish inherited-schema compatibility, payload validity, approval predicates, provider support, CEL typing or execution admission. [Evidence and limits](../research/output-schema-consistency-evidence-2026-10-30.md).

`kora.ir.validate_edge_types` takes a structurally validated snapshot that remains unchanged during the call.
It checks each node input in the root and every stored body once, including unused bodies, without expanding call instances.
Whole-artifact references resolve only within the owning body.
All node kinds and optional inputs follow the same rule.

Each node input contributes once to one category, in this order.

1. **Excluded.** An authored `path`, including `""`, or `$params`, `$inputs` or `$previous_attempt` stops comparison.
2. **Unresolved.** The `from` or `output` is omitted, the named producer or output is missing, either spelling is unbalanced, or either base lacks valid saved ancestry.
   Missing, cyclic, unqualified and unpublished ancestry leaves descendants and identical names unresolved, even with unequal list depths.
3. **Checked.** Iterative parsing yields the base and list depth, with depth zero for a scalar.
   Both bases must resolve before comparing depths.
   Compatibility requires equal depth and a producer base equal to or descended from the input base in the saved closure.
   A known mismatch reports `IR003` at the consumer's `inputs.<name>.type`.

Published built-in name/revision pairs and namespace-qualified custom types with null parents root the ancestry forest.
An iterative reverse-forest traversal indexes half-open intervals with one preorder counter across roots.
Balanced spelling follows the declaration check, and output-role cardinality uses the original declaration without unwrapping lists.

`EdgeTypeResult.edge_types_valid` means there is no established nominal mismatch.
The result counts nodes, definitions, inputs, examined contracts, indexed valid types and checked, unresolved and excluded inputs.
The link and declaration passes retain their diagnostics and codes, and independent known edges still get checked.
The pass leaves authored fields intact and uses saved nominal ancestry without schema contents, installed behavior or current aliases.

The result may be valid even when inputs are excluded or a prerequisite pass fails.
Inherited CEL properties, schema subsumption, payload validity, path and reserved-source typing, list admission and materialization, body interfaces, export and collection mappings, provider support and installation or execution admission keep their own checks and activation rules.
The [scalar](https://github.com/realkarych/kora/issues/482) and [list](https://github.com/realkarych/kora/issues/495) scopes have separate [scalar evidence](../research/direct-edge-type-validation-evidence-2026-10-30.md) and [list evidence](../research/list-edge-type-validation-evidence-2026-10-30.md).

The subgraph-export type pass, `kora.ir.validate_export_types`, is implemented under [issue #497](https://github.com/realkarych/kora/issues/497), with the same structurally validated, unchanged-snapshot precondition. Every stored definition is counted once, including unused bodies and bodies with absent/empty exports. Every declared export is compared against its same-body producer's named declared output; call instances are not expanded. `TypeAncestry.accepts_type` shares complete spelling and saved-base assignability with the direct-edge pass, preserving its classification and output-role boundaries.

Missing producers/outputs, malformed endpoint spelling or unavailable saved ancestry leave that export unresolved, including equal unknown names and unequal depths. Only known incompatible endpoints produce fixed `IR003` at `graph.subgraphs.<body>.outputs.<name>.type`, using the body's safe source with null node and offset. Independent known comparisons continue. `ExportTypeResult` counts definitions, visited exports, contract entries, indexed valid types, compared exports and unresolved exports; compared plus unresolved equals visited exports. `export_types_valid` means no established mismatch and may coexist with failed prerequisite passes.

This pass changes no authored data and consults no schema contents, behavior bindings, providers, payloads or current aliases. Wrapper mappings, fan-out over/collect, inherited schema compatibility, aggregate materialization and public execution admission remain separate. ADR-0027 and the existing AgentSession, StructuredOutput and CommandResult aggregate-consumer gates remain active. [Export validation evidence and limits](../research/subgraph-export-type-validation-evidence-2026-10-30.md).

The ordinary-subgraph interface pass, `kora.ir.validate_subgraph_interfaces`, is implemented under [issue #505](https://github.com/realkarych/kora/issues/505), with the same structurally validated, unchanged-snapshot precondition. Root and every stored definition, including unused/shared bodies, are scanned once without invocation expansion. Only ordinary wrappers compare input/output maps under section 4.4. Missing bodies and invalid spelling/ancestry leave dependent comparisons unresolved; declared exports remain authoritative even when internal links fail.

Fixed IR003 owns missing wrapper outputs, missing/extra inputs and known input/output type mismatches, at the wrapper member or its `type` child with node-only safe source, node ID and null offset. Existing graph-link IR002 remains the sole owner of extra wrapper outputs; this pass counts those names unresolved without duplicate diagnostics. Independent known findings continue. `SubgraphInterfaceResult` holds bounded copied diagnostics and counters; input/output union visits partition into compared types, unresolved types and owned name mismatches. `interfaces_valid` means no established mismatch owned by this pass: extra outputs alone can coexist with true while graph links fail. No full validity, payload/aggregate admission, schema compatibility, fan-out mapping, public operation or execution authority is established. [Evidence and limits](../research/subgraph-interface-validation-evidence-2026-10-30.md).

The fan-out declared-type pass, `kora.ir.validate_fan_out_types`, is implemented under [issue #503](https://github.com/realkarych/kora/issues/503), with the same structurally validated, unchanged-snapshot precondition. It visits root and every stored body's nodes once, including unused/shared/nested bodies, and checks only fan-out nodes without expanding invocations. It constructs one saved ancestry index. The local `over` declaration is checked independently of its upstream producer, reserved source or selected path. A known scalar yields fixed `IR004` at `config.over`. Every declared collection output resolves the referenced body's named export and applies section 4.4's one-added-list-level relation. Known incompatible types yield fixed `IR003` at `outputs.<name>.type`; the diagnostic uses the fan-out node ID and only that node's revalidated source, with no semantic offset.

Missing local inputs, bodies, exports or collect names, malformed spellings and unavailable saved ancestry remain unresolved here. Ancestry is established before shape comparison, including identical unknown names and unequal depths. Existing link/declaration passes own those diagnostics; independent known errors continue, without requiring globally successful prerequisite passes. `FanOutTypeResult` counts visited nodes/definitions/fan-outs/collection outputs, contract entries, indexed valid types, checked/unresolved mapped inputs and compared/unresolved collections. Checked plus unresolved mapped inputs equals visited fan-outs; compared plus unresolved collections equals visited collection outputs. `fan_out_types_valid` only means no established mismatch, not successful full graph validation or execution admission.

The pass changes no authored fields and consults no schema contents, behavior bindings, current aliases or payloads. Ordinary wrapper mappings, upstream/path typing, effective-schema evaluation and aggregate admission/materialization remain separate. Runtime gate #71, ADR-0027 and all existing artifact aggregate-consumer gates stay active. [Fan-out type evidence and limits](../research/fan-out-type-validation-evidence-2026-10-30.md).

The expression/path syntax pass, `kora.ir.validate_expression_syntax`, is implemented under [issue #487](https://github.com/realkarych/kora/issues/487), with the same structurally validated, unchanged-snapshot precondition. It checks every authored operational CEL field: branch conditions/reasons, node and stored-body retry predicates, approval predicates, event matches, wait/trigger sensor predicates and `runtime.infrastructure_retry.predicate`, including disabled policies and unused bodies. It also parses node input paths, both agent/command workspace references, approval context and every binding path, and output-target paths. Each stored body is visited once without expanding calls. Absent fields remain absent; structurally permitted empty paths are parsed and refused.

`ExpressionSyntaxResult.syntax_valid` covers existing parser syntax and static parser limits only. Each independently invalid field contributes one fixed `IR011`/`IR027`, at its exact field path with the parser's code-point offset. Node fields use their node's safe source, body predicates use body source, sensor-trigger/target paths use graph source with no node, and runtime predicates have neither. Unsafe source labels are omitted without ancestor fallback. Counters record every visited node, every stored definition once, every authored graph trigger including non-sensors, and every expression/path parser invocation including failures. Enumeration is lazy; parser tokens/trees and field state are not retained in the result. [Evidence and ownership](../research/expression-syntax-validation-evidence-2026-10-30.md).

This pass interprets no opaque metadata, schema content, commands or prompt templates. It neither inserts approval defaults nor checks contextual root eligibility, names/fields, expression result types, rendering round-trip, binding compilation, cost admission, path resolution, execution or installation/public admission. Syntactically valid unknown names, wrong roots and wrong result types may pass. Length rejection above 4,096 code points remains at structural admission; this pass does not bypass that boundary or activate the raw-document error converter.

The node-local input path pass, `kora.ir.validate_input_paths`, is implemented under [issue #491](https://github.com/realkarych/kora/issues/491), with the same structurally validated, unchanged-snapshot precondition. Shared lazy enumeration selects only agent/command `config.workspace.from_ref`, human-input `config.context`, `config.binds.vcs_ref` and every indexed `config.binds.gate_digests` entry. It parses each authored field with the existing path parser, then checks the first two steps against the node's own input map. Root and every stored body, including unused bodies, are visited once without call expansion. Missing fields stay absent and the document is unchanged.

Each established binding refusal emits one fixed `IR002` or `IR005` at the exact owning field with its node ID, revalidated node source only and null offset. Unsafe source is omitted without ancestor fallback. Parser `DiagnosticError` failures produce no duplicate `IR011`/`IR027`; independent fields continue. `InputPathResult` counts every visited node, every stored definition, every parsed field (`paths_checked`), successful parses including semantic refusals (`bindings_checked`) and parser failures (`paths_unresolved`). Always `paths_checked == bindings_checked + paths_unresolved`. `input_paths_valid` requires both a valid diagnostic collection and zero unresolved paths. Results retain no document, parser or path objects.

This scoped guarantee proves only node-local declaration membership. Syntactically valid suffix fields/indexes remain unchecked. Optional inputs and declarations with invalid producer/type references can bind locally. Relative input and graph-target paths, nested schema/value resolution, CEL environments, artifact/digest typing, approval transitions, provider support and public/installation admission remain separate. [Evidence and ownership](../research/input-path-validation-evidence-2026-10-30.md).

The omitted-approval predicate pass, `kora.ir.validate_approval_defaults`, is implemented under [issue #526](https://github.com/realkarych/kora/issues/526), with the same structurally validated, unchanged-snapshot precondition. It visits root and every stored body once without expanding calls and examines only human-input nodes with an absent `approve_when`. Only an absent form selects `default_approval_form()`; an authored `{}` remains custom. One validation-local `SchemaTypes` maps effective forms under the existing plain view, resolving only saved custom contracts and exact installed built-in resources. A mapped record must declare `decision`; the existing typed checker then checks `response.decision == 'approve'` with a Boolean result requirement. Explicit predicates remain unchanged and unchecked here.

A known ineligible default yields one fixed `IR011` at the owning `config.approve_when`, with null offset and only the safe node source. `SchemaError` leaves the predicate unresolved without a secondary finding; existing schema passes retain syntax/closure ownership and independent nodes continue. `ApprovalDefaultResult` counts visited nodes, stored definitions, human-input nodes, resolved omitted predicates, unresolved omitted predicates, actual fixed-expression checks and instrumented mapper/checker steps. Resolved predicates include established refusals; resolved plus unresolved equals omitted predicates. `approval_defaults_valid` requires no owned errors and zero unresolved predicates. No result contains a form, type, environment or checked expression.

This is eligibility under section 7.4's mapper, not JSON Schema truth evaluation. Required `decision` schemas mapping to `dyn`, including boolean `false` and mixed-kind enums, follow the existing checker; schema validity and response admission remain separate. The pass inserts no form, predicate or response value and publishes no execution contract. Per-node checking can revisit shared reachable type/bound metadata even though schema mapping is memoized; step counts are not time or heap/RSS guarantees. General CEL environments, explicit predicate typing, binding compilation, saved defaults, effective response typing/projections and approval transactions retain their existing implementing owners. [Evidence and scope](../research/approval-default-eligibility-2026-10-31.md).

The explicit response-context foundation, `kora.ir.validate_approval_response_predicates`, is implemented under [issue #553](https://github.com/realkarych/kora/issues/553) and extended with declared graph parameters under [issue #640](https://github.com/realkarych/kora/issues/640), omitted-parameter interpretation under [issue #644](https://github.com/realkarych/kora/issues/644), and direct-input contexts under [issue #646](https://github.com/realkarych/kora/issues/646), with the same structurally validated, unchanged-snapshot precondition. It visits root and every stored body once and selects only authored human-input `approve_when` values. This is a deliberately partial library pass: section 7.1 still permits `response`, `inputs` and `params`. An identifier or comprehension binder named `inputs` requires the complete node-local input context, including in dead branches. The direct-input scope below leaves unavailable contexts unresolved. A `params` identifier or binder uses the complete authored schema or, only when `graph.params` is omitted, the exact closed-empty schema from the [omission contract](#graph-parameter-schema-omission). A selected field name, quoted string or literal string map key does not require either context. Root nodes and stored bodies use the same effective graph parameter schema. The approved omission rule does not supply unavailable input context. Parameter-only unknown fields, literal keys and shadowing binders reach the existing typed checker; dynamic-key indexing retains its existing rules.

The terminal classification order is syntax, context, schema, typed. Parser failures remain syntax-owned without duplicate `IR011`/`IR027`; `SchemaError` remains schema-unresolved without another `IR022`. Eligible expressions use the existing effective form, one validation-local `SchemaTypes` and `check_typed` with `response`, required effective `params` and `inputs` variables, and the Boolean-result requirement. Response and parameter schemas keep the plain view; direct inputs use their selected view below. `inputs` remains selection-only. Parameter mapping preserves the complete `Variable` and nested `ValueBound`, rather than selecting only a referenced property. Its success or unresolved status is cached once per validation call; exceptions and tracebacks are not cached. Parameter mapping is lazy: response-only predicates do not map parameters or depend on an unresolved parameter schema. The fixed omitted schema is created only for the first required mapping and remains invocation-owned; authored omission and explicit `{}` are preserved in the document and canonical export. Legal recursive closures and pure reference cycles retain the existing mapper behavior, including dynamic types where applicable. Only an absent form selects the published default; authored `{}` stays custom, and the structural top-level object-form contract is unchanged. Explicit predicates need no `decision` property. Existing null, nullable, enum, dynamic-result, reference and single-`allOf` rules remain those of section 7.4. Boolean subschemas and resolved schema targets confer no new Boolean-form admission or schema-truth guarantee.

The internal direct-input producer under #646 resolves only whole-output bindings from ordinary agent, command, human-input and wait-event producers in the current graph/body. Every declared input must resolve before any input schema is mapped. Supported outputs are non-list root types. Exact native roles are agent `session` for AgentSession, a declared agent publication for VcsRef and command `result` for CommandResult, all revision 1. Exact StructuredOutput uses the role rules below. A namespaced custom contract with a null parent and no inline schema is supported for the command file role. A command file producer requires `output_file` and one declared output in addition to `result`. Thus the existing nominal compatibility prerequisite for this scope requires matching producer and consumer type names; the producer supplies the checker schema. Missing or contradictory declarations, unsupported revisions, inherited custom types, lists, authored paths, reserved sources and wrapper/collection outputs leave the expression context-unresolved. Authored inline schemas on VcsRef, CommandResult and AgentSession also remain outside this slice.

Exact AgentSession uses the packaged closed checker schema with its original reference root. After all direct binding prerequisites, the checker uses the shared bounded policy/checker acquisition and verifies exact published bytes before decoding. A missing or inconsistent resource makes this context schema-unresolved. Success and unavailability are cached once per validation call. Ordinary schema references and form/parameter mapping retain the full logical schema. Exact StructuredOutput uses its structured-output view: human response selects the effective form and verifies any simultaneous output schema; agent output uses the accepted four-case selection in section 2.1, including a declaration-only inline schema and equality when both sources are present; wait-event and command outputs use their declared inline schema. Without an applicable inline schema the exact built-in retains its genuine dynamic view. Agent role cardinality and saved ancestry must be known before selecting an output view. No schema combination or current registry lookup supplies a missing view.

The assembled input record is closed, with a field for every declared input and required membership unless `optional` is true. Missing/empty input declarations produce an empty record, so unknown names reach the ordinary checker. Each field preserves its complete mapped type and nested bounds; source schemas retain their original local-reference bases. Source/view choices, producer role scans and successful/failed root-view mappings are invocation-cached without exceptions or tracebacks. Response-only predicates do not resolve or map input contexts. Per-expression checker preparation can still revisit shared metadata; these caches establish no globally linear whole-validator or wall-time guarantee. [Direct-input verification and remaining composition](../research/approval-direct-inputs-2026-11-03.md).

After successful parsing, typed diagnostics, including cost `IR027`, use `from_cel` with the exact owning `config.approve_when` path, code-point offset and only the safe node source. `ApprovalResponsePredicateResult` retains copied bounded diagnostics and scalar node/definition/human-input/predicate/unresolved/step counts. `predicates_checked` counts actual typed-check attempts, including refusals; `predicates_seen` equals that count plus syntax-, context- and schema-unresolved counts. `approval_predicates_complete` requires zero unresolved counts and zero omitted diagnostics; known fully reported type errors may be complete. `approval_predicates_valid` additionally requires no owned errors. Any unresolved category therefore makes both flags false even with empty diagnostics: incomplete checking is not an invalid-DSL or approval decision.

No AST, checked expression, form, type, environment or caller source survives through this result. No binding compilation, saved execution contract, default insertion, response coercion, payload validation or runtime/public admission is added. [Issue #552](https://github.com/realkarych/kora/issues/552) retains full-context production under IR/compiler work item 3 and public integration under work item 11; those obligations and their evidence must be completed before full approval validation or public consumption. This partial delivery does not close that capability. [Response-foundation ownership, checks and limits](../research/approval-response-predicate-evidence-2026-10-31.md) and [declared-parameter integration](../research/approval-parameter-context-2026-11-03.md).

The graph CEL check under [issue #773](https://github.com/realkarych/kora/issues/773) collects genuine successful default or authored approval checks from the root and every stored body, including unused and recursive definitions once without invocation expansion. One caller-supplied declaration allowance counts the root body, stored bodies, all nodes and type contracts before either checker is constructed. Each checker and its invocation-local caches are shared across the admitted pass; direct-input lookup preserves the owning body. The exact `config.approve_when` path anchors an authored condition or its generated default, with that provenance retained for the native pattern phase. Syntax, context and schema incompleteness have that fixed priority across the complete pass and return no partial inventory or diagnostics. Otherwise, established static errors use one shared bounded collector. Only a fully successful pass contributes its sites, and equal renderings at different fields keep their distinct associations. Declaration admission bounds enumeration and site count, not arbitrary schema size or whole-validator work; unchanged-snapshot acquisition and admission remain upstream responsibilities. `tests/ir/test_checked_cel_sites.py` owns the approval inventory evidence. No public budget or approval/execution authority is established.

The wait-expression contextual pass, `kora.ir.validate_wait_expressions`, is implemented under [issue #743](https://github.com/realkarych/kora/issues/743), with the same structurally validated, unchanged-snapshot precondition. It visits every root node and every stored body's nodes once, including unused and recursive definitions without invocation expansion. It checks only authored `config.event.match` and required `config.poll.until`. Omitted event matching stays absent and supplies no synthetic expression. The settled section 7.4 environment supplies exactly `payload: Variable(DYN)` for event matching or `observation: Variable(DYN)` for polling, with existing `DATA_BOUND` and `ResultKind.BOOL`. The other external root is unavailable. A dynamic result remains subject to exact Boolean enforcement by its future runtime owner; no provider or observation schema supplies another type.

The pass reuses the shared demand-driven input and parameter contexts without broadening supported source views. Unsupported input views or unavailable schema mappings remain explicit context/schema incompleteness, and parser failures remain unresolved for the syntax pass. Independent fields continue; established checker failures retain fixed `IR011`/`IR027` findings at the exact field path, original code-point offset, owning node and only its safe source. `WaitExpressionResult` retains copied bounded diagnostics and scalar node, definition, wait, expression, unresolved and step counts. Seen expressions partition into checked attempts and syntax/context/schema unresolved counts. Completeness requires no unresolved category or omitted diagnostics; validity additionally requires no owned error. No document, environment, schema or checked tree survives through this diagnostic-only result.

For the graph CEL check under [issue #773](https://github.com/realkarych/kora/issues/773), the wait inventory first admits root, saved body, type-contract and node declarations with a caller-supplied allowance, before context construction or field enumeration. A structurally valid wait supplies at most one expression, and an omitted event match supplies none. This bounds declaration enumeration and site count, not arbitrary schema/input/name size or whole-validator work; unchanged-snapshot acquisition/admission remains upstream. Only a complete valid typed inventory contributes its event and poll sites. `tests/ir/test_checked_cel_sites.py` owns the wait inventory evidence. No public quota, wait observation/transition or execution authority is established. Those integrations remain pending under [#593](https://github.com/realkarych/kora/issues/593) and [#552](https://github.com/realkarych/kora/issues/552).

The ordinary retry contextual pass, `kora.ir.validate_retry_expressions`, is implemented under [issue #745](https://github.com/realkarych/kora/issues/745). It consumes the same structurally checked, unchanged snapshot and visits every ordinary node and stored-body retry once, including disabled policies and unused or recursive bodies without expanding invocations. Only authored `retry.predicate` fields are checked; omission remains absent. The pass requires the existing Boolean result rule and selection-only `inputs`/`outputs`, using shared demand discovery and actual input/parameter contexts. Existing approval, branch and wait discovery keeps its original searched roots, mapping order and Meter behavior.

Demanded node outputs reuse the supported direct-producer views and invocation-local mappings described above, including original schema reference roots, nested bounds and the published AgentSession view. Every output is a non-required member of a closed record; guards and ordinary field/type restrictions remain mandatory. All source declarations are checked before mapping, including collection exclusion. Missing or unsupported views remain context-unresolved and mapping failures schema-unresolved. Empty timer/branch declarations give a known empty record; their nonempty declarations and wrapper, list, inherited or otherwise unsupported output compositions do not receive fabricated types. Literal-only fields do not map unused input, output or parameter contexts. Node prerequisite resolution preserves inputs-before-parameters, then demanded outputs.

Stored-body predicates containing only literals or graph parameters can be typed. Demanded body inputs or exports remain explicitly context-unresolved until their true view composer is available. Parser failures remain syntax-unresolved for the syntax pass; independent known findings continue through the bounded collector. Established `IR011`/`IR027` findings retain exact node/body `retry.predicate` paths, code-point offsets and only that owner's safe source; body findings have no node ID. `RetryExpressionResult` retains copied diagnostics and scalar node, definition, authored policy, expression, unresolved and step counts. Seen expressions partition into typed-check attempts and the three unresolved categories. Completeness requires no unresolved or omitted diagnostics; validity additionally requires no owned error. These flags cover ordinary node/body predicates only, excluding `runtime.infrastructure_retry` context and complete graph validity.

For the graph CEL check under [issue #773](https://github.com/realkarych/kora/issues/773), the ordinary retry inventory admits the caller's root, type-contract, body and node declaration allowance before constructing contexts or enumerating fields. There is at most one predicate per ordinary node or stored body, and an omitted predicate supplies none. Admission bounds enumeration/site count, not arbitrary schema, input/output or name size, or whole-validator work; snapshot acquisition/admission remains upstream. Only a fully complete valid scoped inventory contributes its node and body sites. `tests/ir/test_checked_cel_sites.py` owns the retry inventory evidence. Body/wrapper/source composition, replacement scheduling and public integration remain under [#593](https://github.com/realkarych/kora/issues/593) and [#552](https://github.com/realkarych/kora/issues/552). No saved disabled policy, public limit or execution authority changes.

The root sensor-trigger contextual pass, `kora.ir.validate_sensor_trigger_expressions`, is implemented under [issue #537](https://github.com/realkarych/kora/issues/537), with the same structurally validated, unchanged-snapshot precondition. It visits every root graph trigger once and checks only sensor `poll.until`, preserving the original trigger index. The fixed environment is exactly `observation: Variable(DYN)` under section 7.4 with existing `DATA_BOUND` and `ResultKind.BOOL`. No observation schema, provider inventory or sensor `config_schema` supplies another type. Known wrong results fail; a dynamic result passes statically and still requires exact Boolean enforcement by its future runtime owner.

An existing-parser prerequisite leaves syntax/static-parser failures unresolved without duplicating the syntax pass's findings. Independent sensor predicates continue. Successful prerequisites invoke existing `check_typed`, including canonical rendering round-trip and logical cost admission. Established checker failures retain fixed `IR011`/`IR027` messages at `graph.triggers.<index>.poll.until`, graph-only safe source, null node and the original code-point offset. `SensorTriggerExpressionResult` retains only shared diagnostics and counts for all triggers, selected sensor triggers, typed invocations, unresolved prerequisites and existing parser/checker steps. Typed plus unresolved equals selected sensor triggers; `expressions_valid` requires no owned errors and zero unresolved predicates.

The pass preserves authored data and releases its call-local environment, parse/check/bound/projection state before the result escapes. Parser and checker work repeats per expression; step counts establish no wall-time, heap/RSS or general validator-scale guarantee. Wait-event/node contexts, inputs/params typing, provider/config/minimum-interval admission, native binding compilation, saved contracts/projections, public validation and runtime start/apply remain separate owners. This result grants no sensor/start authority. [Scoped evidence and limits](../research/sensor-trigger-expressions-2026-10-31.md).

The graph CEL check under [issue #773](https://github.com/realkarych/kora/issues/773) reuses this pass on a structurally validated, unchanged snapshot. It admits the entire root trigger list against a caller-supplied count before visiting any trigger, including non-sensors in the count. Only a complete typed pass contributes the exact authored `poll.until` fields. Static refusal returns the existing diagnostic and incomplete counts before native admission. The diagnostic-only entry retains no checked inventory. `tests/ir/test_checked_cel_sites.py` owns the sensor inventory evidence. Public validation and execution admission are owned separately.

The internal server check under [issue #773](https://github.com/realkarych/kora/issues/773) combines branch, approval, ordinary retry, wait and root sensor-trigger sites from one structurally validated, unchanged snapshot. It uses the existing branch declaration count and full root trigger count, then requires each family's successful typed inventory. The caller supplies all four allowances. Snapshot admission and bounds on arbitrary schema work belong to the caller.

The check admits aggregate pattern work and canonical rendering characters before native submission. It compiles each field once in the existing serial queue, including equal renderings at distinct fields, and probes each unique decoded pattern once across the inventory. Authored pattern refusals retain exact field paths and offsets. A complete result contains a compiled field count and bounded pattern diagnostics, which can contain errors. Static or native incompleteness returns the original scoped refusal without a successful prefix; a native rendering refusal carries the exact original field path, including integer segments, and the closed fault, without native text. The check releases the document before native delivery and drops checked trees before probing. Native refusal and cancellation release invocation-owned state while the caller owns evaluator closure. `tests/api/test_graph_cel.py` owns native compilation, admission before checker construction, failure association, cancellation and release evidence. Broader context derivation, saved CEL facts and public validation remain under #552 and #593.

The authored infrastructure predicate joins this combined check under [issue #779](https://github.com/realkarych/kora/issues/779). Its producer applies the [parameter-only context](#71-expression-kinds-and-environments) and resolves the effective parameter type on demand. Omission contributes no site. Syntax and schema prerequisites return explicit incomplete results. Known typing and pattern diagnostics identify the exact runtime field. Successful typing contributes one authored site to both aggregate allowances, rendering compilation and cross-field pattern deduplication. Trusted configured predicate acquisition and diagnostics belong to the complete validation owner.

Under [issue #789](https://github.com/realkarych/kora/issues/789), a caller may supply paired positive allowances for aggregate metered typed work and retained checked state. Existing parser, checker and schema meters and precharged type catalogue enumeration share one work allowance, while component step counts retain their own meanings. Before retaining each successful site, the verifier charges the existing logical rendering, bound and projection accounting plus the exact segmented path. Exhaustion returns capacity before preparing detached facts or submitting native work. Raw snapshot bytes, the temporary single check and source-view work outside these meters retain upstream admission responsibilities.

Validation emits [shared diagnostics](diagnostics.md) and collects every independent fault within accepted request bounds. Invalid prerequisites prevent only their dependent semantic checks. Collection bounds never turn omitted errors into success, and a provider diagnostic-limit failure is incomplete validation, never a successful or warning-only result. The shared contract owns safe metadata, source coordinates, code-point offsets, deterministic ordering and separate omitted counts; the HTTP and incomplete-validation mappings remain with [API conventions](api-and-agent-surface.md#validation-envelopes). Accepted IR018/IR022 and installation-feature refusals remain document diagnostics, not dependency outages. Provider `diagnostics_limit` and aggregate count overflow prevent a normal collection or deployment.

The authored first-slice configuration pass, `kora.ir.validate_first_slice_configuration`, is implemented under [issue #524](https://github.com/realkarych/kora/issues/524), with the same structurally validated, unchanged-snapshot precondition. It checks the root's one-agent → one-command → one-human-input topology, declared input/control ordering, nonmanual trigger kinds, `graph.target`, nonempty `graph.subgraphs` and the matrix's explicitly unsupported authored skills, subagents, hooks, MCP servers, repository trust, USD budgets/headroom and retry/provider-wait settings. Every stored definition and node is inspected once without invocation expansion, including unused or recursive definitions. Exact no-ops remain unchanged; provider zero durations are classified without integer conversion. Metadata, schemas and payload words are not configuration authority.

Fixed `IR028` findings identify exact unsupported fields; cardinality/kind topology uses `graph.nodes`, and aggregate ordering uses the owning node path. Node/body/graph source metadata belongs only to that owner and is revalidated; runtime findings have no source. Only a root with exactly one of each supported kind receives ordering comparisons. Missing links, missing producer outputs, non-parameter reserved inputs and cyclic endpoints leave dependent comparisons unresolved rather than duplicating `IR001`/`IR002` or retry-legality findings. Independent configuration findings continue through the existing diagnostic count/byte bounds and omissions.

The agent-secret rule `IR015` is implemented under [issue #1462](https://github.com/realkarych/kora/issues/1462) as `kora.ir.agent_secrets.agent_secret_findings`, which deploy runs with the other passes. Its trusted inputs are each installed runtime's declared `enforces_network_policy` and `isolates_tool_secrets` and the namespace's `allow_unenforced_agent_secrets` and `require_network_enforcement`. A node runs on its `config.runtime`, else on the document's `runtime.runtime`; a runtime the installation does not have declares neither capability. A `secret_env` counts only when it names at least one variable. A `network_policy` is non-empty when it declares `egress`, including an empty list that denies all egress; an absent policy, `{}` or one with only extension fields is empty. Each finding carries the path of the offending `secret_env`, with the shared placeholder in place of an MCP server name. The namespace permission lifts the agent and MCP-server cases only, never the command-node case.

The refusal of a network policy the runtime cannot enforce (first-slice refusal rule 2) is implemented under [issue #1496](https://github.com/realkarych/kora/issues/1496) as `kora.ir.network_policies.network_policy_findings`, with the same trusted inputs. An explicit `network_policy` on an agent or command node, including `{}` and `egress: []`, is `IR028` at that path when the node's runtime does not declare `enforces_network_policy`; only omission is a no-op. An MCP server's own `network_policy` is `IR028` at `config.mcp_servers.<placeholder>.network_policy` unless the server declares a non-empty `secret_env` and the runtime declares both `enforces_network_policy` and `isolates_tool_secrets`, because only such a server is isolated and runs under its own policy ([protocols.md section 2](protocols.md#2-runtime-fr-81)); a server without secrets runs in the agent's domain under the agent's policy. When the namespace allows unenforced agent secrets, the ADR-0009 exception keeps an agent's policy deployable, honestly unenforced, only when that agent declares a non-empty `secret_env`, and an MCP server's policy only when that server does. A policy on a node or server without secrets and every command's policy stay refused.

`FirstSliceConfigurationResult` exposes node, definition, trigger and checked/unresolved ordering counts. `authored_configuration_supported` requires no owned diagnostic and no unresolved ordering comparison; it is false for an unresolved chain even when this pass emits no error. It establishes only the authored configuration checks, without full graph validity, installed capability resolution, namespace security, defaults normalization, saved retry policies or installation readiness. No provider/secret resolution, mutation, public validate/deploy wiring, persistence or runtime behavior is activated. Those work-item-3 and consumer obligations remain pending.

Schema validation first, then semantic checks, each with a stable code and a safe segmented document path when the field is known (FR-0.4): `IR001` acyclic graph (including `depends_on`), `IR002` every `from` resolves, `IR003` output/input type assignable, fixed implicit output roles (section 3.1), and an explicit response output schema structurally equal to the effective form (section 4.2), `IR004` fan-out `over` names a list-typed input and every `collect` names a body output, `IR005` `binds` paths resolve to a `VcsRef` and digest-bearing artifacts, `IR006` `$previous_attempt` only inside a retried subgraph, `IR007` secret aliases resolve, fall inside the namespace's allowed reference prefixes and no value-like strings appear in `env`, `IR008` harness cannot honour a non-security field (warning; unsupported security controls are errors), `IR009` runtime lacks a requested capability such as `enforces_network_policy` or `supports_resume`, or the node's runtime and VCS declare no common plan executor ([protocols.md](protocols.md) section 4) (warning only for optional non-security capabilities; unsupported security controls and no common plan executor are errors), `IR010` branch targets exist in their graph/body scope, each has the owning branch directly in `depends_on`, no target has multiple distinct branch owners, neither branches nor ordinary direct targets set `attempt_invariant: true`, and `$fail_attempt` appears only inside a subgraph, `IR011` a human-input predicate is explicit or has a valid default (section 4.2), CEL expressions parse and type-check against the declared schemas, and paths parse: unsupported syntax or functions, unknown identifiers and fields, shadowing comprehension variables, enum literals outside the allowed values, operand overloads, unguarded optional reads, `size()` on strings or `dyn` values, non-literal or uncompilable `matches()` patterns, wrong result types, a canonical rendering that does not parse back to the same tree and expressions the binding cannot compile at server-side validation (section 7); path resolution keeps its own codes, `IR012` Jinja2 templates parse in the sandbox, `IR013` `tool_policy` patterns are valid under the grammar and bounds of [protocols.md](protocols.md) section 12, in the node's and in the namespace's policy, and none has the exact tool name `StructuredOutput`, the harness output channel (a wildcard pattern stays valid and never applies to it), `IR014` sensor `config` fails the sensor's `config_schema`, `IR015` an agent node with `secret_env` lacks a non-empty `network_policy` or runs on a runtime declaring `enforces_network_policy: false`, an MCP server with `secret_env` lacks a non-empty own `network_policy` or runs on a runtime declaring `isolates_tool_secrets: false` or `enforces_network_policy: false`, unless the namespace sets `allow_unenforced_agent_secrets`; a command node with secrets on a non-enforcing runtime while the namespace sets `require_network_enforcement` (ADR-0009 as amended 2026-10-18), `IR016` run parameters appear in a `cmd` element or `workspace.rev` of a graph with a webhook trigger, `IR017` `trust_repo_config: true` on a node whose namespace policy does not allow it (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)), `IR018` an agent node's `harness` names no harness adapter registered with the installation, which in MVP includes every reserved `acp:<agent>` name (an error, not a warning: unlike `IR008` and `IR009`, the node could not run at all; ADR-0004), `IR019` a namespace tool-policy layer has an uncovered requirement under the [complete policy-coverage rules](validation-facts.md#9-complete-policy-coverage-requirements), because the platform team's rule would otherwise run unenforced (an uncovered node-only requirement uses `IR028`, ADR-0009), `IR020` `graph.target.key` names a run parameter that is not a string in `graph.params`, or a node, an output or a path that does not resolve to a string, checked against the output's declared schema like `IR011` (FR-6.11, ADR-0014), `IR021` the selected runtime lacks `protected_execution` while namespace policy requires it (ADR-0016); local remains valid for trusted development/tests without that requirement. `IR022` rejects missing/inconsistent type closure, unsupported schema dialect or behavior, invalid schema references and incompatible author-side contract declarations (section 2.1). `IR023` rejects an `independent_of` entry that is not a declared input name of that node. `IR024` rejects a command node with `resources` on a runtime that does not declare `protected_execution` when the namespace sets `require_protected_resources` (issue #45). `IR026` rejects an agent node whose `env` or `secret_env` sets a name or prefix in its harness adapter's `reserved_env` ([protocols.md](protocols.md) section 3), such as the `claude` adapter's `HOME`, `ANTHROPIC_*`, `NODE_*` or `HTTPS_PROXY`, or a variable the runtime assigns to the candidate domain, because the adapter or the runtime owns those variables and they change the harness's destination, its TLS trust or what it and its shell load at start-up (issue #125). `IR027` rejects a CEL expression or path over a static limit (4,096 code points, depth 32, four comprehension macros nested at most two deep), a `matches()` pattern over its size-estimate cap or its group-nesting limit, an expression over the cost budget, or static trigger `params` over the CEL data bounds, including a `maxLength` violation found by schema validation (section 7). `IR029` rejects a command node that declares more than 64 inputs (issue #106).

`IR028` rejects a well-formed capability/configuration the installation does not implement, including an unenforceable node-only security control. Its recovery hint is to remove the unsupported configuration or use an installation that implements it; it never suggests disabling a required protection. [First-slice.md](first-slice.md#refusal-contract) defines the refused kinds/fields, exact empty/absent values, topology, resolved-default checks and narrow ADR-0009 exception. The shared validator uses the trusted installation feature inventory for both validate and deploy, reports the unavailable capability at its safe segmented document path under [the shared diagnostic contract](diagnostics.md) and retains more specific syntax/type/security codes. It traverses operational typed fields, not arbitrary metadata or payload strings. Diagnostics preserve the shared safe-location fallback, source metadata, collection bounds and omitted counts; a deploy refusal is `API003` with `details.errors[]`. Full-language schema/CEL checking remains independent of first-slice executability, including dormant predicates and every worked example.

`test_slice_one_rejects_unsupported_topology` exercises wider/parallel graphs and confirms identical `IR028` locations through validate/deploy. Configuration/default tests cover budgets (including zero), retries, node resources, repository trust and unsupported providers/security settings before secret resolution or other side effects. These installation-feature validator tests remain pending work item 3; the scoped graph-link pass does not establish installation admission.

### 10.1 Approval and implicit-output contracts

These contracts are pending implementation. The [implementation plan](../../docs/implementation-plan.md#7-named-contracts-by-work-item) assigns their model, validator, CEL, compiler and runtime legs. Structural example checks establish neither semantic diagnostic codes nor approval behavior.

| Test | Required result |
|---|---|
| `test_default_approval_form_and_expression` | Omitted form and predicate resolve to the built-in form and checked expression; decision is required, comments optional, and missing, null, wrong-type or unknown decisions cannot approve. No schema default fills a response. Explicit predicates are preserved, including a predicate that approves `decision=reject`. Valid default approve/reject responses follow the existing authority, binding and gate rules; the rejection gives `approval_rejected`. |
| `test_custom_form_without_decision_requires_approve_when` | A supplied empty schema or a custom form without decision and without an explicit predicate fails with `IR011`; an explicit valid predicate receives the custom payload unchanged. Optional, incompatible and enum-excluded decision fields fail ordinary checking, including through `$ref` and single-schema `allOf`. False rejects; evaluation error retains the response and fails with `config_error`. Conflicting explicit response schema fails with `IR003`. |
| `test_implicit_output_names_are_fixed` | Direct IR rejects missing, renamed, retyped or aliased roles, including nested bodies, with `IR003` and the owning role path. Human-input and wait-event nodes have only their one role output; an agent's ordinary `result` remains valid. Compiler insertion preserves conflicts, adds the required type closure, emits deterministically and agrees with shared validation. Effective response and event schemas reach downstream typing and projections. |
| `test_reject_reports_authoritative_status` | A real CLI against the real API displays the actual request status in human output and preserves the API body exactly under `--json`. The default predicate rejects; explicit `response.decision == 'reject'` can approve the same payload. Neither implies completion of a VCS-bound gate. Missing required built-in or custom fields fail noninteractively without synthesizing values. |

Retain `test_cel_approve_when_not_evaluated_without_authority`, `test_cel_approve_when_error_marks_request_failed` and `test_push_cannot_rebind_old_evidence` for the effective default as their components activate. Approval integration cases run on PostgreSQL and SQLite; code-host and compiler/scenario legs activate with those components. Diagnostics remain content-free and never carry forms, comments or response payloads.

## 11. Worked examples

- [examples/sql-optimization.ir.yaml](examples/sql-optimization.ir.yaml): the canonical graph from `positioning.md`, as written in the DSL example of [dsl.md](dsl.md) section 1: target → baseline → hypotheses → fan-out of a retried subgraph (implement → dev run → equivalence → review → branch → pack candidate by a command node), the fan-out node collecting the candidates → select best (a command node: gain over the baseline, the 15 % threshold, ties broken by the lowest commit) → review bundle narrative (cheap agent) → VCS approval with kora-attached cost → wait for deploy → timer → metrics check. The two scripts, `pack_candidate.py` and `select_best.py`, are command resources of the graph version, delivered and verified by digest (section 4), and belong to the end-to-end fixture of the canonical graph ([protocols.md](protocols.md) section 13); `test_candidate_selection_exact_and_reproducible` covers threshold edge values, ties with that tie-break, the empty set, an invalid baseline, a missing metric, that no reference outside the input set can appear in `best`, and that identical inputs give an identical output digest.
- [examples/dependency-upgrade.ir.yaml](examples/dependency-upgrade.ir.yaml): a small cron graph: Codex upgrades one dependency, a command node runs the tests, an approval on the CLI channel, then a sensor waits for the merge.

Both validate against the schema.

Publication-producing nodes follow [ADR-0016](../../docs/decisions/0016-trusted-execution-and-publication.md): a `VcsRef` is derived from trusted prepared/publication results, not an agent-reported branch. Existing candidate commits are preserved; uncommitted changes become one additional prepared commit. Publication credentials are acquired per purpose by trusted code ([ADR-0023](../../docs/decisions/0023-code-host-identity-and-purpose-scoped-credentials.md)), not a required node `secret_env`. Every published `VcsRef` under [revision 1](artifacts/README.md) requires `pushed_by` as the complete scoped `PrincipalRef` of the actual publisher, including `kind: local_service` for credential-free local publication. `opened_by` and the change-request `url` are present only for an actually opened change request; do not infer an opener from the pusher or commit author. A local bare-Git branch-only reference omits `opened_by`, `url`, `commit_url` and `compare_url`, rather than filling them with nulls or fabricated identities/links; its ordinary required repository/branch/commit/base/diff fields remain unchanged. The built-in artifact contract must accept this exact branch-only shape and preserve current-head approval and immutable digest behavior. Explicitly granting candidate code a code-host credential grants it independent external side effects.
