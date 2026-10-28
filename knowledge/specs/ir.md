# kora MVP design — the YAML intermediate representation (IR)

Status: design draft for owner review (2026-10-15, revised after the design review, see [review-2026-10-15.md](../reviews/review-2026-10-15.md)). Scope: the canonical graph representation that `kora compile` emits, `kora validate` checks and the scheduler consumes (FR-1.2–1.7, FR-2, FR-3.2, FR-6.3–6.6, NFR-6). The machine-readable contract is [ir.schema.json](ir.schema.json), published at a stable `$schema` URL and registered in SchemaStore so editors validate `*.ir.yaml` files; worked examples are in [examples/](examples/). Design decisions taken by the owner on the choices the requirements left open are recorded in [design-decisions.md](../reviews/design-decisions-2026-10-15.md) and referenced here as **DD-n** ("design decision n").

CEL cost-regression amendment dated 2026-10-23: the project owner approved lightweight validation and a 100 ms target under [issue #167](https://github.com/realkarych/kora/issues/167#issuecomment-6351849696); the corresponding human-facing documentation wording was reviewed line by line and approved by the project owner on 2026-10-23.

The committed schema is generated from the real `GraphDocument` model by `kora.ir.schema.generate_ir_schema` and `render_ir_schema`. The dialect and public `$id` are unchanged. Local `$defs` and references use the real Pydantic model/type names; the unreleased IR 1.0 does not preserve the former handwritten fragment names as compatibility definitions. Scalar and literal constraints may be inlined in model properties. Every generated reference resolves inside the schema; generation reads no committed schema and performs no network or provider lookup. The complete byte-drift test preserves source-owned annotations as well as constraints.

## 1. Design rules

1. **The IR is data, not code.** The scheduler never imports user Python; everything it needs is materialised in the file, including resolved per-node configuration (FR-3.2) and expressions for branches and predicates (section 7).
2. **Stable string ids and named references**, never positional edges (FR-1.5). Node ids are unique within a graph document and match `^[a-z][a-z0-9_]{0,63}(?![\s\S])`.
3. **Three concerns in three sections** (FR-1.3): `graph` (topology and node definitions), `runtime` (graph-level runtime and policy configuration), `secrets` (references only, never values). Compiler provenance (preset and defaults snapshots) lives under `meta`, outside all three.
4. **Lenient at the root, strict inside typed objects** (FR-1.5): unknown root keys are ignored with a warning; unknown keys inside node, retry, limits and config objects are errors. `x-` prefixed keys are allowed on IR record objects, including nested records, for tool metadata. Named maps (such as nodes, type contracts and secret aliases) still validate their entry names and values; embedded JSON Schemas and user payloads retain their own rules.
5. **Deterministic output** (NFR-6.2): keys in schema order, lists in source order, no timestamps.
6. **Two versions, kept apart.** `ir_version` at the root is the format version under semver (NFR-6.3: minor bumps add optional fields, major bumps may remove or re-type fields); `graph.version` is the author's own version string for the graph, informational, distinct from the control plane's deployed version numbers.

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

`meta.presets` and `meta.defaults` are **informational snapshots**: the scheduler reads only each node's resolved `config` (FR-3.2); they let a reviewer diff a preset change and a UI show provenance.

`GraphDocument` is the structural root, with eight kind-specific envelopes behind the discriminated `Node` union. `GraphBody` and `Subgraph` carry stored nodes; `Graph` owns the shared body registry. These records preserve authored omissions and never insert role outputs, predicates, overlap defaults or scheduling settings. Timer and branch output declarations remain structurally optional and may be authored; semantic validation must refuse nonempty declarations before execution.

Unknown root keys are discarded without traversing their values. `inspect_ir_root` counts them before nested validation, and a successful parse retains only `unknown_root_key_count` as non-wire metadata, excluded from schema and canonical export. Root `x-` metadata follows the existing bounded extension contract and is not counted. The Python spelling `schema_` is an ignored root key; only wire `$schema` binds that field. `$schema` retains its raw strict string with `format: uri` as an annotation, without normalization, resolver authority or a claim of URI admission. Public warnings, diagnostic codes and bounded collection remain with the future shared validator.

`Provenance` retains typed compiler, source digest and raw partial preset/default snapshots plus arbitrary detached JSON members. It is informational data, including `x-` members, and supplies no execution authority. `export_ir` and `render_ir_json` accept validated IR records and the explicit standalone `Provenance` root. They exclude private parse metadata and preserve wire aliases, authored field sets, map order and list order through iterative canonical export. Arbitrary Pydantic models and sequence roots are not supported.

### 2.1. Authoritative type contracts (ADR-0018)

The unreleased IR 1.0 uses `kora.execution.v1` interpretation. `type_contracts` is required in this format and contains every used non-list type and its parent/reference closure. A graph that uses no artifact types, such as a timer-only graph, has an empty `type_contracts: {}` closure. `List[T]` derives its contract from the fixed list semantics and `T`; it has no separate registry alias. The compiler emits definitions from author-side models, including an explicit `$schema`, and installed immutable built-in revisions. It performs no registration/network write during compilation.

The [built-in revision catalogue](artifacts/README.md) publishes authoritative VcsRef and [StructuredOutput revision-1](artifacts/StructuredOutput.md) schemas and validation, digest-selection and summary semantics. Other built-in definitions remain owned by #138. A built-in entry is `{builtin_revision: "1"}`. The installation must resolve that revision to its immutable schema and behavior descriptors; a revision cannot be rebound to new bytes. A custom entry contains `schema`, `parent` (a type name or null) and `behavior` with exact `validation`, `digest_fields` and `summary` implementation identities. These identities identify provider-declared immutable semantics, not executable source or an instruction to download/import author code. Registered provider metadata supplies the installed binding and verifies its implementation identity; an unavailable or mismatched binding is rejected, not replaced by the current implementation.

The generic behavior identities are `kora.json-schema-2020-12.v1` for schema validation and `kora.json-payload.v1` for whole-payload digest selection and the identity summary. Custom summary bindings use their provider's immutable identity and the trusted installation's configured entry point. Identity summaries are content-bearing payloads, not permanent small metadata. Actual selected fields/projections and summaries are stored at production time ([protocols.md](protocols.md) section 8).

Schemas use JSON Schema draft 2020-12. Local fragment references resolve inside their document; cross-type references use `urn:kora:artifact:<name>` with a namespace separator encoded as `:`, for example `urn:kora:artifact:dwh_core:Candidate`. Built-in schemas have the same canonical `$id` form. Deployment builds an in-memory resolver only from the saved closure. Other external references, unresolved parents/references, parent cycles, unsupported dialects and unavailable behavior identities are `IR022` at the affected path. No runtime network resolver or mutable registry lookup is allowed. Schema syntax/metaschema validity and closure/assignability are semantic checks beyond the outer JSON Schema.

Custom definitions in the document are authoritative, even if the namespace's current alias has changed since compilation. Deploy checks namespace ownership and the whole closure, expands built-in revisions and includes inline node output schemas in `graph_version.execution_contract`. It does not replace an embedded definition with today's registry value. Registration changes a namespace alias for future authoring only. Deployment identity is the digest of canonical `{ir, execution_contract}`; `ir_digest` remains the digest of the compiled document. The same complete deployment is idempotent; different resolved resource/type behavior cannot reuse the old identity. `meta` remains informational.

Every consumer uses this closure: schema-driven CEL checking, edge assignability, worker output schema, API upload validation, branch/collection evaluation and content-bound approval digests. Scheduler-only nodes acquire no physical attempt row. A supported execution-semantics version must implement both saved built-in revisions and declared custom behavior; unsupported execution fails explicitly before the affected operation, while historical reads use saved results. Referenced skill/MCP/resource identities are carried when declared or observed, with delivery still owned by issues #45 and #48.

Artifact type names are a built-in or namespaced type, recursively wrapped in `List[T]` where needed. The JSON Schema checks the structural spelling; semantic type validation (`IR003`) parses the complete type and checks balanced brackets at every nesting level. The schema alone cannot establish arbitrary bracket balance and imposes no list-depth limit.

#### StructuredOutput effective contracts

`kora.execution.v1` supports the previously unbound StructuredOutput revision 1 through the immutable compatibility binding `kora.structured-output-view.v1`. Its scope is that exact revision, custom types whose saved ancestry reaches it, and corresponding derived lists. Save the binding, resolved parent/reference closure, every applicable validation schema and its source, selected checker schema, behavior identities and all parameters in the effective output contract. Installations must support that exact binding. A historical record lacking it cannot infer support from current defaults: refuse new effects before allocation/execution while retaining saved historical results. Unrelated supported interpretations remain unchanged.

For corresponding derived lists, structural validity and individually admitted items do not activate execution. The [StructuredOutput aggregate prerequisite](artifacts/StructuredOutput.md#1-payload-limits-and-immutable-identity) requires owner-approved finite, immutable list-wide canonical-byte, depth and item-count semantics saved in the effective contract, plus the [named conformance/resource gate](artifacts/StructuredOutput.md#5-verification-and-activation), before activation. It covers recursively nested lists and scheduler collections/exports that bypass upload; existing CEL projection and upload bounds are insufficient. No numerical aggregate default or whole-list 1 MiB policy is inferred.

Every applicable schema validates the same direct value without coercion, normalization or inserted defaults. For an exact built-in StructuredOutput output, an applicable inline schema supplies its checker view; without one the view is `dyn`. A custom descendant or derived list retains its saved declared/derived checker schema: inline constraints cannot introduce dot-access fields, change their types or make them required. A different static view needs a named type. Do not send a manufactured multi-schema `allOf` to the checker; that would erase declared fields to `dyn`. The effective contract digest binds both validation constraints and checker view ([section 7.4](#74-schema-driven-checker)).

For the one agent-produced structured output, `config.output_schema` is its inline schema. A simultaneous `outputs.<name>.schema` must be structurally equal as JSON, ignoring object order, or fail with `IR003`. With no declared structured output, existing harness-only `output_schema` and ReviewBundle narrative handling remain unchanged and grant no artifact authority. Human-input responses retain section 4.2's exact effective-form equality rule; wait-event `outputs.event.schema` supplies its inline schema; command `output_file` validates the declared effective contract. `List[T]` retains its distinct derived semantics and saved item contracts. Descendants inherit StructuredOutput admission and canonical rules; list items inherit those rules relative to each decoded item without inventing independent raw bytes or a new whole-list cap.

These are published contracts, not implemented compiler/checker/provider support. The [activation table](artifacts/StructuredOutput.md#5-verification-and-activation) assigns their executable gates and resource bounds.

### Structural declarations

The declaration records preserve authored fields without resolving installed resources or executing schemas. `InputRef` requires only `type`; its producer, output, path and optional flag remain absent when unauthored. `OutputDecl.schema` and custom contract schemas are open JSON objects, including nested boolean schemas. A custom contract requires the literal draft 2020-12 `$schema`, a required nullable `parent`, and nonempty behavior identity strings. Built-in revision and behavior availability, schema validity, recursive type balance and assignability remain semantic checks.

`IrSourceLocation` preserves the wire `file` string, positive JSON integer `line` and `x-` metadata. It is distinct from the existing safe diagnostic `SourceLocation`; parsing opens no path and grants no diagnostic authority. Trusted public conversion remains with the shared validator.

The version, type-contract name, retention period, secret reference and artifact-type patterns match the whole authored string, including refusal of trailing newlines. Named maps apply their entry contracts to every key: secret aliases use environment-name keys, approval-channel names are unrestricted strings, and `x-` names in these maps are ordinary entries. Runtime records preserve missing concurrency, retention and retry settings. `provider_retry` accepts ordinary record extensions and does not insert `max_wait`; first-slice disabled-policy normalization and provider support remain consumer responsibilities.

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

**Edges** are the union of `inputs[*].from` and `depends_on`. An input names the producer node and one of its declared outputs; the validator checks that the producer's output type is assignable to the input type (the same saved type contract, or a subtype through the graph version's saved parent closure, FR-4.3 and ADR-0018); a subtype keeps the CEL type of every parent property, so it cannot narrow `number` to `integer` (section 7.4). Reserved sources: `$params` (run parameters), `$previous_attempt` (section 5), and `$inputs` inside a subgraph body (the subgraph's declared inputs, by `path`; in a fan-out body the mapped input resolves to the current list element). There is exactly one way to feed a body: the `inputs` of the `subgraph` or `fan_out` node. Inputs declared with `optional: true` may be unresolved when the producer was skipped; others make the node `upstream_failed`/`skipped` according to the trigger rule.

**Input independence** (FR-6.8a, ADR-0007): `independent_of` contains names from this node's `inputs`, not producer IDs. Unknown names fail semantic validation with `IR023`. It preserves only an already successful outcome on the affected input edge; any other affected input or control edge still invalidates the node. Consumers without a successful result are reevaluated. Runtime invalidation resolves declared edges even before input binding, through body input forwarding and exports/collection, together with current invariant-reuse bindings ([data-model section 6](data-model.md#6-clear-downstream-fr-68a)). It never waives a branch/control gate.

**Outputs** declare names resolved exclusively through the compiled `type_contracts` and the deployed closure, not current registry aliases (ADR-0018). Built-in names: `StructuredOutput`, `VcsRef`, `AgentSession`, `CommandResult`, `ReviewBundle`, plus `List[<type>]` for fan-out results and user types registered under `namespace/Name`. The `VcsRef` payload follows [revision 1](artifacts/README.md#2-vcsref-publication-facts): immutable publication facts, required pushing principal and optional code-host facts; current status/head observations remain separate. A [StructuredOutput](artifacts/StructuredOutput.md) payload is the direct JSON value, including a scalar or produced null; its schema belongs to the saved effective contract (section 2.1), without a `{schema, data}` wrapper. **Agent output contract** (DD-36): an agent node declares at most one `StructuredOutput`-derived output and at most one `VcsRef` output besides the implicit `session`; `config.output_schema` supplies the structured output's inline contract schema under section 2.1's equality rule; the worker publishes the workspace only when a `VcsRef` output is declared, and a declared `VcsRef` with no diff fails with `artifact_invalid`. The `AgentSession` payload carries harness name and installed version, session id, transcript `storage_ref`, usage per model with completeness, cost estimate with basis and billing class, turn count, duration, `permission_denied` events with whether the harness-side ones are complete, stop reason, and provenance (worker id, image digest). A `CommandResult` payload carries the exit code, the duration, stdout and stderr references with truncated tails, and the shielding of the measurement (DD-34; [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)): `protected_paths`, the resolved list in force for the node, `base_commit`, the commit the digests were compared against, and `protection`, `verified` when every listed path was unchanged against `base_commit` before the command ran and `unprotected` when the node declares no protected paths; a changed protected path fails the attempt with `protected_path_modified` before any `CommandResult` exists. All of them enter the `ReviewBundle` facts.

A **`ReviewBundle`** (DD-33) has two parts: `facts`, written by kora from its own records and read-only to agents (every input artifact of the bundle node with type, digest, producing attempt, worker and image; gate results from `CommandResult` inputs with exit codes, protected paths, base commit and verification result, so that each gate is labelled as verified against its protected paths or unprotected; metrics references; the `VcsRef` with commit; the run's attempt cost), and `narrative`, the only part the agent node writes (`summary`, `rationale`). Approvals bind to the digests of `facts`; `kora web` and the change-request comment render the two parts visibly apart, each gate with its label. There is no node kind for building artifacts (DD-8, ADR-0012): packing a `Candidate` or selecting the best candidate by a fixed formula is a command node over its materialised inputs (section 4), because a copy and a formula need no model and must be reproducible; writing the `ReviewBundle` narrative, or a selection that needs judgement, is an ordinary agent node on a cheap model from its typed inputs. kora itself attaches the aggregated cost of all attempts to the approval request (section 4.2); the agent never computes cost.

**Trigger rules** (FR-1.6 plus DD-1): `all_success` (default), `all_done`, `one_success`, `one_failed`, `none_failed`, `all_skipped`, and `none_failed_min_one_success` (borrowed from Archon: no upstream failed and at least one succeeded, so a node after an all-skipped branch stays skipped). The enum is closed in IR v1.

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
| `command` | deterministic gate | `cmd` (argv, no shell), optional `output_file`, `env`, `secret_env`, `network_policy` (enforced by the runtime like an agent node's, ADR-0009), `workspace`, `success_exit_codes` (default `[0]`) | `result: CommandResult` (exit code, duration, stdout and stderr references, truncated tails, `protected_paths`, `base_commit` and `protection`, section 3) and, with `output_file`, one declared output of type `StructuredOutput` or any custom artifact type whose schema the file must satisfy; stdout and stderr lines stream as session events (DD-36) |
| `human_input` | approval gate | `channel`, `timeout`, `on_expiry`; optional `form`, `approve_when` and `binds` (section 4.2) | `response: StructuredOutput` (effective form response) |
| `wait_event` | sensor | `event` or `poll`, `timeout`, `on_expiry`; optional `interval` | `event: StructuredOutput` (event payload) |
| `timer` | durable delay | `duration` or `at` (absolute time) | none |
| `branch` | deterministic routing | `cases: [{when, to}]` | none; durable selection gates its direct targets (section 4.5) |
| `fan_out` | map a list over a subgraph | `body` (subgraph id), `over` (name of the list-typed input) | per-item instances; the node's own outputs are `List[<type>]` collected from the body's outputs (`collect`) |
| `subgraph` | graph as a node | `body` (subgraph id); the node's `inputs` feed the body | the body's declared outputs |

**Command inputs.** Before a command starts, the worker writes the payload of each of the node's inputs to `.kora/inputs/<input name>.json` in the workspace, a `List[...]` input as a JSON array of element payloads, so a script reads lists and nested artifacts from files; `cmd` elements may still carry scalar fields through templates (`{{ inputs.target.task_id }}`). `.kora/` is kora's own directory in every workspace, excluded from publication ([protocols.md](protocols.md) section 4).

**Command resources** (issue #45). A command node may declare `resources`: a list of `{name, sha256, size, executable}` and a `resource_set_digest`. `name` is a relative path in Unicode NFC with `/` separators, no empty, `.`, `..` or absolute segments, no control characters (U+0000 to U+001F, U+007F) and no backslash, unique within the node. `sha256` is `sha256:` followed by the lowercase hex SHA-256 of the file's bytes. `resource_set_digest` is `sha256:` followed by the lowercase hex SHA-256 of the **manifest bytes**: the node's entries as a JSON array of objects with exactly the keys `executable` (boolean), `name` (string), `sha256` (string) and `size` (integer), sorted by `name` compared as UTF-8 byte strings, serialised by the JSON Canonicalization Scheme (RFC 8785: keys in code-unit order, no whitespace, minimal string escaping), encoded as UTF-8 with no trailing newline. Other mode bits are not part of the identity. Test vector: a file `select_best.py` holding the 11 bytes `print('a')` plus a newline (`sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb`, not executable) and an empty executable file named `bin/run "ñ".sh` (`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`) give the manifest bytes `[{"executable":true,"name":"bin/run \"ñ\".sh","sha256":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","size":0},{"executable":false,"name":"select_best.py","sha256":"sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb","size":11}]` and `resource_set_digest` `sha256:ac238bb1de5db1d8c8cee691626af721119ffc241431f22603ce77c22af1e305`; the compiler, the API and every runtime must reproduce it. Changed content under an unchanged name changes the file digest, the set digest and the IR digest, so it deploys as a new graph version; no package name or version enters the identity. The content is not in the IR: `kora compile` writes it next to the IR as the resource bundle `build/<graph_id>.resources/<hex>`, one file per distinct digest, and the deploy request carries it ([dsl.md](dsl.md) section 2, [api-and-agent-surface.md](api-and-agent-surface.md) section 2). `cmd` refers to a resource as `{resource_root}/<name>`; the runtime materialises the verified bytes there, outside the candidate root and read-only for candidate code on runtimes declaring `protected_execution` ([protocols.md](protocols.md) section 2). The authoritative bytes are the stored, verified content; a repository file with the same name is never consulted. A resource is executed through an interpreter named in `cmd` and present in the runtime image; read-only resources protect file integrity and do not make a gate an independently trusted evaluator (ADR-0009). The `CommandResult` records the `resource_set_digest` and the per-file digests verified immediately before execution, with the runtime's `resource_protection` (`protected` or `none`); the `ReviewBundle` facts show them next to each gate.

The branch fields else and else_reason and the fan-out fields max_parallel and skip_failures are optional in structural IR. Omission is preserved; structural models do not insert execution defaults.

### Structural configuration data and canonical export

The configuration records preserve authored omissions with `MISSING`. Optional fields refuse explicit null, except `human_input.default_value`, whose JSON null is distinct from omission. Empty strings, maps and lists and explicit false values retain their meanings. Structural parsing does not resolve providers, execute CEL/templates, insert execution defaults, admit first-slice capabilities or schedule waits. Pure models and export helpers acquire no operational resources and emit no diagnostics; public error mapping remains with the shared validator under [diagnostics.md](diagnostics.md).

`agent.hooks`, `agent.output_schema`, `human_input.form` and sensor `config` are open object-root JSON data. `human_input.default_value` accepts any finite JSON value. Nested boolean schemas and arbitrary schema keywords remain data; boolean roots for object fields are refused. These fields use the shared iterative IR-data validator rather than the bounded protocol JSON parser. There is no added field depth, byte or integer-size cap. API ingress retains its own whole-request bound, schema/provider consumers retain their admission contracts, and record `x-` extensions retain their existing bounds. An `x-` key inside open data is ordinary data and grants no authority or semantic-validation exception.

Open fields are detached from caller-owned dictionaries/lists. Non-string keys, non-finite numbers, runtime objects, cycles and repeated dictionary/list identities anywhere inside one open field are refused with content-free errors. Mutable identity shared across separate open fields is allowed; immutable scalar sharing and reuse of validated configuration instances elsewhere are allowed. Alias refusal prevents folded input graphs from expanding while copying and introduces no aggregate field limit.

`export_ir(root: IrRecord)` returns a detached JSON object and `render_ir_json(root: IrRecord)` renders its compact JSON representation. These are the canonical export boundary for the implemented record hierarchy. They preserve wire aliases, authored field sets, list/map order, exact integers, finite float values and flattened `x-` extensions. Unauthored defaults, `MISSING` and private metadata do not appear. They preflight mutated open data using the same rules. The writer uses deterministic `ensure_ascii=True` string escaping, lowercase JSON literals, established finite-float encoding, unquoted integer encoding, no insignificant whitespace and no trailing newline. Non-ASCII, astral characters, controls and materialized lone surrogates are escaped without a new Unicode admission rule.

These helpers accept materialized data; they do not replace JSON/YAML text decoders and make no arbitrary-depth decoding claim for `json.loads`, Pydantic JSON parsing or YAML loaders. The IR representation boundary must preserve YAML timestamp tokens as raw strings. No production YAML loader is implemented here.

[Dated implementation and measurement evidence](../research/ir-document-models-2026-10-23.md) distinguishes model, checker and decoder coverage from pending consumers.

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

`tool_policy` is the input to the **policy protocol** ([protocols.md](protocols.md) section 12, DD-29) and the only tool-permission field in IR 1.0: the DSL's `allowed_tools` and `disallowed_tools` compile into it. The harness's pre-tool-call hook asks the session runner before every tool call the harness does not reject itself; the runner evaluates the namespace's `tool_policy` (set by the platform team) and the node's per layer: a deny from either stops the call, every layer with a non-empty `allow` must match, so a node never widens a namespace allow list, and order inside an array has no effect; a denied call is recorded as a `permission_denied` session event on the `AgentSession`. A call a harness permission rule denies is blocked without reaching the runner and, on an adapter that reports such denials, is recorded from the harness's report with `source: harness`; a call a built-in tool's own input validation rejects never runs and is an ordinary failed tool result, not a denial ([protocols.md](protocols.md) sections 3 and 12). The one exception to the policy is the harness's output channel: on `claude` the adapter allows the synthetic `StructuredOutput` tool of a node with `output_schema` without evaluating either policy, under the conditions of [protocols.md](protocols.md) section 3 (ADR-0009, amended 2026-10-19). Patterns use kora's own grammar and bounds, stated in [protocols.md](protocols.md) section 12 with where it differs from each harness; a shell call with a form on section 12's closed indirection list matches every governing `Bash` deny and no allow pattern. The field is inherited through the preset layers. Every file-edit tool of the harness (section 12's file-edit family) is denied on `protected_paths` (DD-34) for every agent node of the graph. MVP ships the `allow_deny` implementation; Cedar or OPA arrive as plugins without an IR change.

**Protected paths** (DD-34): `runtime.protected_paths` plus the node's `protected_paths` list gate scripts, build files, test directories and tooling configuration. Each entry is a path glob (issue #233): a relative path with `/` separators, at most 4,096 characters and 32 segments, with no empty, `.` or `..` segment and no NUL or control character. `*` matches within one segment, and `**` as a whole segment matches zero or more segments; `?`, `[`, `]`, `{`, `}`, `\` and a `**` inside a longer segment are refused. A leading `~`, a searched piece (every piece of a segment but the last) longer than 96 characters in its case-folded form, and a `*` that touches a character of nonzero combining class in the decomposed or the case-folded form (canonical reordering could move another mark of a name across it) are refused too, and the globs of one call hold at most 8,192 pieces, 64 KiB and 16,384 characters outside ASCII together. Every pattern ends in an implicit `**`, so `tests` protects every file under `tests/`. Both sides compare in canonical caseless form (NFD, case folded, then NFD again, so a glob prefix compares in decomposed form and also covers a name whose next character carries a combining mark), on every platform, with the interpreter's Unicode case tables; a name a filesystem treats as equal beyond canonical caseless equivalence (a case pair newer than those tables, an ignorable code point, compatibility normalization) is caught only by the gate-time check below. The combined runtime and node list a call carries is at most 64 globs. Checking the globs and that bound when the IR is validated is pending work item 3; until then an invalid list surfaces at the first tool call as `protected_paths_invalid`, which fails closed. `kora.ir.globs` implements the syntax for the policy and the executor alike. Before a command gate runs, the preparation phase of its attempt compares the paths changed between the base commit and the candidate commit (the `changed_paths` capture of the checkout plan, [protocols.md](protocols.md) section 4) with those paths and fails the attempt with class `protected_path_modified` on a match; no second checkout is made.

### 4.2 Human-input node config (FR-2.2, FR-7)

The [first-slice chain](first-slice.md#refusal-contract) uses direct CLI bindings to its published `VcsRef` and command `CommandResult`, as in [dependency-upgrade.ir.yaml](examples/dependency-upgrade.ir.yaml). A ReviewBundle is an optional context artifact, not a requirement for this CLI path; its fact/narrative ownership remains unchanged when used.

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

### 4.5 Branch ownership and selection

Every ordinary target named by any branch case or `else` must exist in that graph/body scope and have the branch ID directly in its `depends_on`. DSL lowering inserts the edge; direct IR validation requires it (`IR010`). A direct target has exactly one owning branch in MVP; listing it in multiple cases of that same branch is allowed. Multiple distinct owners are `IR010`. Ordinary DAG validation (`IR001`) includes these edges, rejecting self-targets and routing cycles. `$fail_attempt` is a reserved subgraph action and creates no node/control edge.

MVP rejects `attempt_invariant: true` on every `branch` and on every ordinary direct target named by any case or `else` (`IR010`, at the offending node's flag). Apply this semantic rule in every graph/body scope, including nested definitions, regardless of retry count or which case may be selected. A branch that only routes to `$fail_attempt` is still a branch and cannot be invariant; the reserved action itself is not a node to validate. Omitted/false flags remain valid, and other nodes retain ordinary invariant reuse. The shared validator used by compile and deployment enforces the rule; no runtime cached-route mechanism is introduced.

Selection is an additional readiness gate, not a substitute for ordinary dependencies or trigger/input checks. Every evaluation uses the current concrete owner instance's compact persisted selection ([data-model section 5.1](data-model.md#51-current-branch-selection)). An unresolved owner blocks readiness; a skipped owner or a successful owner that excludes the target skips it; failed/upstream-failed owner produces `upstream_failed`. A selected target still waits for all ordinary upstreams to be terminal and must satisfy its trigger rule and required inputs. The owning branch's control edge participates normally: its success counts for `one_success` and prevents `all_skipped`. Input independence cannot remove this gate.

Only direct targets have this ownership constraint. Untargeted reconvergent joins and failure handlers retain ordinary trigger semantics; no recursive blanket skipping occurs. Target-only clear cannot erase a retained exclusion, and owner clear removes its old selection before reevaluation. No new IR field or hidden runtime edge category is introduced.

## 5. Sub-graph attempt semantics (FR-2.7a)

A loop is a subgraph with `retry.max_attempts > 1`. Rules materialised in the IR:

- A node inside the body can end the attempt with `fail_attempt`: a `branch` case whose `to` is the reserved target `$fail_attempt` with a `reason` expression. The attempt ends `failed` with error class `attempt_failed`.
- Sub-graph attempt N+1 receives the subgraph's inputs plus `$previous_attempt`, a built-in `PreviousAttempt` artifact: `{subgraph_attempt: N, reason, error_class, artifacts: {<node_id>.<output>: <artifact ref>}}`. A node receives it only by declaring an input with `from: $previous_attempt`, so the dependency is visible in the graph. In the control plane a sub-graph attempt is the `subgraph_attempt` integer on the node instances; the word "attempt" alone always means a runtime submission ([data-model.md](data-model.md) section 5).
- Nodes permitted by section 4.5 to use `attempt_invariant: true` are not re-run in sub-graph attempt N+1; their earlier artifact version is reused and lineage records the reuse.
- `limits` on the subgraph (timeout, budget) span all sub-graph attempts; `limits` on body nodes apply per node attempt. `retry.max_attempts` on the subgraph counts sub-graph attempts; body nodes keep their own node-level retries inside each.
- Artifacts of every sub-graph attempt are retained; the previous ones are marked `superseded` when a new sub-graph attempt starts (FR-4.5).
- VCS side effects of failed attempts are never touched (FR-2.7, FR-6.1b).

## 6. Retry policy and strategy (FR-6.3, FR-6.4)

For first-slice installations, omitted `runtime.provider_retry`, an empty object and any schema-valid zero `max_wait` resolve to `{max_wait: "0s"}`, the disabled policy. This built-in default is chosen before the enabled-feature namespace/six-hour fallback. An explicit positive value or a positive default in namespace, binding, standalone or `kora.toml` configuration is refused before effects, never silently replaced. Malformed/null values retain schema errors. Save the normalized zero in the graph version's required `execution_contract.retry_policies` map ([data-model section 11.1](data-model.md#111-graph-version-contract)); its contract and deployment digests bind these values so a later installation upgrade cannot reinterpret omission. Numeric budget zero remains refused independently. Zero disables provider-pause/deadline/backoff and new-attempt scheduling, not observations, interruption, same-attempt recovery or closure. The future enabled behavior below applies only to a positive resolved provider wait.

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

For first-slice `runtime.infrastructure_retry`, omission, `{}` and a valid object with omitted or explicit-one `max_attempts` resolve to a saved explicit `max_attempts: 1`: one initial attempt and zero replacements. Select this disabled built-in before enabled-feature fallback. Refuse any explicit or operator-configured count above one, including namespace, runtime/binding, standalone and `kora.toml` defaults, even when an author override, empty retry list or false predicate would hide it. Never clamp an enabling default silently. Preserve schema/type errors and static checking of every dormant field, including CEL predicates, and retain validated authored fields without allowing them to reinterpret the saved disabled policy after restart or feature activation. Store this complete normalized policy in the same per-node `execution_contract.retry_policies` map. Run, attempt, admission and recovery consumers use that saved map, including before the first attempt after an upgrade; missing saved policies fail closed under the graph-contract compatibility rules. No infrastructure predicate, backoff or counter increment can authorize a replacement in this slice.

Error classes (closed enum, used by `retry_on`, `non_retryable` and the data model): `harness_error`, `command_failed`, `timeout`, `events_exhausted`, `budget_exhausted`, `artifact_invalid`, `attempt_failed`, `approval_rejected`, `approval_expired`, `candidate_moved`, `wait_expired`, `stalled`, `protected_path_modified`, `secret_leak`, `provider_limited`, `integrity_error`, `infrastructure`, `cancelled`, `config_error`. Execution uncertainty is not an error class or an IR retry option: lease expiry preserves the original non-terminal attempt and reservations. ADR-0017 blocks all new runtime admissions in its run while execution/publication recovery is unresolved. Recovery can settle by affirmative adoption of the same live execution or its authoritative immutable prepared/completed checkpoint once every earlier grant/operation is closed; direct replacement still requires runtime termination and every dispatcher/platform publication closure. Manual retry records clear/cancel intent while blocked; a linked rerun is refused with `API002` for any replacement-blocking condition. A separately triggered run follows the ordinary target policy. `infrastructure` and `provider_limited` never consume **agent** attempts: `infrastructure` is retried only when the feature is enabled and the saved separate `infrastructure_retry` policy at `runtime` level permits another attempt (NFR-2.4); the first-slice saved count of one forbids replacements, and `provider_limited` under `runtime.provider_retry`, which waits until the provider's reset (or with back-off from 1 to 30 minutes when none is reported) and fails the node only after `max_wait` of total waiting; neither class may appear in a node's `retry_on` (ADR-0024, NFR-2.6). `stalled`, `protected_path_modified` and `secret_leak` are ordinary classes decided by the node's policy. `config_error`, `cancelled`, `candidate_moved` and `integrity_error` are never retryable; `integrity_error` is a command resource whose bytes do not match their pinned digest before execution (Archon's "never retry config errors"; a retried approval node would request again on the bundle built for the old commit, [scheduler.md](scheduler.md) section 5).

`strategy: resume` requires the runtime capability `supports_resume`; when the runtime lacks it, when the session handle is gone or when the installed harness version changed, the attempt falls back to `fresh_with_feedback` and records `strategy_fallback` (FR-6.4). In MVP neither reference runtime declares it (`local` and `docker` release the workspace that holds the harness checkpoint, [protocols.md](protocols.md) section 2), so `kora validate` warns `IR009` for `strategy: resume` on them and every such retry takes the fallback; `fresh_with_feedback` is the strategy that works on every runtime and the documented default where the next attempt needs the previous attempt's work.

## 7. Expressions and templates

Branch conditions and reasons, retry predicates, `approve_when`, event `match` and sensor `until` are **CEL** (Common Expression Language) expressions (DD-3). kora parses and checks every expression with its own parser and schema-driven checker, then evaluates it through the Rust-backed `common-expression-language` binding, which the `kora[cel]` extra installs ([ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md) as amended under issues #126 and #167). Other SDKs use any conforming CEL implementation. Only CEL's standard functions and macros are available, narrowed by the rules below. There are no custom functions and no user code. The parser, limits, checker rules, projections, conversions, data bounds and cost model in this section belong to the execution semantics `kora.execution.v1` ([data-model.md](data-model.md) section 11). Every installation applies the same rules, and changing one of them needs a new semantics identity, except the explicitly provisional initial settings before any deployment stores an estimate (section 7.6) or the explicitly supported, narrowly scoped `kora.structured-output-view.v1` compatibility binding for previously unbound StructuredOutput revision-1 contracts (section 2.1). A new binding pin repeats functional conformance, estimate-golden, compact cost-regression, evaluator-lifetime and applicable security checks against those rules. A semantic incompatibility requires a new semantics identity. A newly exposed workload gap needs an explicitly approved measurement method; pin changes do not require exhaustive coefficient fitting or crash-threshold searches. Measurements of the binding cited here are dated evidence from the [CEL binding probe](../research/cel-binding-probe-2026-10-19.md), not guarantees.

### 7.1 Expression kinds and environments

| field | variables | result | evaluated by | an evaluation error (section 7.7) |
|---|---|---|---|---|
| `branch` `cases[].when` | `inputs`, `params` | `bool` | scheduler, branch pass | fails the branch with `config_error`; its direct targets become `upstream_failed` (section 4.5) |
| `branch` `cases[].reason`, `else_reason` (evaluated only on a `$fail_attempt` route) | `inputs`, `params` | `string` | scheduler, branch pass | fails the branch with `config_error`; the iteration does not end as `attempt_failed` |
| `retry.predicate` (node and subgraph) | `outputs`, `inputs`, `params` | `bool` | scheduler, retry handling | no retry; the node keeps its original error class |
| `human_input` `approve_when` | `response`, `inputs`, `params` | `bool` | the shared approval transition (direct API response or channel observation) | the request becomes `failed` and the node fails with `config_error` |
| `wait_event` `event.match` | `payload`, `inputs`, `params` | `bool` | scheduler, wake-ups | the event does not match this wait |
| `wait_event` `poll.until` | `observation`, `inputs`, `params` | `bool` | scheduler, prepared-observation apply | "not yet": the wait keeps polling |
| sensor trigger `poll.until` | `observation` | `bool` | scheduler, start-sensor apply | "not yet": no run starts |

- `inputs.<name>` is the node's declared input, bound from the stored projection of the consumed artifact version (section 7.5). An input declared `optional: true`, and `$previous_attempt`, is a non-required member.
- `outputs.<name>` is an output of the failed attempt, or an export of the failed subgraph attempt, that passed validation and the data bounds. Every member is non-required.
- `params` are the run parameters, typed by `graph.params` and bound from the projection cut at run creation.
- `response` is the approval form response, typed by `form`, projected and bound-checked when the response is received.
- `payload` is the inbound event body and `observation` the sensor result. Neither has a declared schema; both are `map(string, dyn)`.

### 7.2 Parser, limits and canonical rendering

kora's own lexer and recursive-descent parser follow the CEL grammar of cel-spec, without backtracking. Positions are zero-based offsets in Unicode code points into the decoded field value.

**Characters and literals** (`IR011`):

- Identifiers, digits and whitespace are ASCII: an identifier is `[_a-zA-Z][_a-zA-Z0-9]*`, whitespace is space, tab, line feed, form feed and carriage return, and any other character outside a literal or a comment is rejected. kora never accepts a character that CEL's grammar rejects and then rewrites it in the rendering.
- These code points are rejected anywhere in the expression, inside literals and comments too: C0 controls other than tab, line feed, form feed and carriage return; DEL and the C1 controls; U+2028 and U+2029; the format characters U+00AD, U+0600 to U+0605, U+061C, U+06DD, U+070F, U+0890 and U+0891, U+08E2, U+180E, U+200B to U+200F, U+202A to U+202E, U+2060 to U+206F, U+FEFF, U+FFF9 to U+FFFB, U+110BD, U+110CD, U+13430 to U+1343F, U+1BCA0 to U+1BCA3, U+1D173 to U+1D17A, U+E0001 and U+E0020 to U+E007F (bidirectional, zero-width, tag and other format controls, the Unicode 17 general category Cf); and surrogate code points. The set is pinned here, not read from the running interpreter's Unicode database. Variation selectors are combining marks and stay allowed. An author who needs a rejected character in a string writes its `\u` or `\U` escape.
- A comment runs from `//` to the next line feed, as in the cel-spec grammar. A carriage return inside a comment that a line feed does not follow is rejected, because editors show it as a line break; a form feed does not end a comment. A raw carriage return inside a string or bytes literal, triple-quoted and raw ones included, is rejected too, because the binding would read it as a line feed; authors write `\r`.
- Escapes are those of the cel-spec grammar: `\u` and `\U` are valid only in string literals, and an escape denoting a surrogate or a code point beyond U+10FFFF is rejected. The hexadecimal prefix is `0x`.
- An `int` literal beyond the signed 64-bit range, a `uint` literal beyond 64 bits and a `double` literal whose value is not finite are rejected. A run of unary `!` or of unary `-` is reduced as cel-go and the binding reduce it (decided on [issue #162](https://github.com/realkarych/kora/issues/162)): an even run is dropped and an odd run keeps one operator, so `!!x` and `--x` are `x` and `!!!x` is `!x`. A single `-` directly before an `int` or `double` literal that no selection or index follows is part of the literal, as is such a `-` after a run of `!`, so `-9223372036854775808` and `!-1` hold literals and `-1.f` is the negation of `1.f`. The `-` kept from a longer odd run stays a negation of the unsigned literal, as in the binding, so `---1` is `-(1)` and `--9223372036854775808` and `---9223372036854775808` are rejected because `9223372036854775808` is out of range. The depth rule below still counts every operator of the author text. The binding's unary negation of the smallest integer wraps instead of overflowing, a deviation the evaluator has to account for.

**Constructs** (`IR011`): message construction; optional syntax (`.?`, `[?`, `{?`); leading-dot qualified names, which have no meaning without containers; backtick-quoted field selection; two-variable comprehensions (a third argument of `all` or `exists`; a third argument of `filter` or `exists_one` is a malformed macro); a `has()` argument that is not a field selection; a macro iteration variable that is not a simple identifier; a reserved word as an identifier (reserved words stay valid as field names after a dot); and functions outside the standard function table.

**Standard function table.** The global functions `size`, `int`, `uint`, `double`, `string`, `bytes`, `type`, `dyn`, `duration` and `timestamp` with one argument; the methods `size()`, `contains`, `startsWith`, `endsWith` and `matches` with one argument; and the timestamp and duration accessors `getDate`, `getDayOfMonth`, `getDayOfWeek`, `getDayOfYear`, `getFullYear`, `getHours`, `getMilliseconds`, `getMinutes`, `getMonth` and `getSeconds` without arguments. The table is the cel-spec standard library narrowed to what binding 0.10.0 executes: it lacks `bool()`, the global form `matches(s, p)` and the time-zone forms of the accessors, so they are rejected. Gaps that depend on argument types (`int(timestamp)`, `timestamp(int)`, date accessors on durations) are left to the schema-driven checker. Every entry is verified by evaluation in the binding, and every pin change repeats that verification.

Static limits. An expression or path over any of them is `IR027`, including a `maxLength` violation found by schema validation:

- At most **4,096 code points**, checked before lexing. `ir.schema.json` carries the same `maxLength`.
- Syntax-tree depth at most **32**. Depth is measured on the built tree: a leaf has depth 0, and every edge from a node to a child adds one level, so each binary-operator operand, conditional `?:` condition and branch, selection, index operand and index, call receiver and argument, unary operand, list element, map key and value, and macro range and body adds one level. Each explicit parenthesis adds one level too, and a macro body counts once. It is checked on every descent of the parser, before recursing, and as each node is built, so prefix nesting (`((((`, `!!!!`, `----`) stops with `IR027` before any recursion limit and chains a parser builds in a loop (`a + a + …`, `a.b.b…`) count fully.
- At most **4 comprehension macros** (`all`, `exists`, `exists_one`, `map`, `filter`) per expression, nested at most **2** deep. A macro nests in another when it appears in that macro's body; a macro in another macro's receiver, as in a chain `l.map(…).map(…)`, does not nest.

Lexing, parsing, checking and the cost estimate are linear in the size of the tree. Checking stops at an expression's first error.

The probe measured these thresholds on one macOS machine:

- the binding rejects bracket nesting from 96;
- a chain of about 12,000 selections or additions crashed the process with SIGSEGV on a main thread;
- on a 256 KiB thread stack, a chain of 300 survived and 1,000 crashed.

These observations do not establish a crash margin for every admitted expression on the 1 MiB evaluation thread of section 7.6 or on every supported platform. Functional boundary tests remain required on pin changes under the rule above; no full native crash-threshold search is a current gate. A native stall or crash can affect the shared API or scheduler process.

**Canonical rendering.** From the checked tree kora writes a canonical text: every operator parenthesized, string literals as double-quoted non-raw literals with only the escapes `\\`, `\"`, `\n`, `\r`, `\t` and `\u`/`\U` for other control and non-ASCII characters, bytes literals the same with `\x` for every byte outside printable ASCII, no comments, single spaces. Validation parses the rendering again with kora's parser and requires the same tree, ignoring grouping parentheses, otherwise `IR011`; this uses only kora's parser, so `kora compile` runs it too. Each node puts at most one pair of brackets around its children, except that a unary operator applied to a numeric literal renders the literal in parentheses, `(-(1))`, so that it does not fold into a negative literal, and a negative literal used as the operand of a selection, index or method is parenthesized. Doubles are rendered in their shortest round-trip form and integers in decimal. The rendering is not held to the author-text limits: its length is at most 16 times the source's (a non-ASCII character in a bytes literal renders as up to four `\xHH` escapes), and its re-parse runs under a depth bound of 2 × 32 + 1 levels, checked before recursion, which keeps its bracket nesting below the binding's bracket limit of 96; the boundary tests compile the maximal rendering of each shape in the binding. The binding compiles only this rendering, never the author's text, so it evaluates exactly the tree kora checked. The IR keeps the author's text.

**Where the binding runs.** Server-side validation (`/validate`, `/plan`, deployment, and `kora_compile` through `/validate`) compiles every rendering with the binding, deduplicated by rendering, and compiles each distinct `matches()` pattern once; a rejection is `IR011`, reported without the binding's message. `kora compile` runs on the author's machine with kora's parser and checker only and never imports the binding ([dsl.md](dsl.md) section 2). Deployment stores the rendering, the projection path sets (section 7.5) and the cost estimate with the graph version, and evaluation never recomputes them.

### 7.3 Paths

`inputs[*].path`, `binds.vcs_ref`, `binds.gate_digests`, `workspace.from_ref`, `human_input` `context` and `graph.target.key.path` are member-access chains restricted to field selection and indexing, such as `inputs.bundle.facts.change` or `artifacts['implement.change']`. They contain no operators and no calls. This is the one path notation in the IR.

- **Parsing.** Paths use the lexer and the length and depth limits of section 7.2: `IR011` for syntax, `IR027` for limits. An index is a non-negative decimal integer literal within the signed 64-bit range or a string literal; a negative, hexadecimal or unsigned index, or an index expression, is `IR011`. A relative path starts with a field name that is not a reserved word, so a key that is not an identifier cannot start one. Resolution keeps its existing codes (`IR002`, `IR003`, `IR005`, `IR020`).
- **Walking.** The walker treats a missing key, an index beyond the list, and a field or index on a value of another JSON type as absent. It never applies negative indexing.
- **Resolution.** kora resolves paths with its own walker; a path never reaches the binding.
- **Roots.** `inputs[*].path` and `graph.target.key.path` are relative to their source value. `binds.*`, `workspace.from_ref` and `context` start at `inputs.`.

`timer` takes a `duration` or an absolute `at` time, never an expression.

### 7.4 Schema-driven checker

Types come from the saved type closure (section 2.1). It is resolved lazily and memoized within one validation, so recursive `$ref` structures are checked in bounded time. Each input is typed from the producer's effective saved output contract at its call site.

A schema is mapped after resolving `$ref` and a single-schema `allOf`. Its type then comes from `type`, or, when `type` is absent, from its keywords: `properties` means an object, `items` or `prefixItems` an array, and `enum` or `const` the JSON type of their values. The first matching row wins:

| JSON Schema | CEL type |
|---|---|
| pure `type: null`, `const: null` or `enum: [null]` under `kora.structured-output-view.v1` | existing `NULL` / `null_type`; produced null remains distinct from absence |
| `type: [T, "null"]`, `anyOf`/`oneOf` of `T` and `null`, or an `enum` of one type plus `null` | nullable `T`, following CEL wrapper types; when `T` is already nullable, that same nullable type, so no nullable type wraps another |
| object with `properties` | record of the declared fields, with the `required` set; `additionalProperties` types index reads |
| object without `properties`, or with only `patternProperties` | `map(string, V)`; `V` from `additionalProperties` or `patternProperties`, else `dyn` |
| string, or an `enum` or `const` of strings | `string`, with its allowed values |
| integer | `int` |
| number, or an `enum` or `const` of numbers | `double` (`int` under `type: integer`) |
| boolean | `bool` |
| array with `items` | `list(T)`; with `prefixItems` or without `items`, `list(dyn)` |
| a `type` array of several non-null types, any other `anyOf`, `oneOf` or multi-schema `allOf`, an `enum` of mixed types, the boolean schema `true`, a schema with no type, keyword or `$ref` | `dyn` |

`payload` and `observation` are `map(string, dyn)`, and a `dyn` value is checked dynamically:

- dot selection on `payload`, `observation` or another `dyn` value is allowed and needs no guard, and a missing key is the evaluation error `missing_field`; a schema-typed `map(string, V)` is read by index, guarded by `in` like an open object's extra keys;
- a `dyn` operand satisfies the same-type rule for `==`;
- a `dyn` result passes checking and is checked for exact `bool` (or `string`) when evaluated.

These rules give `IR011`, reported at the expression's YAML path and offset:

- **Identifiers and fields.**
  - An unknown identifier is rejected. `inputs` and `outputs` may appear only under field selection.
  - A dot selection of a field outside `properties` is an unknown field, also on an object that allows additional properties. Undeclared keys of an open object are read only by index, `x['key']`.
  - A comprehension variable that shadows an environment variable is rejected.
- **Enum literals.** A string literal compared with an `enum`-typed value must be one of its allowed values.
- **Numbers.**
  - `==` and `!=` require the same type, so `int == double` is rejected, as in the cel-go and cel-cpp checkers.
  - Ordering across `int`, `uint` and `double` is allowed.
  - Arithmetic that mixes numeric types needs `double()` or `int()`.
- **Optional fields.** Reading a non-required field without a guard on the same path is rejected.
  - The guards are:
    - `has(p)` as any conjunct to the left of the read in an `&&` chain;
    - `!has(p)` as any disjunct to the left of the read in an `||` chain;
    - `has(p)` as the condition of `?:` for its true branch.
  - `'k' in x` guards `x['k']` in the same positions.
  - The parent of a `has()` operand must itself be present or guarded, as in `has(a.b) && has(a.b.c) && a.b.c`.
- **Null.** A comparison with `null` yields `bool`. Selection, ordering, arithmetic or a call on a `null` value is an evaluation error.
- **`size()`** on a `string` or a `dyn` value is rejected while the binding counts UTF-8 bytes instead of code points.
- **`matches()`** takes a string-literal pattern only, checked by the parser of section 7.2. kora parses the pattern in a closed subset of the binding's regular-expression syntax:
  - **Supported:** literal characters and escaped ASCII punctuation other than `<` and `>`; the escapes `\n`, `\t`, `\r`, `\f`, `\v`, `\a`, `\xHH` and `\x{H…}`; `.`; the class escapes `\d`, `\D`, `\w`, `\W`, `\s` and `\S`; `\p{…}` and `\P{…}`, or their one-letter forms, for the Unicode classes of the class-size table (`L`, `Lu`, `Ll`, `N`, `Nd`, `P`, `S`, `Z`, `Greek`, `Latin`, `Han`, `Cyrillic`); bracketed classes, negated or not, with ranges and nested bracketed classes; the anchors `^`, `$`, `\b`, `\B`, `\A` and `\z`; capturing, non-capturing and named groups, each name unique within the pattern and starting with a letter or underscore; the flags `i`, `m` and `s`, with at most one `-` that a flag follows; alternation; and the repetitions `*`, `+`, `?`, `{n}`, `{n,}` and `{n,m}`, each optionally lazy.
  - **Rejected** (`IR011`): every other construct, among them verbose mode `(?x)`, the flags `U`, `u` and `R`, look-around, backreferences, `\Q…\E`, `\<` and `\>` (word-boundary assertions in the binding), POSIX classes, class set operations (`&&`, `--`, `~~`), a range that starts or ends at a class escape, and Unicode classes outside the table.
  - **Nesting.** Groups and bracketed classes share one nesting counter of at most 32, checked before the parser recurses.
  - **Size estimate.** Pattern units are a positive fixed base plus the maximum of atom/class/repetition units and a syntax-character floor. Each atom uses the provisional class-size tables pinned under `kora.execution.v1`. Empty patterns, sibling empty captures, empty alternatives and flag-only groups therefore retain a nonzero charge. The base and syntax-character coefficients are each 1. An `i` flag anywhere in the pattern applies the case-insensitive weights to the whole pattern. A bracketed class is charged the sum of its members' weights, and a negated one at least the weight of `.`; a range that reaches beyond ASCII and a negated Unicode class are also charged at least the weight of `.`. A repetition multiplies by its upper bound, or by its lower bound plus one when it is unbounded (so `+` multiplies by two and `*` by one), and adds a fixed overhead.

  A pattern over the nesting limit or the provisional estimate cap of 1,048,576 is `IR027`. The tables, base, syntax floor and cap remain unchanged under the lightweight validation of section 7.6; they are not a calibrated native-memory or compiled-size envelope. The server-side compile also compiles the pattern in the binding.
- **Result type.** The result must have the field's type from section 7.1.

**Settled cases** (decided on [issue #330](https://github.com/realkarych/kora/issues/330)). Where the table and rules above leave a case open:

- **Mapping.** Pure null outside `kora.structured-output-view.v1`, and the boolean schema `false`, are `dyn`. `$ref` and a single-schema `allOf` take their target's type; sibling keywords do not change it. Local references are JSON pointers within their document and cross-type references resolve only through the saved closure. A `$id` on a schema object below a document root, also one a JSON pointer passes through (a member of `properties`, `$defs` or a similar map named `$id` is not a resource), a malformed keyword, an unresolvable reference and a pointer index that is not canonical are closure faults the validator reports as `IR022` before checking. Mapping resolves every reference chain and visits every schema object at most once per document root and view, and never recurses on the interpreter stack, so recursive, repeatedly referenced and deeply nested closures map in counted linear steps; a failed mapping, including a failure of the caller's resolver, leaves no partial types behind.
- **Additional and pattern properties.** An object with `additionalProperties: false` and no `patternProperties` declares no further keys. Otherwise an undeclared key has the `additionalProperties` type when there are no `patternProperties`, the single pattern's type when `additionalProperties` is `false` and exactly one pattern exists, and `dyn` in every other case; a map without `properties` takes its value type the same way. This replaces reading a map's value type from `patternProperties` alone, which keys outside the pattern would not satisfy.
- **Conversion checks JSON kinds.** Before evaluation, conversion refuses a value whose JSON kind does not match its declared CEL type with `invalid_value` (section 7.7): numbers for `int`, `uint` and `double`, strings, booleans, `null`, arrays for lists and objects for maps and records; a nullable type also admits `null`, and `dyn` admits any kind. Keyword-inferred object and array types therefore hold for every bound value even though JSON Schema validation alone admits other kinds there.
- **Schema-tightened bounds** ([issue #374](https://github.com/realkarych/kora/issues/374)). With each CEL type the mapping derives the variable's value bound: a schema whose type maps to a list declares an element count of the smaller of `maxItems` and 1,000, and one whose type maps to `string` declares a UTF-8 length of the smaller of four bytes per `maxLength` code point and 16 KiB. Element bounds follow `items` and the additional-properties type, field bounds follow `properties`, through the same reference, single-`allOf`, nullable and view rules as the types; a keyword on another type, a sibling of `$ref`, `prefixItems` positions and a schema of several types tighten nothing. Byte and value counts are never tightened: every declared bound keeps the 4,096 bytes and 256 values of the data bounds. A node declares children only when something below it is tightened, and a node reached again while its own mapping is open (a recursive reference) declares nothing, so recursive closures never produce cyclic bounds and only loosen. A record whose additional-properties bound is tightened declares every property, an untightened or recursive one at the data bounds, because an undeclared field reads the element bound. A `maxItems` or `maxLength` that is not a non-negative integer on a schema the mapping classifies is a closure fault (`IR022`); keywords the mapping never reads, such as siblings of `$ref`, are not checked here. `SchemaTypes.variable` returns the type and bound together.
- **Environment.** The checker receives each variable's CEL type, the field's required result type and the roots that may appear only under field selection. `payload` and `observation` enter as `dyn`, which gives them the unguarded dot selection above.
- **Undeclared keys.** A literal-key index read of an undeclared key on an object that declares no further keys is an unknown field. Otherwise the read has the undeclared-key type above and needs a guard, as the key is optional.
- **Guards.** A guard and a read match on a section 7.3 path: field selections and literal indexes from the same variable or comprehension binding, where a field and a string key of the same name are the same step, so `has(x.f)` and `'f' in x` guard both `x.f` and `x['f']`. A dynamic-key read of a map or object needs no guard: it reads the whole subtree (section 7.5) and a missing key is the evaluation error `missing_field`. A read whose operand is not a path, such as an element of a list literal, cannot be guarded. Guard facts compose: the true facts of `has(p)` are `p`, of a literal `'k' in x` are `x['k']`, of `a && b` the union and of `!a` the false facts of `a`; the false facts of `a || b` are the union and of `!a` the true facts of `a`. The right operand of `&&` sees the true facts of its left operand, the right operand of `||` the false facts of its left operand, and the true branch of `?:` the true facts of its condition; facts stay valid inside comprehension bodies.
- **Null.** `==` and `!=` with `null` accept a nullable, `null` or `dyn` operand only; presence is tested with `has()`. Selection, ordering, arithmetic and calls on a nullable value are checked against its inner type. A nullable result is not the required `bool` or `string`.
- **Maps and joins.** Dot selection on any map, schema-typed or literal, is an unknown field; maps are read by index. Every map literal key is `int`, `uint`, `bool`, `string` or `dyn`, and so is the left operand of `in` on a map; a repeated literal key is `IR011`, because the binding keeps the last value where cel-spec fails. List indexes are `int` or `dyn`; map indexes match the key type. Heterogeneous list and map literal elements and `?:` branches join to `dyn`, and `T` with `null` to nullable `T`.
- **Equality and literals.** Scalars compare only with the same kind, and a string with allowed values compares with any string; a list compares with a list and a map or object with a map or object; `dyn` compares with anything. Enum literals are checked in `==`, `!=` and the list-literal elements of `in`.
- **Overloads.** Operators and functions follow the cel-spec standard overloads restricted to what the binding evaluates correctly: `int(timestamp)`, `timestamp(int)`, `string(bool)`, `duration + timestamp`, the date accessors and `getMilliseconds()` of a duration are absent, and unary `-` and `getMilliseconds()` do not accept a `dyn` operand, where the binding negates booleans and returns a duration's total milliseconds; all of these are `IR011`. `string()` of time values, accessor time zones, invalid UTF-8 in `string(bytes)`, time ranges, error absorption, iteration order, repeated map keys computed from data and mixed `int` and `uint` keys reached through `dyn` remain the deviations of section 7.8.
- **Diagnostics.** The `IR011` reasons are `unknown_identifier`, `root_not_selected`, `unknown_field`, `shadowed_variable`, `unknown_enum_value`, `no_matching_overload`, `unguarded_optional`, `string_size`, `repeated_map_key` and `wrong_result_type`, each with a fixed message and the code-point offset of the failing node; a wrong result type is reported at offset 0. CEL types carry field names and enum values from the schemas, so consumers never log them or place them in diagnostics.

A child type must keep the CEL type of every parent property: assignability (`IR003`, section 2.1) rejects a child that narrows `number` to `integer`.

Diagnostics use the [shared representation and collection contract](diagnostics.md): canonical segmented document paths, code-point offsets and optional source locations. Keep one diagnostic per expression; the enclosing collection owns deterministic path/offset ordering, error and warning bounds and separate omitted counts. An offset is preserved only while the final safe path still identifies the exact expression field.

### 7.5 Projections and typed conversion

**Projection path sets.** At deployment the checker records, for every output an expression can read, the set of paths it reads, and freezes these path sets in `graph_version.execution_contract`.

- **Notation.** A path set is a sorted list of section 7.3 paths, in which `[*]` stands for every element of a list and `['k']` for a map key.
- **Composition.** Paths compose through every route by which a value reaches an expression:
  - `inputs[*].path`;
  - `$inputs` forwarding into a body;
  - the fan-out element, which becomes `[*]` on the mapped list;
  - `collect` and subgraph exports;
  - `$params`, cut from `run.params`;
  - `$previous_attempt`, including `artifacts['n.o']`, whose paths compose into the earlier iteration's output.
- **Whole subtrees.** A dynamic index, a comprehension range and a whole-value read project the whole subtree at their path. A path read only through `has()`, or a key of a map-typed operand tested only by a literal-key `in`, projects its presence (a `dyn` operand projects its whole subtree): the field or key is kept with an empty value of its type, so the test gives the payload's answer without copying the subtree.

**Cutting and storing.** Every projection is cut once, by whichever step produces its value. For `AgentSession`, whose stored payload is the transcript, the projectable fields are exactly its session metadata: harness name and installed version, session id, usage per model with completeness, cost estimate with basis and billing class, turn count, duration, stop reason and provenance (worker id, image digest). These are kept after the transcript expires ([data-model.md](data-model.md) section 10) and are distinct from the artifact type's `summary`. Reading any other `AgentSession` field is `IR011`. Transcript content is never projected, so CEL needs no `transcripts` capability.

- **Uploaded outputs.** An uploaded output is cut when it is validated at upload, including a staged target-source output (ADR-0019).
- **Scheduler-produced outputs.** Each is cut in the transition that produces it: a `human_input` response, a `wait_event` payload output, a `PreviousAttempt`, a fan-out collection and a subgraph export. A collection or export is re-cut from its children's stored projections under its own path set, so no storage I/O happens inside a scheduler transaction; the re-cut reads at most each child's stored projection (at most 4 KiB and 256 values each, for at most 1,000 children), stops writing at the first bound it crosses, and one re-cut counts as progress under the allowance.
- **Run parameters.** They are cut at run creation.

kora cuts from the validated value, converts it as described below, checks it against the data bounds of section 7.6 and stores it inline in the metadata database with the artifact version, or with the run for parameters. A projection keeps its artifact's retention class: the artifact-payload period, forever for reference-only types, and for `AgentSession` the lifetime of its session metadata, which outlives the transcript ([data-model.md](data-model.md) section 10). Listings never load it.

Affected StructuredOutput projections require separate lossless persistence of converted numeric kind, binary64 signed zero and NUL-bearing strings/keys on both databases. Canonical payload bytes do not establish projection fidelity. Work items 4/8/10 must approve a concrete representation and extend `test_cel_projection_number_kinds_round_trip` and `test_cel_projection_matches_full_binding` before activation. Original/converted bounds and collection/export re-cutting remain required; no storage I/O moves into scheduler transactions.

Collection/export projections must equal conversion of the admitted full value under each destination's frozen checker view, including downstream whole-subtree and presence reads. Before a lossy conversion, retain sufficient original admitted projected numeric value, kind and sign in the stored child projection representation to support the composed views; retaining only the already converted values is insufficient. An integer-view child containing `-0.0` converts to int `0`, but collection into `List[StructuredOutput]` with dyn items must still bind double `-0.0`. A number-view child containing integer `9007199254740993` must export that exact integer into an integer-view wrapper, rather than reconstruct it from rounded double `9007199254740992.0`. Both cases extend `test_cel_projection_matches_full_binding` and `test_cel_projection_composes_through_collect_and_exports`. Every re-cut keeps the existing original/converted bounds and saved views; it cannot recover pre-admission source tokens, load storage in a transaction or consult current schemas. Work items 4/8/10 approve the concrete bounded representation, ownership and overhead evidence before activation; this publication chooses no codec or new refusal policy.

**Binding.** Evaluation binds only stored projections.

- **Absent and unavailable.** A path missing from the value is absent in the projection, as it would be in the payload. A projection that is itself unavailable is the error kind `projection_unavailable`, never absent data. A projection is unavailable when retention has deleted it, when it was never cut, or when it was cut under a different path set. It follows its artifact's `payload_state`, except that an `AgentSession` projection, which holds only session metadata, stays available after its transcript expires.
- **Summaries.** The artifact type's `summary` serves API and console views and is never visible to CEL ([protocols.md](protocols.md) section 8).
- **`payload` and `observation`.** They are projected once per event or observation and per frozen path-set digest, by a bounded walker, and the bounds are checked against that path set. A wait's result therefore never depends on which other waits are open. The projection is a disposable cache bounded by entry count, released when the event's batches finish, and recomputed after a restart.

**Conversion.** It follows the producer's saved contract:

- **Integers.** `integer` becomes `int`, and `1.0` becomes `1`.
- **Numbers.** `number` becomes `double`.
- **Values without a schema** (`payload`, `observation`, `dyn` subtrees). Every JSON number becomes `double`, as in CEL's JSON mapping.
- **Strings.** They stay strings, whatever their `format`.
- **Schema defaults** are not filled in.

**Invalid values at entry.** Required CEL conversion refuses a value outside signed 64-bit range under `integer`, a nonfinite `number`/`dyn` conversion or data over a section 7.6 bound. Under the saved `kora.structured-output-view.v1` binding, standalone artifact admission separately permits the [StructuredOutput numerical domain](artifacts/StructuredOutput.md#2-numerical-values-and-canonical-bytes), including larger unprojected integers; required conversion still fails explicitly, never truncating. Unrelated contracts keep their existing entry rules. Direct and polled approvals and run-parameter ingress retain the independently stricter entry checks below, including unused integer fields:

- an output fails with `artifact_invalid`;
- a direct form response, and run parameters at trigger, webhook start and rerun, are refused with `API003`, and the static `params` of cron, sensor and graph triggers are `IR027` at deployment;
- a polled channel response is not applied, the request stays `pending`, and a diagnostic is recorded ([scheduler.md](scheduler.md) section 16.3).

These narrower approval rules also remain in the [API approvals contract](api-and-agent-surface.md). For delayed event/observation/approval producers, StructuredOutput preservation begins at the selected typed snapshot admitted from the retained source. Existing `inbound_event.payload`, `wait.observation` and `approval_request.response` normalization before that boundary is unchanged; no original numeric token or already normalized signed zero is recovered. The producer freezes the admitted artifact once rather than recomputing it through current providers. Both-database source-to-output cases and the harness absence-versus-null gate are assigned in the [activation table](artifacts/StructuredOutput.md#5-verification-and-activation).

**The FR-2.5 example.** In the IR it reads `inputs.review.verdict == "approved" && inputs.bench.cpu < inputs.baseline.cpu * 0.85`.

- With `cpu` declared `number`, a stored `100` becomes `100.0`, so both operands of `*` are `double`.
- With `cpu` declared `integer`, the checker rejects `int * double` and the author writes `double(inputs.baseline.cpu) * 0.85`.
- Without conversion, the binding fails with `TypeError` whenever the JSON value happens to be whole (probe).

### 7.6 Data bounds, cost and evaluation

**Data bounds.** They apply after projection to each stored projection, and to the aggregate of all referenced variables before and after typed conversion. Count every referenced root and every scalar/container occurrence; map keys contribute canonical bytes, not value count:

| bound | limit |
|---|---|
| total size of the bound values, and of each stored projection | 4 KiB (4,096 bytes) of canonical JSON |
| total scalar/container values, and values in each stored projection | 256 |
| list length | 1,000 elements |
| string length | 16 KiB of UTF-8 |
| nesting depth | 32 |
| map size | 1,000 keys |

Where each bound is checked:

- **At the cut.** Every stored projection is checked against every bound where it is cut (section 7.5). For a fan-out `over` list, each element is checked as well as the list. Over a bound:
  - the producer fails with `artifact_invalid`, including a scheduler-produced collection, export, wait payload output or `PreviousAttempt`, whose producing node fails;
  - a form response is checked when it is received, against the paths of `approve_when` and of every consumer of its output: a direct response is refused with `API003` and a polled one stays `pending`;
  - run parameters are refused with `API003` at trigger, webhook start and rerun, and the static `params` of cron, sensor and graph triggers are checked at deployment (`IR027`).
- **At binding.** Both original and converted aggregate bytes and value counts are checked. An original aggregate that exceeds a bound is refused before copying the root that crosses it, even when numeric conversion would shrink it. The checked expression carries the declared bound of every variable its estimate used (section 7.4, schema-tightened bounds), and binding checks each converted value against it after conversion's JSON-kind check, so a value of another kind stays `invalid_value`: a list or map with more entries than a declared element count, or a string longer in UTF-8 than a declared string length, at any depth the bound declares, is refused, so the estimate holds for every bound value whatever produced it. Declared byte and value counts are enforced only through the aggregate data bounds; the schema mapping never tightens them, and an environment builder must keep them at the data bounds for the estimate to hold. Over a bound, the evaluation error kind is `bound_exceeded`. Unreferenced inputs are neither copied nor charged.
- **`payload` and `observation`**, at projection. Over a bound, the result is an evaluation error with the meaning of section 7.7.

**Cost estimate.** At validation the checker computes each expression's worst-case cost from its tree and the size bounds of its variables; a smaller `maxItems` or `maxLength` in a schema tightens the bounds (section 7.4, schema-tightened bounds). The estimate has two dimensions:

- **Steps.** The work charged by the table below.
- **Memory.** A logical estimate over converted data and peak live intermediate sizes, with a provisional fixed allowance and multiplier. It does not measure native representation, allocator overhead or process RSS.

An expression over either dimension of the budget is `IR027`. The charges model the pinned binding's observed complexity, with provisional coefficients:

| charge | proportional to |
|---|---|
| each evaluation | a fixed base, plus the bound size and value count of every top-level variable the expression references; the evaluator binds exactly those (the binding converts all bound data on every call) |
| a node visit | one unit |
| `all`, `exists`, `exists_one` | the range's bound length times the body's charge; nested macros multiply |
| `map`, `filter` | the square of the range's bound length (binding 0.10.0), plus the range length times the body's charge |
| `+` on an operand that may be a string, bytes, list or `dyn` value; list and map literals | the result size and value count, the sums over the operands, which propagate to every consumer |
| result size and element count of `map` and `filter` | for `map`, the range length times the body's result size; `filter` over a list keeps the list's bounds, and over a map, a record or `dyn` it yields a list of the iterated keys or elements, with the range's total size and value count and the iterated element bound; the element count of both is the range length |
| comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in` | the operand sizes and value counts |
| conversions and string functions | the operand size |
| `matches()` | kora's size estimate of its literal pattern, for compiling on every call, plus that estimate times the subject length for matching; the compiled pattern also counts toward memory |
| a compile on a cache miss | the canonical rendering's length |
| projection work in a scheduler transaction (re-cutting a collection, projecting an event); charged to the allowance only, not to an expression | the bytes read and written |

**Bounds and arithmetic.**

- **Hereditary bounds.** Each variable carries a bound: its total size in bytes of canonical JSON, its value count, its element count, its string length, and a bound for its elements or for each declared field. An element or field without its own bound inherits its parent's size and value count, and the element count and string length of the data bounds. Every bound on a cycle of declared bounds (a strongly connected component of more than one bound, or a bound that contains itself) loses all its declared children, which are then inherited. Converting from the leaves up, a bound whose converted subtree would be deeper than the nesting bound of 32 also loses its declared children, so no converted bound nests deeper than 32. Both rules depend only on the bounds, never on where an expression reaches them. Selecting a declared field reads that field's bound, and selecting any other field reads the element bound. An index whose key is a string literal naming a declared field reads that field's bound; any other index into a value with declared fields, and the element of a join of values of which either declares fields, inherit from the parent instead, because the element bound need not cover the declared fields. The environment must be a plain `dict` with string names. Each entry the expression references must be exactly a `Variable` whose type is a closed structure of exact instances (not subclasses) of the CEL type classes of this section, with record fields in a plain `dict` of string names and `RecordField` values whose `required` is a bool, no nullable type wrapping another nullable type, and cycles allowed, and each bound is read once, before anything is derived from it: it must be exactly a `ValueBound` (not a subclass), with non-negative integer sizes, fields held in a plain `dict` with string names and value-bound children, and a declared child must be no larger than its parent in bytes and value count and within the data bounds' element count and string length. An estimate that reaches a bound breaking this raises `BoundError`, an error of the environment's builder rather than a diagnostic, because inheriting from such a parent could lower the estimate. The whole bound and type of every variable the expression references are examined; the entries of variables it does not reference are not. The environment is plain data from kora's own builder, not author input, so `BoundError` reports a builder defect. The default bound of a variable uses 4,096 canonical bytes and 256 values, with the independent per-container and per-string ceilings in the data-bound table. The aggregate limits can dominate those ceilings.
- **Joined element bounds.** The element bound of a list literal, a concatenation or a conditional joins its operands' element bounds, using the inherited bound for an operand that declares none or that declares fields, whatever the static type. A range of type `dyn` iterates over the join of its element bound (the inherited bound when it declares fields) and a key bound, a map or record over its keys. A join keeps a type only when both sides have the same type object or the same primitive kind; otherwise it is `dyn`, so a join never compares type structures. The schema-driven checker therefore builds one type object per schema node, so that equal types from one closure are identical.
- **Canonical result sizes.** Empty list/map literals and zero-length comprehension results occupy two canonical bytes. A `string()` conversion allows up to six canonical bytes per input byte, covering escaped controls and replacement characters from invalid UTF-8, including a `dyn` operand, plus the scalar allowance. These sizes propagate through collections, concatenation, comprehensions, comparisons and pattern subjects.
- **Affine results.** A size computed inside a comprehension is affine in the iteration variable's size, so the total over the range is bounded by the range's own total rather than by the element count times the largest element: `l.map(x, x)` is bounded by the size of `l`.
- **Typed charges.** A charge follows the value's type: `+` on two typed numbers is a node visit, and on anything that may be a string, bytes, list or `dyn` value it is a concatenation. Unless both sides are typed strings, bytes or lists, the result of `+` is charged at least the size of a scalar, so a `dyn` operand never makes an addition look cheaper than its typed form.
- **Arithmetic.** The estimate is one post-order pass in exact integer arithmetic. Each bound object of the environment is converted once per estimate, iteratively, so the estimate's work is linear in the expression and in the number of bound objects and declared fields of the environment. Memory is checked before steps, so an expression over both reports the memory dimension. Memory adds `memory_base` exactly once, then applies `memory_byte` to converted bound data once and every value an operation allocates; a reference to a variable or to part of a value allocates nothing. The provisional `memory_base` is 0 and `memory_byte` is 1; neither has been fitted to resident-memory measurements.

**Provisional budget and regression target.**

- The steps budget is **100,000,000,000,000 logical charge units**, ten times the previous provisional budget. All fourteen time weights remain unchanged. The memory budget remains **16 MiB (16,777,216 logical estimated bytes)** with the provisional base and multiplier above. Neither dimension is a calibrated bound on elapsed time or RSS.
- The observed coarse regression target is **100 ms per evaluation interval** on the reference CI runner. The [compact protocol](../research/cel-cost-calibration-2026-10-22.md#current-compact-regression-protocol) measures ten fixed cases, each in a fresh child, with one cold cache miss and three warm cache hits through `Evaluator.evaluate`. Each of the forty intervals must be at most 100,000,000 ns. The interval includes binding, queueing, cache-miss compilation when needed, native execution and result delivery; expression checking and evaluator startup precede it, and result assertions follow it. This is neither an interruption deadline nor a bound for every admitted expression.
- The selected initial data bounds remain 4,096 canonical bytes and 256 values in both original and converted aggregates. The [qualified smaller-bound result](../research/cel-cost-calibration-2026-10-22.md#qualified-smaller-bound-result) retains the earlier conversion evidence. Before the first stored estimate, these settings remain provisional under the initial `kora.execution.v1` exception. That exception ends at the first stored estimate; later data-bound or cost-rule changes require a new execution semantics identity, without implying a migration mechanism.
- The golden corpus pins provisional estimate outputs. The compact regression and golden checks replace exhaustive time/memory fitting and crash-margin calibration as the pre-consumer validation gate under [issue #167](https://github.com/realkarych/kora/issues/167). Schema-driven checking/projections, correction of native minimum-int unary negation and real consumer integration remain prerequisites for author-expression consumers. The independently tested evaluator infrastructure of [issue #166](https://github.com/realkarych/kora/issues/166) does not activate those consumers.
- The weights and provisional regex tables belong to `kora.execution.v1`, under the pin rule at the start of section 7. Their values have not been established by full numerical calibration.

The logical memory dimension rejects compounding concatenation inside sequential `map` operations. Four of them, each joining sixteen copies of a 2 KiB element of a list with `maxItems: 1`, denote 128 MiB while fitting every static limit and the initial input-data bounds.

**Evaluation.** It runs in the API and scheduler processes, inside the existing short transactions ([scheduler.md](scheduler.md) section 2).

- **The evaluation thread.** Each process has one dedicated long-lived evaluation thread with a stack of at least 1 MiB, shared by every in-process standalone instance the process starts. The process owns it: it is a daemon thread released when the process exits, and stopping one instance rejects only that instance's queued jobs. Startup sets the thread stack size, creates the thread and restores the previous setting, so no other thread is affected.
  - It runs one job at a time.
  - Evaluations requested from inside a transaction take priority over server-side compile jobs. Compile work is submitted one expression at a time, so an evaluation waits for at most one compile, whose length and pattern size the static limits bound.
  - At shutdown, queued jobs are rejected and the job in flight finishes, since it holds the GIL and cannot be interrupted. The logical budget and observed 100 ms target do not bound that wait; an external supervisor's kill remains the hard limit. Stopping an embedded standalone (`kora test`, `run_scenario`, `kora_test`) waits for that job the same way and never exits the host process. A cancelled caller discards its result.
- **The compiled-program cache.** The process worker owns a least-recently-used cache keyed by the canonical rendering digest and expression YAML path. Defaults are 128 entries and 131,072 summed rendering characters. A hit verifies the stored rendering; a miss reparses the canonical rendering before `Binding.compile(checked)`. Validation compiles return a small outcome without entering the cache. A compile on a miss is charged to the allowance.
- **No interruption.** The binding holds the GIL while it parses or evaluates and cannot be interrupted (probe). Static admission and the compact regression reduce exposure but do not guarantee a bound on GIL hold, API response time or scheduler transaction duration. An admitted expression can still stall or crash the shared process.

**Evaluator infrastructure admission.** Each process accepts at most 32 evaluation jobs and 32 validation compiles, including in-flight work and completed results awaiting delivery. A batch contains at most 16 evaluations. The shared outstanding charge is at most 8 MiB; it accounts for detached converted values, renderings, bounded metadata and result retention. Result growth that cannot fit the shared charge returns the operational `busy` refusal. This charge is an ownership bound, not a measurement of native memory. At most 128 handles are admitted. Pending lifecycle reports also consume new-handle admission; they are bounded by 256 because each handle can produce one timeout and one completed-close report. YAML paths are bounded to 1,024 characters and event identifiers to 128; checked renderings to 65,536. These internal limits precede the first product consumer and are configurable through `Settings`.

Before conversion, an iterative walk checks exact JSON types, finite numbers, Unicode scalar values, 32 levels of nesting, 1,000 entries per list or map, 16 KiB UTF-8 strings, 4 KiB of canonical JSON and 256 values. Conversion copies only referenced variables; both original and converted representations must fit the aggregate byte and value bounds. Integer schema fields bind as signed 64-bit integers, accepting integral JSON floats; number and schema-less numeric values bind as finite doubles. Missing `outputs` binds as an empty map; another missing referenced projection is `projection_unavailable`. The caller supplies the saved, validated schema types and projection bounds; the declared value bounds come with the checked expression. `Record.extra` carries the type of undeclared keys (section 7.4), and conversion refuses a value whose JSON kind differs from its declared type with `invalid_value`.

An `Evaluator` belongs to its creating event loop. Closing a handle rejects its queued jobs, waits for its in-flight and undelivered work, and leaves other handles and the shared worker usable. Its default one-second close deadline returns `close_timed_out`; it cannot interrupt native work. A cancelled queued job never executes; an in-flight cancellation discards the result. Closed loops release their retained jobs; stopped open loops remain charged until delivery or close. An unmapped worker fault closes admission permanently, settles waiting callers and releases the cache. The worker starts in an empty context, and the process owner loads the binding without the creating request's context.

**Evaluation allowance.** The installation limits the sum of logical charges evaluated in one scheduler transaction; the default is 100,000,000,000,000 charge units, and it may never be set below one expression's steps budget. This is a work-accounting threshold, not a wall-time bound. Its consumer integration remains pending.

- **Scope.** The allowance applies to every scheduler transaction that evaluates CEL: the branch pass, retry handling, wake-ups and prepared-observation apply.
- **Progress.** Each transaction evaluates at least one expression. When the allowance is used up, the pass commits what it has decided, leaves `needs_scheduling` set for the rest and moves on, rotating across runs and namespaces.
- **Events.** One inbound event is matched against open waits in batches, per run and under that run's lock.
- **Order, not results.** The allowance changes when work happens, never how an expression evaluates. Deferred work may cross a wait's expiry, which the existing expiry rules then decide.
- **Configuration.** It is installation configuration rather than execution semantics.
- **API evaluations.** An evaluation in the API (`approve_when`) is outside the allowance and is admitted under the per-expression logical budget; that budget establishes no response deadline.

### 7.7 Evaluation errors

**Error kinds.**

- From the binding's documented exception classes:

  | binding exception | error kind |
  |---|---|
  | `KeyError` | `missing_field` |
  | `TypeError` | `no_overload`, which includes selection on `null` |
  | `IndexError` | `index_out_of_range` |
  | `ZeroDivisionError` | `division_by_zero` |
  | `OverflowError` | `overflow` |

- Raised by kora itself:
  - `bound_exceeded`;
  - `number_out_of_range`;
  - `invalid_value` for values outside exact JSON types or Unicode scalar strings, or whose JSON kind does not match the declared CEL type (section 7.4);
  - `wrong_result_type`: anything that is not exactly `bool`, or `string` for reasons; Python truthiness never applies;
  - `projection_unavailable`.
- `internal_fault`, logged at ERROR, covers everything the checker should already have prevented: a `ValueError` or `RuntimeError` at execution, and the binding's parser panic.

**Catching.** Exceptions are caught narrowly, around the binding call only; kora's own conversion code runs outside that handler.

- The parser panic class is a `BaseException`. It is resolved exactly when the process starts, and a process that cannot resolve it refuses to start.
- `CancelledError`, `KeyboardInterrupt` and `SystemExit` are never caught.

**The error record.** A recorded error has these fields:

- **`kind`**: one of the kinds above.
- **`field`**: the YAML path of the expression.
- **`offset`**: set only when kora itself locates the fault, null for kinds the binding raises.
- **`event_id`**: for a `match` error, the inbound event's identifier.
- **`observed_at`**: for an `until` error, the time of the observation.
- **`at`**: when the error was recorded, from the injected clock.
- **`fault`**: null except for `internal_fault`, where it is one of the closed classes `value_error`, `runtime_error`, `panic` or `unmapped`; never a native message or an arbitrary exception type.

A node that fails because of an evaluation error fails with `config_error`, reason `cel_evaluation_error`, and carries this record in its details.

**Meaning per kind**, beyond section 7.1:

- **`approve_when`** is evaluated only after the response has passed the staleness, commit and digest, and approver checks (section 4.2). A response refused by those checks is never evaluated. On an error:
  - the response is kept with the request, and the request becomes `failed` ([data-model.md](data-model.md) section 4);
  - `approval.responded` records `applied: true` and the error record;
  - the channel is closed with `notify(resolved)` linking the canonical status, which shows `failed`;
  - the node fails with `config_error`.

  A `failed` request never counts as approval, and a later response is recorded with `applied: false`. A direct API response returns HTTP 200 with the request's status.
- **`retry.predicate`** is evaluated only when the class is retryable, listed in `retry_on` and attempts remain. It is never evaluated for `infrastructure` or `provider_limited`, which do not consume attempts. Every `outputs.*` read needs a `has()` guard (section 7.4). An error is recorded on the node's failure event.
- **`match`.** Each event and wait pair is evaluated at most once, whether the result is false or an error. The evaluating transaction inserts an `event_evaluation` row for the pair ([data-model.md](data-model.md) section 2), together with any consumption. An event is eligible for a wait under the existing matching rules, and an open wait with an eligible event it has not evaluated keeps its run's `needs_scheduling` set. A `match` error is written in that pass-6 transaction, after the exact open wait is verified under the run lock.
- **`until`.** An error counts as a successful observation for dispatch: the token is consumed, the next poll keeps the normal interval and the integration is not degraded ([scheduler.md](scheduler.md) section 16). A start binding consumes no occurrence or cursor and writes no `trigger.*` event. An `until` error is written only from the current claim.
- **Recording external errors.** The last `match` or `until` error is stored on the wait or the start binding and is visible in the API and the console.
- **Unavailable inputs.** `projection_unavailable` fails the owning node with `config_error` for every kind, waits included, because an expired or missing projection never becomes available again. The one exception is `retry.predicate`, where it means no retry and the node keeps its original class, like any predicate error.

**Safe content.** Error records stored on a node failure, a wait, a start binding, an approval request or an event, and every error the API returns, carry only the fields above. The evaluator returns frozen error values across the thread boundary; operational refusals are raised on the caller loop with both cause and context cleared, including when called from inside another exception handler. They never carry expression text, bound values, binding messages, chained exceptions or panic payloads. Diagnostics follow the same rule ([observability.md](observability.md) section 5), including at DEBUG. The evaluating component emits them, the scheduler or the API, and deduplicates them on the wait, binding or node, the field and the kind.

### 7.8 Conformance

The cel-spec conformance suite runs in CI against the binding, through a hand-written reader of the textproto subset its selected files use, with no protobuf dependency.

- **Vendoring.** The files are vendored under `tests/` at a pinned cel-spec commit, with the upstream licence. Their source URL, full commit SHA and digest are recorded in `knowledge/research/` when the suite is added.
- **Selection.** Explicit include and exclude lists name the files. Excluded are `dynamic` and `namespace` (message types and containers), `macros2` (two-variable comprehensions), `proto2`, `proto3`, `wrappers` and `enums` (message types), `optionals`, `unknowns`, `type_deduction` and every extension file. Within the included files, explicit entries name each case, or a whole section, that kora rejects, with the feature it needs and the diagnostic reason kora gives: message types and qualified type names, backtick-quoted fields, unescaped control characters in literals, the narrowed standard functions and functions outside the standard library.
- **Integrity.** The files are byte-identical to the upstream commit, and a test checks them and the licence against a manifest of SHA-256 digests.
- **Known deviations.** Every case `kora.cel` accepts runs through the binding with its bindings (a `google.protobuf.Duration` binding is decoded from its seconds and nanoseconds to microseconds, the binding's resolution), and its typed result is compared with the expected value. A case where binding 0.10.0 disagrees with cel-spec is listed in `tests/cel/conformance/deviations.py` with its category and the binding's exact outcome, so the test fails when a deviation disappears or changes; a deviation is never skipped. The comparison binds `int` and `uint` alike, because the binding returns both as Python integers, accepts any error where cel-spec expects one, checks an expected timestamp by type only, does not check the sign of a zero double, compares a type value by its name, which is how the binding returns it, looks up an expected map key by Python equality, so the key's kind among `bool`, `int` and `uint` is not checked, and cuts expected and bound durations to microseconds. Measured under [issue #165](https://github.com/realkarych/kora/issues/165), 37 of 1,005 cases deviate:
  - **string size in bytes**: `size()` of a string counts UTF-8 bytes;
  - **mixed numeric keys**: `int`, `uint` and `double` values of equal value are not interchangeable as keys: an `int` or `uint` key misses the entry of the other kind, a `double` map key, list index or `in` test raises a type error, and maps with such keys compare unequal;
  - **error absorption**: `all()` returns the first error instead of a later `false`;
  - **negated boolean**: `-false` is `true` instead of an error; on `dyn` data that is a silently wrong value, a case for the schema-driven checker;
  - **wrapping negation**: `-(-9223372036854775808)` wraps instead of overflowing;
  - **invalid UTF-8**: `string()` of invalid UTF-8 bytes substitutes the replacement character instead of failing;
  - **time ranges**: `duration()` of a string beyond cel-spec's range of a signed 64-bit count of nanoseconds (about ±292 years) clamps to that bound instead of failing, so `duration('9300000000s') == duration('9400000000s')` is `true`; sums of durations and differences of timestamps exceed the bound unchecked; a timestamp string whose UTC offset carries it outside the years 1 to 9999 is accepted, while other timestamps beyond that range are refused;
  - **time formatting**: `string()` of a timestamp or a duration uses another format than cel-spec;
  - **duration accessors**: `getMilliseconds()` of a duration returns the whole duration in milliseconds rather than its millisecond component;
  - **missing overloads**: `int(timestamp)`, `timestamp(int)` and `duration + timestamp` fail at evaluation; the conformance cases on `timestamp(timestamp(…))` and on comparing a timestamp with `null` deviate because they build their timestamps with `timestamp(int)`;
  - **qualified names**: variables named with dots and qualified type names such as `google.protobuf.Timestamp` are not resolved;
  - **repeated map keys**: a map literal with a repeated key keeps the last value instead of failing.

  Iterating over a map visits its keys in an order that changes between evaluations, so `map()` and `filter()` over a map return their elements in an arbitrary order. The timestamp accessors read the calendar fields in the UTC offset written in the timestamp string rather than in UTC, so `timestamp('2020-12-31T23:00:00-05:00').getFullYear()` is 2020 for an instant in 2021; no included conformance case covers it. The time ranges, time formatting, duration and timestamp accessors, missing overloads, repeated map keys and iteration order are recorded for the schema-driven checker and the evaluator ([issue #166](https://github.com/realkarych/kora/issues/166)).
- **Reader and parser.** The reader has two layers: a closed generic textproto syntax that parses whole files, and a typed case model that fails on any field outside the subset the selected files use. Every included case also runs through kora's parser and canonical rendering: a case without an entry must be accepted, a listed case must give exactly its listed reason, and every entry must match a case.

### 7.9 Future contracts

The parser, checker, cost estimate and textproto reader are unit-tested, as AGENTS.md allows for parsers, checkers and arithmetic. Every test that touches the metadata database runs on PostgreSQL and SQLite. All are deterministic; anything that measures time or memory is a measurement below, not a test.

**Status.** The parser, limits, rendering, paths, pattern subset, cost estimate and conformance reader landed under [issue #161](https://github.com/realkarych/kora/issues/161) in `kora.cel` and `tests/cel/`. The binding, its loader and every comparison of `kora.cel` with the binding landed under [issue #165](https://github.com/realkarych/kora/issues/165) in `kora.cel_eval.binding`, `tests/cel_eval/` and `tests/cel/`, and their rows below are active, the two entry-point tests in their loader legs only. These tests are active in full: `test_cel_rejects_expression_over_depth_limit` (its 4,096-code-point leg is `test_cel_prefix_floods_stop_at_the_depth_limit`), `test_cel_rejects_comprehension_over_limits`, `test_cel_paths_obey_limits_and_never_reach_binding` (its binding leg is `test_cel_paths_never_load_the_binding`, in a fresh process) and the rows added below for `kora.cel` other than the two binding rows. These are active in part:

- `test_cel_canonical_rendering_matches_author_text`: the re-parse leg over the corpus, the included conformance cases and expressions generated from a fixed seed; its binding leg is `test_cel_canonical_rendering_matches_author_text_in_binding`.
- `test_cel_rejects_expression_over_length_limit`: the expression leg; the schema-validation leg waits for the `ir` validator.
- `test_cel_rejects_non_literal_regex_pattern`: the literal rule; the backreference leg at server-side validation waits for the API.
- `test_cel_regex_compile_cost_bounded`: the nesting and cap legs in `kora.cel`; the `kora compile` and `/validate` legs and compiling each distinct pattern once wait for the DSL and the API.

The evaluator conversion, native error mapping, exact result types, thread priority/cancellation/close, bounded cache, safe diagnostics and resource-lifetime legs are active under [issue #166](https://github.com/realkarych/kora/issues/166). Under [issue #167](https://github.com/realkarych/kora/issues/167), `tests/cel/test_cel_cost_regression.py` adds the active provisional golden corpus and budget/specification consistency checks, and `tests/measurements/test_cel_cost_regression.py` adds the compact 100 ms measurement gate. They establish no full calibration or crash margin. The projection, API, scheduler and DSL legs stay `pending implementation` with the work items that own them in the implementation plan.

The schema-driven checker of section 7.4 landed under [issue #337](https://github.com/realkarych/kora/issues/337) (`kora.cel.schema`, the schema-to-CEL-type mapping) and [issue #330](https://github.com/realkarych/kora/issues/330) (`kora.cel.checker` and `check_typed`), applied to an environment its caller builds. These rows are active: `test_cel_rejects_unknown_field`, `test_cel_rejects_non_boolean_approve_when`, `test_cel_rejects_unknown_enum_literal`, `test_cel_rejects_mixed_int_double_equality`, `test_cel_rejects_unguarded_optional_field`, `test_cel_rejects_string_size`, `test_cel_recursive_closure_checked_in_bounded_time` and the rows added below for the schema mapping and the checker. The checker legs of `test_cel_converts_json_numbers_to_schema_types`, `test_cel_dyn_selection_needs_no_guard` and `test_cel_dyn_result_requires_exact_bool` are in `tests/cel/test_cel_checker.py` under the same names, beside their evaluator legs in `tests/cel_eval/`. The untyped `check` remains the syntax and cost entry of binding agreement and conformance; every author-expression consumer uses `check_typed`. Building environments from a graph document (the type closure at each call site, StructuredOutput effective views, `AgentSession` summary fields and the `inputs`, `outputs`, `params` and `response` roots), `test_cel_child_type_keeps_parent_cel_types` and `test_cel_examples_pass_checker` stay with work item 3 and the remaining #138 contracts; projections and the checker leg of `test_cel_limits_at_boundary_compile_in_binding` stay pending. Schema-tightened estimate bounds and their binding check landed under [issue #374](https://github.com/realkarych/kora/issues/374): `test_cel_cost_estimate_rejects_compounding_concatenation` is active in full, its `maxItems` leg building the variable from a schema, and the rows added below for schema bounds are active.

| Test | Required result |
|---|---|
| `test_cel_rejects_unknown_field` | `response.decison`, a dot selection of an undeclared field on an open object and a dot selection on a schema-typed map are `IR011` at the right offset |
| `test_cel_rejects_non_boolean_approve_when` | `approve_when: "response.decision"` is `IR011` |
| `test_cel_converts_json_numbers_to_schema_types` | stored `100` under `number` binds as `double` and the FR-2.5 example evaluates; under `integer` the checker rejects `int * double`; schema-less numbers bind as `double` |
| `test_cel_rejects_expression_over_depth_limit` | 33 levels of each node kind (`+`, `&&` and selection chains, `?:`, `!`, `-`, parentheses, call arguments, list literals) are `IR027`. 4,096 code points of prefix or conditional nesting are `IR027` without a recursion error |
| `test_cel_rejects_expression_over_length_limit` | 4,097 code points are `IR027`, also when found by schema validation |
| `test_cel_rejects_comprehension_over_limits` | a fifth macro, and a macro in the body of a macro in the body of a macro, are `IR027`; four chained macros are accepted |
| `test_cel_limits_at_boundary_compile_in_binding` | the rendering of the depth-32 expression of every nesting shape, parenthesized unary chains included, compiles in the binding, and evaluating it raises no panic; the checker leg, which needs a typed environment for every shape, is pending |
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
| `test_cel_binding_exceptions_map_to_error_kinds` | each documented binding exception maps to its kind, with a null `offset` |
| `test_cel_maps_only_documented_native_errors` | each native exception maps to its documented kind and fault, the parser panic to `internal_fault` with the fault `panic`, and the error carries neither the native message nor its cause |
| `test_cel_cancellation_propagates_through_evaluation` | `asyncio.CancelledError`, `KeyboardInterrupt` and `SystemExit` raised during evaluation still propagate |
| `test_cel_persisted_errors_exclude_values` | no stored, returned or logged error carries expression text, values or binding messages. This includes binding messages that contain payload keys, DEBUG output and exception chains, in every sink: wait, binding, approval, event, API, CLI, MCP and logs |
| `test_cel_evaluation_error_reads_parity` | the error record appears in node, trigger-binding and approval reads, the Python client, CLI `--json`, MCP output and the TypeScript client |
| `test_cel_evaluating_entry_points_refuse_without_binding` | the API, scheduler and standalone refuse to start without `kora[cel]`, and `kora test` and `kora_test` return a structured error naming the extra, through an injected binding loader; `kora doctor` reports the missing extra |
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

Before the first author-expression consumer, the compact regression and golden checks (#167), schema-driven checking/projections and correction of native minimum-int unary negation (including dynamically computed minimum integers) must land, together with that consumer's real integration evidence. This infrastructure retains the documented binding deviations; it does not claim that the native wrapping negation has been corrected. Persistence, scheduler/API decisions, surface parity and consumer diagnostic deduplication remain pending their implementing work items. The 1,000-node and five-second scheduling requirements, fairness and transaction measurements remain mandatory when those consumers activate. The existing `cel` diagnostic activation row and future API/scheduler error ownership in [observability.md](observability.md#7-component-coverage-and-activation) are unchanged. The local/CI compact and lifetime commands are in [development](../../docs/development.md), with lifetime evidence in the [evaluator research record](../research/cel-evaluator-lifetime-2026-10-21.md).

### 7.10 Prompt templates

Prompt templates (`prompt`, `system_prompt_append`, `cmd` elements) are **Jinja2 in a sandboxed environment** (DD-11): the full template language, the variable names of section 7.1 (the CEL projections of section 7.5 do not apply to templates), no filesystem loaders, no imports or includes, no access to Python attributes beyond the bound data. Templates are rendered once when the nonsecret execution request is sealed before preparation/execution effects; the attempt owns the saved rendered content and availability state. Recovery reads that content and never re-renders over current data (ADR-0018). Run parameters that arrive from a webhook may be used in prompts but never inside `cmd` elements or `workspace.rev` (`IR016`).

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

Validation emits [shared diagnostics](diagnostics.md) and collects every independent fault within accepted request bounds. Invalid prerequisites prevent only their dependent semantic checks. Collection bounds never turn omitted errors into success, and a provider diagnostic-limit failure is incomplete validation, never a successful or warning-only result. The shared contract owns safe metadata, source coordinates, code-point offsets, deterministic ordering and separate omitted counts; the HTTP and incomplete-validation mappings remain with [API conventions](api-and-agent-surface.md#validation-envelopes). Accepted IR018/IR022 and installation-feature refusals remain document diagnostics, not dependency outages. Provider `diagnostics_limit` and aggregate count overflow prevent a normal collection or deployment.

Schema validation first, then semantic checks, each with a stable code and a safe segmented document path when the field is known (FR-0.4): `IR001` acyclic graph (including `depends_on`), `IR002` every `from` resolves, `IR003` output/input type assignable, fixed implicit output roles (section 3.1), and an explicit response output schema structurally equal to the effective form (section 4.2), `IR004` fan-out `over` names a list-typed input and every `collect` names a body output, `IR005` `binds` paths resolve to a `VcsRef` and digest-bearing artifacts, `IR006` `$previous_attempt` only inside a retried subgraph, `IR007` secret aliases resolve, fall inside the namespace's allowed reference prefixes and no value-like strings appear in `env`, `IR008` harness cannot honour a non-security field (warning; unsupported security controls are errors), `IR009` runtime lacks a requested capability such as `enforces_network_policy` or `supports_resume`, or the node's runtime and VCS declare no common plan executor ([protocols.md](protocols.md) section 4) (warning only for optional non-security capabilities; unsupported security controls and no common plan executor are errors), `IR010` branch targets exist in their graph/body scope, each has the owning branch directly in `depends_on`, no target has multiple distinct branch owners, neither branches nor ordinary direct targets set `attempt_invariant: true`, and `$fail_attempt` appears only inside a subgraph, `IR011` a human-input predicate is explicit or has a valid default (section 4.2), CEL expressions parse and type-check against the declared schemas, and paths parse: unsupported syntax or functions, unknown identifiers and fields, shadowing comprehension variables, enum literals outside the allowed values, operand overloads, unguarded optional reads, `size()` on strings or `dyn` values, non-literal or uncompilable `matches()` patterns, wrong result types, a canonical rendering that does not parse back to the same tree and expressions the binding cannot compile at server-side validation (section 7); path resolution keeps its own codes, `IR012` Jinja2 templates parse in the sandbox, `IR013` `tool_policy` patterns are valid under the grammar and bounds of [protocols.md](protocols.md) section 12, in the node's and in the namespace's policy, and none has the exact tool name `StructuredOutput`, the harness output channel (a wildcard pattern stays valid and never applies to it), `IR014` sensor `config` fails the sensor's `config_schema`, `IR015` an agent node with `secret_env` lacks a non-empty `network_policy` or runs on a runtime declaring `enforces_network_policy: false`, an MCP server with `secret_env` lacks a non-empty own `network_policy` or runs on a runtime declaring `isolates_tool_secrets: false` or `enforces_network_policy: false`, unless the namespace sets `allow_unenforced_agent_secrets`; a command node with secrets on a non-enforcing runtime while the namespace sets `require_network_enforcement` (ADR-0009 as amended 2026-10-18), `IR016` run parameters appear in a `cmd` element or `workspace.rev` of a graph with a webhook trigger, `IR017` `trust_repo_config: true` on a node whose namespace policy does not allow it (current contract [ADR-0009](../../docs/decisions/0009-target-repository-trust-boundary.md)), `IR018` an agent node's `harness` names no harness adapter registered with the installation, which in MVP includes every reserved `acp:<agent>` name (an error, not a warning: unlike `IR008` and `IR009`, the node could not run at all; ADR-0004), `IR019` a `tool_policy` pattern from the namespace policy names a call kind the node's harness adapter does not intercept (`HarnessCapabilities.policy_coverage`, [protocols.md](protocols.md) section 12), an error because the platform team's rule would otherwise run unenforced; the same pattern from the node alone is error `IR028` (ADR-0009, amended 2026-10-23), `IR020` `graph.target.key` names a run parameter that is not a string in `graph.params`, or a node, an output or a path that does not resolve to a string, checked against the output's declared schema like `IR011` (FR-6.11, ADR-0014), `IR021` the selected runtime lacks `protected_execution` while namespace policy requires it (ADR-0016); local remains valid for trusted development/tests without that requirement. `IR022` rejects missing/inconsistent type closure, unsupported schema dialect or behavior, invalid schema references and incompatible author-side contract declarations (section 2.1). `IR023` rejects an `independent_of` entry that is not a declared input name of that node. `IR024` rejects a command node with `resources` on a runtime that does not declare `protected_execution` when the namespace sets `require_protected_resources` (issue #45). `IR026` rejects an agent node whose `env` or `secret_env` sets a name or prefix in its harness adapter's `reserved_env` ([protocols.md](protocols.md) section 3), such as the `claude` adapter's `HOME`, `ANTHROPIC_*`, `NODE_*` or `HTTPS_PROXY`, or a variable the runtime assigns to the candidate domain, because the adapter or the runtime owns those variables and they change the harness's destination, its TLS trust or what it and its shell load at start-up (issue #125). `IR027` rejects a CEL expression or path over a static limit (4,096 code points, depth 32, four comprehension macros nested at most two deep), a `matches()` pattern over its size-estimate cap or its group-nesting limit, an expression over the cost budget, or static trigger `params` over the CEL data bounds, including a `maxLength` violation found by schema validation (section 7).

`IR028` rejects a well-formed capability/configuration the installation does not implement, including an unenforceable node-only security control. Its recovery hint is to remove the unsupported configuration or use an installation that implements it; it never suggests disabling a required protection. [First-slice.md](first-slice.md#refusal-contract) defines the refused kinds/fields, exact empty/absent values, topology, resolved-default checks and narrow ADR-0009 exception. The shared validator uses the trusted installation feature inventory for both validate and deploy, reports the unavailable capability at its safe segmented document path under [the shared diagnostic contract](diagnostics.md) and retains more specific syntax/type/security codes. It traverses operational typed fields, not arbitrary metadata or payload strings. Diagnostics preserve the shared safe-location fallback, source metadata, collection bounds and omitted counts; a deploy refusal is `API003` with `details.errors[]`. Full-language schema/CEL checking remains independent of first-slice executability, including dormant predicates and every worked example.

`test_slice_one_rejects_unsupported_topology` exercises wider/parallel graphs and confirms identical `IR028` locations through validate/deploy. Configuration/default tests cover budgets (including zero), retries, node resources, repository trust and unsupported providers/security settings before secret resolution or other side effects. These validator tests are pending work item 3; defining the error here implements no validator.

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
