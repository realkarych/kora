# Type-declaration validation evidence

Date: 2026-10-30. Implementing [issue #468](https://github.com/realkarych/kora/issues/468), a semantic part of MVP work item 3. This record describes scope and method; actual executable results and independent review provenance belong to the PR with their tested revisions.

## Delivered boundary

`validate_type_declarations` consumes a structurally validated `GraphDocument` whose caller-owned maps stay unchanged during the synchronous call. It checks every supplied contract and every root/body input/output declaration, including subgraph interfaces and unused bodies, once. It parses balanced nested `List[T]` spelling without recursion, requires declared base and parent names, checks custom-name qualification, recognizes the four published revision-1 built-ins and reports only members of parent cycles. Missing parents do not enter the cycle graph. No transitive closure or call-instance expansion is built.

`declarations_valid` describes only this pass. Raw JSON/Pydantic admission, schema dialect/reference semantics, effective schema compatibility, installed behavior binding, namespace ownership authorization, edge assignability, CEL and execution admission remain outside it. Recognition of list spelling does not activate aggregate materialization. `ReviewBundle` and `PreviousAttempt` are not in the published catalogue and remain unsupported here.

The pass shares unchanged diagnostic collection, safe-source conversion and iterative cycle analysis with the existing IR owner. Fixed messages never include rejected spellings/revisions, schema payloads or behavior identities. Accepted identifier paths locate the field; ASCII contract keys above the shared 256-byte segment bound fall back to the `type_contracts` ancestor. Unsafe source labels are omitted, not rendered. The INFO completion event reports only validity and counts; existing process diagnostics own output transport and unexpected failures.

## Blocking evidence

`tests/ir/test_type_declarations.py` covers all eight node kinds, root outputs, stored-body interfaces/exports, unused/shared definitions, nested and malformed spellings, present and missing bases/parents, independent faults, chain/self/multiple-node cycles, built-in manifests and unsupported pairs, custom-name qualification, insertion-order determinism, exact visit counts, long contract keys and safe/unsafe source metadata. Dense faults retain at most the existing 1,000-error prefix and count every omission. Existing collector tests continue to enforce the combined one-MiB serialized bound.

`test_type_declaration_validation_releases_owned_references` covers valid and invalid nested graphs. Weak references prove release of the source document, graph, body, nodes, custom contract and source objects while the result remains live, and release of retained diagnostics when the result is dropped. The pass owns no task, thread, descriptor, database transaction or child process.

`test_type_declaration_logging_is_content_free` covers both outcomes, fixed event fields and stdout/content exclusion. `test_type_declaration_result_survives_unavailable_sink` uses the real bounded logging transport with blocked and broken pipes; validation results are unchanged and shutdown releases the writer. Existing transport limits and deadlines are unchanged.

## Diagnostic method

`test_type_declaration_resource_observations` uses existing Report/Sample support and the JUnit suite property `type_declaration_resources`. Inputs are built before measurement: 1/100/1,000 declarations, 1,200-contract acyclic and cyclic parent chains, 1,000 children sharing one parent, a List spelling at depth 2,000, and 3,000 missing declarations. There is one warm-up per workload, three untraced timings and six mixed batches with post-GC traced current/peak memory and available Linux RSS before/after. There is no intentional cache. Samples record declaration and contract counts separately and normalize by their sum, so contract-only workloads have a nonzero denominator.

Index-based spelling scans make one final base-name slice. Parent graph construction and SCC traversal are linear in contract vertices/parent edges; body traversal visits stored definitions once. Including identifier hashing and diagnostic selection, work scales with visited nodes, total spelling/name bytes, contracts and bounded collector operations. Per-call graph storage scales with supplied contracts; diagnostics retain the unchanged count/byte prefix. No new input-depth or document-capacity limit is claimed.

Elapsed time, process memory and RSS on shared runners are diagnostics without a passing threshold or leak claim. Exact output bounds, traversal counts and weak-reference release remain blocking. The observations exclude input construction and raw schema validation; RSS is unavailable on macOS through this collector. Material unexplained growth or a demonstrated retention/contract defect triggers bounded follow-up under the existing verification policy. No old measurement manifest, CI lane, acceptance method or public surface changes.
