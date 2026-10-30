# Graph-link validation evidence

Date: 2026-10-30. Implementing [issue #463](https://github.com/realkarych/kora/issues/463), first semantic part of MVP work item 3. This record describes the implementation and measurement method; executable results and independent review provenance belong to the PR with their tested revisions.

## Delivered boundary

`validate_graph_links` consumes a structurally validated, caller-owned `GraphDocument` that remains unchanged during the synchronous call. It checks root and every stored body once, including unused definitions. Ordinary inputs and explicit control dependencies form the node graph; a separate reference graph detects recursive body definitions. Invalid output names do not erase dependencies between known nodes. Branch references check direct ownership without inventing edges. Reserved data sources have no node edge. Body exports, wrapper export/collect names, fan-out input names, target producer/output names and `independent_of` membership are checked within their owning scopes.

`links_valid` is only a link result. Raw/schema-error admission, payload paths, retry legality, type/contract closure, CEL, providers, installation features and HTTP/CLI/MCP validation remain pending. No provider, filesystem or execution authority is introduced. Existing model, CEL and API consumers are unchanged. Unsafe source metadata becomes null; messages are fixed and paths contain structural fields, indices and accepted identifiers only.

## Coverage and ownership

| Component and integration owner | Changed risk | Blocking evidence | Diagnostic evidence | Independent profile |
|---|---|---|---|---|
| IR model-to-link analysis, owned by the graph-link pass | Scope isolation, cyclic versus downstream nodes, missing prerequisites and body reuse | `tests/ir/test_graph_links.py`, iterative algorithm inspection | Sparse/deep/shared/branch workload timings | Correctness, security, performance |
| Diagnostic producer-to-collector, owned by `DiagnosticCollector` | Stable order, count overflow, content protection, bounded retained output | `tests/ir/test_diagnostic_collection.py`, safe-source tests | Dense refusals and mixed-batch memory observations | Correctness, security, performance |
| Validator-to-process diagnostics, owned by the graph-link pass and existing logging transport | Authored content disclosure, sink stalls | `tests/observability/test_graph_link_logging.py`; existing bounded transport/shutdown contracts unchanged | No separate logging measurement campaign | Correctness, security, performance |

Focused local execution covers the named tests and architecture/static checks. Final-head CI supplies the complete supported Linux/macOS and strict-check evidence. No metadata database operation changes. There is no additional native review checkpoint when these independent profiles supply qualifying results.

## Deterministic bounds

Per-scope and definition cycle analysis use iterative strongly connected components in O(V + E) work and space. Control dependencies are indexed once for branch membership. Stored definitions are analyzed without instance expansion. No persistent graph cache, task, thread, descriptor or child is created by analysis.

The collector maintains at most 1,000 candidates per severity in reverse heaps. Each prefix has an encoding weight that fits a standalone 1 MiB compact diagnostic envelope. The two prefixes therefore retain at most 2 MiB of encoded weight, plus bounded candidate/order objects, one cutoff per severity and a transient candidate. This is not a Python heap or whole-validator memory bound. The monotone earliest-excluded key, including emission ordinal, prevents skipping an earlier large entry to retain later entries. The final combined envelope is at most 1 MiB, errors precede warnings, and count/byte omissions remain separate by severity. Any discovered error prevents success, including an omitted error. Overflow beyond the exact integer range refuses the collection.

An independent finite-list oracle tests count truncation and byte prefixes in forward and reverse arrival order, escaping and multibyte cases, exact byte capacity and one byte beyond it. Intermediate retained counts and encoding weights are asserted after each admission. Real-model dense input emits 3,000 independent errors from 1,000 nodes and retains 1,000 with 2,000 omissions. Deep reusable definitions exceed Python's ordinary recursion depth. Weak-reference checks prove that the returned diagnostics do not retain the input graph and disappear after the result is released.

## Resource observation method and decision rule

`test_graph_link_resource_observations` runs in the existing ordinary IR test selection and records a `graph_link_resources` JUnit testsuite property through the existing measurement `Report`/`Sample` format. There is no new CI lane, manifest entry or numerical merge gate. The report records its revision, interpreter and platform; ordinary CI's checkout receipt identifies the tested merge tree. A local dirty run is explicitly labeled `local-uncommitted` unless a revision was supplied.

The fixed workloads are sparse chains of 1/100/1,000 nodes, 1,000 nodes with three unresolved inputs each, 1,200 stored definitions in a chain, 1,000 calls sharing one stored body, and a 300-target branch with growing control fan-in. All input construction is outside observations. One warm-up per workload precedes three untraced timing samples. Six fixed batches each validate every workload; traced current/peak memory is observed after collection, with current RSS before/after where available. No timing sample includes tracing; there is no intentional analysis cache. The process-wide observations include test/report allocation and allocator effects.

The hypothesis is that stored-body work avoids expansion and repeated mixed success/refusal leaves no graph/result retention. Source inspection, exact visit counts, collector bounds and weak-reference release are blocking evidence. Time, traced heap and RSS are diagnostics on shared CI hosts, not latency, leak-freedom or NFR-3.3 guarantees. Preserve adverse measurements. Revisit only on a demonstrated retained graph/result, violated deterministic bound, or material unexplained sustained growth; do not repeat unchanged workloads for a favorable number. Raw input size and upstream Pydantic error allocation remain outside this typed pass and must be bounded or measured when the whole-document admission boundary is implemented.
