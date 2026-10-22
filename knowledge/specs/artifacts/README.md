# Built-in artifact revisions

The project owner approved the VcsRef revision-1 contract and implementation plan under [issue #208](https://github.com/realkarych/kora/issues/208#issuecomment-6353268608) on 2026-10-23. This directory publishes authoritative contracts under [ADR-0018](../../../docs/decisions/0018-immutable-attempt-history.md), not evidence of an implemented provider. [Issue #209](https://github.com/realkarych/kora/issues/209) owns the VcsRef models and boundary validation. [Issue #138](https://github.com/realkarych/kora/issues/138) remains open for CommandResult, AgentSession and StructuredOutput; this publication does not define their revision 1 or ReviewBundle.

## 1. Revision identity

| Type | Revision | Schema | Descriptor |
|---|---|---|---|
| VcsRef | `1` | [VcsRef.schema.json](VcsRef.schema.json), `$id: urn:kora:artifact:VcsRef` | [VcsRef.revision-1.json](VcsRef.revision-1.json) |

The schema is draft 2020-12, with no parent. The manifest's `schema_sha256` identifies the exact UTF-8 schema file bytes, including its final newline. The manifest and the semantics specified here are immutable for this revision. Adding another type cannot change this per-type manifest. A future incompatible definition requires a new revision and behavior identity. Deployment expands the schema and exact behavior descriptors by value into the saved closure; the manifest's schema URN is resolved only from that closure, never through a network lookup. Unsupported or mismatched bindings fail before the affected operation, following [IR section 2.1](../ir.md#21-authoritative-type-contracts-adr-0018).

## 2. VcsRef publication facts

The closed object has required fields `repo`, `branch`, `commit`, `base_commit`, `changed_paths`, `diff_stat` and `pushed_by`. It has no extension map or mutable status field.

| Field | Meaning |
|---|---|
| `repo` | Logical repository resolved through the saved trusted deployment binding; never a candidate URL or transport authority |
| `branch` | Published branch at that trusted destination |
| `commit`, `base_commit` | Exact immutable native revision identifiers, nonempty and at most 255 Unicode scalar values; core imposes no Git hash syntax |
| `changed_paths` | Unique relative POSIX paths from the trusted inspection's union across newly published history, including deletions, reverted intermediate changes and changes against every merge parent; not merely the final-tree diff |
| `diff_stat` | Trusted inspection's display text; not independent verification evidence |
| `pushed_by` | Recorded authenticated publishing principal, including the binding-defined principal for explicit trusted credential-free local publication |

Repository, branch, revision and principal identifiers contain no U+0000–U+001F or U+007F. Paths contain neither those controls nor backslashes, absolute roots, empty segments, `.` or `..` segments. There are at most 10,000 paths and their combined UTF-8 length, without separators or JSON escaping, is at most 524,288 bytes. Providers preserve the complete inspected set; they never truncate it to fit. A real nonempty change is established by trusted inspection, not by accepting nonempty display strings or a claimed path list. Checkout recomputes the provider's protected-path evidence against the recorded base under [protocols section 4](../protocols.md#4-vcs-fr-52).

`PrincipalRef` is an embedded closed record with required `provider_instance`, `stable_id`, `kind` and `display_name`. The three free-text fields are nonempty and at most 255 Unicode scalar values, matching the persisted automation-principal columns. `kind` is `app`, `bot_user`, `service_user` or `user`. Identity comparison uses `(provider_instance, stable_id)`, never display name or kind; recorded display values remain immutable snapshots. The public model belongs in `kora.ir` and protocol consumers reuse it, preserving [ADR-0026](../../../docs/decisions/0026-package-layers.md).

Optional fields are `url` (change request), `commit_url`, `compare_url` and `opened_by` (`PrincipalRef`). `url` and `opened_by` occur together. Commit and comparison URLs may occur independently. These are absolute URLs with a lowercase `http` or `https` scheme, nonempty authority and no userinfo; semantic URL validation also rejects malformed authorities and control characters. No URL is dereferenced during validation. URLs are untrusted display/link values, not repository identity, checkout instructions, authorization or observations of current status. Omission means no recorded fact; explicit null is invalid. A local push-only result omits code-host fields. The [#140 publication scope](https://github.com/realkarych/kora/issues/140#issuecomment-6319392587) retains authorization, principal recording and every late matching/closure check.

## 3. Validation descriptor

`kora.vcs-ref-validation.v1` composes the generic `kora.json-schema-2020-12.v1` structural validator with these fixed boundary rules:

1. Raw JSON is at most 1,048,576 bytes. Enforce the cumulative bound before decoding, including across multipart/chunked uploads; oversized declared totals are refused before receiving payload bytes.
2. A bounded lexical preflight counts object/array nesting outside strings, respecting escapes. Root container depth is 1; refuse depth above 16 before invoking a general decoder. Decode strict UTF-8 JSON, refusing duplicate object names, malformed JSON, lone surrogates, non-JSON/nonfinite values and parser-limit failures. Every string is a sequence of Unicode scalar values without NUL.
3. Validate the closed schema without coercion, default insertion, field dropping or normalization. JSON Schema establishes cardinality, uniqueness, field shape and character limits; it does not establish the following byte predicates or provenance.
4. Check the aggregate path bound as `sum(len(path.encode("utf-8"))) <= 524288`, with no normalization, and the semantic URL predicate. Canonical payload bytes defined below are at most 1,048,576 bytes.

The full behavior differs from structural schema acceptance: a schema-valid value can still fail a byte, Unicode or URL predicate. Constants and this exact semantics are part of the saved behavior binding, not mutable installation defaults. In-process typed validation enforces the same payload domain; only a raw-byte boundary can detect duplicate JSON members. Validation errors use the shared `ContractViolation` shape of #135 and meaningful known JSON paths, with content-free messages. Payloads, URLs, unknown key text and raw decoder/Pydantic exception messages never become diagnostics. The API uses its existing structured `API003` upload-refusal semantics. Validation, hashing, canonical comparison and summary work occur outside run locks.

Shape validity, a matching digest and a saved summary authenticate no publication fact. Trusted consumers separately match repository, branch, revisions and principals to the prepared operation, authorization, verified result and original execution domain. A forged model cannot become publication evidence merely by passing this validator.

## 4. Canonical bytes and digests

Digest selection is `kora.json-payload.v1`: the complete validated payload, including every optional field actually present. The stored field selection is `[""]`, whose empty string is the root JSON Pointer. No field is omitted because it looks informational. Reordering object members changes neither selection nor digest; array order is retained and changing it changes the content identity.

VcsRef contains only strings, objects and arrays. Canonicalization for this type is:

- UTF-8, without BOM or final newline;
- object keys sorted by Unicode code point, arrays in their recorded order;
- comma and colon separators with no whitespace;
- JSON short escapes for quote, backslash, backspace, form feed, newline, carriage return and tab; lowercase `\u00xx` escapes for other U+0000–U+001F characters; all other permitted Unicode scalar values encoded literally;
- no Unicode normalization or injected null/default fields.

SHA-256 covers exactly these bytes. The [fixed vector](VcsRef.digest-vector.json) contains the value, exact canonical text and hash `e978ea1463001e37edfb3970b0e3352cc929fde73efe5958958aedd9c9b5c955`; [vcs-ref.json](examples/vcs-ref.json) is its exact wire representation. This string-only rule agrees with the existing canonical serializer; it does not choose numerical canonicalization for other artifact types or require `kora.ir` to import the database package.

The selected approval digest and complete-payload storage digest remain separate roles even though both select the same canonical bytes for this revision. Storage deduplication and replay always use `payload_digest` and the saved contract identity, never a selected-field digest that another artifact type might narrow.

## 5. Upload, replay and result ownership

VcsRef upload producers send the canonical bytes above. API validation compares the received bytes with their canonical form and refuses a noncanonical representation before promotion or acceptance; it never rewrites staged content. Equivalent whitespace, member order, Unicode escapes, BOM or trailing-newline alternatives are refused even when their declared wire size and digest are correct. This preserves the existing whole-received-byte hash, part digests, size declarations and staging reservations. The VcsRef-specific 1 MiB ceiling applies before the larger generic artifact caps in [API section 8](../api-and-agent-surface.md#8-rate-and-size-limits).

Refusal creates no accepted artifact or promoted address. Existing staging ownership/reclaim applies on refusal, cancellation, disconnect and crash: reserved bytes remain charged until staging deletion. Namespace/worker/grant checks still precede storage; locked acceptance still rechecks authority. Exact full-payload/contract replay returns the original descriptor read-only under the existing replay rules, without renewing retention, restoring expired owners or authorizing another effect.

Persist the publication principal provenance before effect dispatch so recovery cannot attribute an earlier push to a newly acquired credential. `Runtime.finalize` freezes its complete push-only VcsRef with the completed `PublicationOutcome` before returning. Repetition and `RecoveryEvidence.publication_outcome.ref` return that exact saved result, including original principal display values; they do not rebuild it from current credentials or host responses.

Code-host enrichment creates a separate worker-owned immutable candidate before first artifact acceptance and freezes it for upload replay. It never mutates the runtime result. After `(attempt_id, output_name)` is accepted, no enrichment replaces it or creates a second payload under that identity. Later observations remain separate; a different producing attempt follows ordinary artifact production. Late authorization, dispatcher closure, retirement and deadline rules are unchanged.

## 6. Current observations and approvals

`ChangeRequestStatus` and `head_sha` return current observations; VcsRef stores no mutable status. An approval binds its exact recorded commit and artifact/version identity, never a URL or current registry alias. [ADR-0008](../../../docs/decisions/0008-content-bound-approvals.md) and [ADR-0021](../../../docs/decisions/0021-external-observations-outside-transactions.md) retain current-wait fencing, push invalidation and the mandatory fresh post-approval head check.

The supported revision domain also applies to head results, authenticated push revisions and approval-response observations. Out-of-domain values never count as matching/fresh heads, and are never truncated, hashed or coerced into the 255-character observed-head column. Record a content-free invalid-observation result without a valid observed SHA. Invalid provider results follow [scheduler section 16](../scheduler.md#16-external-observation-dispatch-adr-0021): fail the current waiting operation with `config_error`, close its wait and preserve any already-approved audit row. Invalid direct API input receives structured validation refusal.

A relevant authenticated push still conservatively invalidates a pending request even when its revision is invalid. For an approved gate awaiting verification, fence the outstanding read and require a newly claimed valid head observation. Stale results cannot affect replacement owners, extend expiry, satisfy a gate or resurrect a failed node. These bounds preserve both-database behavior without widening persistence or imposing Git-only identifiers.

## 7. Summary and availability

Summary is `kora.json-payload.v1`, the identity value. Store its applied contract identity, outcome, actual selection, selected projection/digest and summary at production/upload. Historical reads never execute today's plugin to reinterpret them.

The reference-only source VcsRef payload remains forever under [data-model section 10](../data-model.md#10-retention-dd-4). Its content-bearing identity summary has a separate `derived_payload` owner with `artifact_payloads` retention under [section 11.3](../data-model.md#113-results-observations-and-availability). Shared bytes do not keep an expired summary owner present and do not permit recomputation. CEL projections separately follow the source artifact's retention class. All namespace and applicable transcript-derived access checks remain. Summaries are untrusted display content, never operational diagnostics or authority.

## 8. Verification and activation

The following commands validate this publication's structural examples; they do not execute the semantic descriptor, provenance or runtime gates. Existing IR/scenario CI does not automatically check new artifact files. Continuous schema/model and immutable-revision enforcement activates with #209 through the existing pytest CI command.

```sh
uvx check-jsonschema@0.38.2 --check-metaschema knowledge/specs/artifacts/VcsRef.schema.json
uvx check-jsonschema@0.38.2 --schemafile knowledge/specs/artifacts/VcsRef.schema.json knowledge/specs/artifacts/examples/vcs-ref.json
uvx check-jsonschema@0.38.2 --schemafile knowledge/specs/artifacts/VcsRef.schema.json knowledge/specs/artifacts/examples/invalid/*.json
```

The first two commands exit 0. The third exits nonzero and reports every fixture: unknown status, traversal (including after U+2028/U+2029), empty segment, trailing control, oversized revision and URL without opener. Fixtures are deliberately invalid payloads, not valid examples to upload.

| Named contract | Owner and activation |
|---|---|
| `test_builtin_revision_1_schemas_are_immutable` | #209: pin exact schema/descriptor identity and reject unsupported/mismatched revisions |
| `test_builtin_digest_selection_is_stable` | #209: exact vector, nested key reordering, optional omission/presence, principal-field changes, Unicode/escaping and changed array order |
| `test_vcs_ref_schema_matches_model` | #209: structural schema agreement only |
| `test_vcs_ref_rejects_invalid_payload`, `test_vcs_ref_behavior_bounds` | #209: strict decoding, duplicate members, scalar/NUL refusal, known safe paths, unknown nested keys, URL/opener pairing, 255/256-character identities, 10,000/10,001 paths, multibyte 512 KiB and raw/canonical 1 MiB boundaries, depth/decoder failures |
| `test_vcs_ref_rejects_forged_publication_facts` | #156/#159 and worker-result acceptance, work item 17: correct schema/digest/summary with forged facts or no matching trusted operation never becomes publication evidence |
| `test_vcs_repeated_finalize_returns_one_ref` | #156/#159 and runtime integration: compare the complete saved push result across response loss, adoption, principal display/credential changes, retirement and expiry |
| `test_vcs_ref_enrichment_replay_is_immutable` | Work item 17 when code-host enrichment ships: independently freeze enriched output and survive upload response loss without changing runtime or accepted results |
| `test_vcs_ref_upload_requires_canonical_bytes`, `test_upload_requires_grant_before_bytes` | API work items 11/12/17: canonical acceptance, noncanonical refusal despite correct wire digest/size, cumulative/chunk/depth limits, current authorization before bytes |
| `test_refused_upload_bytes_are_reclaimed` | Upload/retention work items 5/12/17: refusal before promotion; staging reservations, cancellation/disconnect/crash and retention races on both databases |
| `test_target_source_replay_cannot_change_acceptance` | Work items 9/12/17: full payload/contract replay, cancel/clear/cache expiry, expired owners and unchanged-selected-digest cases for other artifact types |
| `test_push_cannot_rebind_old_evidence`, `test_approval_requires_fresh_current_head_observation` | Work items 9/10/17: valid/invalid revision ingress on both databases, authenticated push invalidation, movement away/back and fresh post-approval reads |
| `test_self_review_excluded_across_principal_change` | Code-host approval integration: historical automation identities, both publication principals and changed display/kind values |

Payload/summary availability tests also cover cross-namespace denial, missing/expired owners and a surviving or recreated shared blob. Provider and API diagnostic tests activate with those components; schema checks establish no diagnostic-runtime or isolation claim.

## 9. Performance methodology

Issue #209 owns `test_vcs_ref_validation_lifetime` in `tests/measurements/test_ir_models_cost.py` under the existing required measurement command. Measure strict decoding/validation, canonicalization, SHA-256 and identity-summary construction together and by phase. Inventory raw/decoded/typed/canonical representations, uniqueness sets, errors, results and schema caches; caller-owned successful results remain legitimate until discarded or persisted.

Cover increasing and near-cap inputs, 10,000 unique paths, early/late duplicates and common prefixes, invalid element types, dense unknown-field/error workloads, multibyte/escaped strings, malformed JSON near the end, depth failures and repeated cleanup. Use cheap bounds before expensive work and avoid pairwise uniqueness checks. Preregister exact samples, cycles, concurrency and justified budgets in #209; measure peak and retained Python/process/native resources with repeated cycles in one long-lived process and independent process runs. No payload/exception cache may retain rejected content.

The first VcsRef upload pipeline owns `test_vcs_ref_upload_validation_lifetime` in work items 11/12/17. In addition to those payloads, measure concurrent near-cap uploads, event-loop progress, staging keys/rows/reservations, storage connections, tasks and descriptors across success, refusal, cancellation, timeout, disconnect, recovery and shutdown on both databases. Validation stays outside run locks. Stored-byte quotas do not bound RSS; do not rescan accumulated bytes after every part. Register actual commands/artifacts at implementation, preserve the ten-minute CI budget, run on an uncontended runner and apply existing diagnostics/lifetime obligations. All runtime measurements here are pending implementation; this specification reports none as passed.
