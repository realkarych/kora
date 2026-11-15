# Standard-schema replacement: source analysis and capacity rationale

Checked on 2026-11-15 for [#1577](https://github.com/realkarych/kora/issues/1577), against repository base `934a94153ef524d342fda65b05e125e2d2805bf5`. This is agent-authored analysis, not accepted policy or executable qualification. The [proposed specification](../specs/schema-validation.md) contains the exact decision submitted to the owner.

## Pinned implementation inputs

The lockfile selects jsonschema 4.26.0. Source inspection used that installed distribution from the existing locked development environment, without changing dependencies or executing a new probe. The relevant files are `jsonschema/_keywords.py`, `validators.py`, `_utils.py`, `_types.py` and `exceptions.py`. Repository source inspection covered `ir/structured_schema_*`, `engine/parameter_*`, `engine/graph_outputs.py`, `graph_output_inventory.py`, `graph_schema_preparation.py`, `graph_cel_facts.py`, `graph_execution_contract.py`, `graph_parameters.py` and `protocols/artifact_type.py`.

Library source is evidence for this pin only. The implementing PR must verify its actual lockfile/distribution inputs and run the named regressions; this record is not permission to accept another version under the same policy.

## Concrete mechanisms

| Source mechanism | Consequence for the proposed design |
|---|---|
| `anyOf` collects `list(descend(...))` for failed alternatives; `oneOf` collects failures before a first success | Failed branches can retain far more state than the one final mismatch exposed by is_valid |
| `_Error` stores context and sets each child's parent; args retain original constructor values | Parent/context cycles retain candidate/schema objects; outer iterator closure does not establish release |
| A later successful alternative discards earlier failures internally; not/if/contains also invoke nested is_valid | The caller cannot repair all hidden cycles merely by clearing a returned error |
| Other supported handlers create individual errors without those context trees | Two exact Boolean handlers, with explicit child iterator closure and fixed context-free mismatch, address the identified cycle source without replacing the whole keyword evaluator |
| `validators.evolve` calls validator_for on the new schema | An accepted root or referenced $schema can select the stock class and lose an adapted integer predicate |
| Non-float multipleOf uses modulo; equality recursively separates booleans from numbers | Exact Fraction views are a plausible narrow numerical adapter; raw binary-float conversion is not canonical-decimal interpretation |
| `uniq` sorts using native nested comparisons, then checks adjacent elements with bool-aware recursive equality | `[[1], [true], [1]]` can hide equal nonadjacent elements; replace this handler with bounded pairwise recursive equality |
| `not` calls nested is_valid; both `$ref` and `$dynamicRef` are independent keyword handlers | Charge not as a call edge and preserve both reference assertions in the execution view |
| `referencing` resolves absolute URIs through standard URL parsing; CPython urlsplit has a global finite LRU cache | Original pointer strings would survive local registry disposal; use only fixed ordinal resource IDs in library resolution |
| Schema/source builders and inventory readers reconstruct expected saved contracts with the current policy constant | A global constant replacement would break old records; complete writer/reader dispatch must use explicit selected/saved identities |

The previously recorded fixed probe under [#1361](https://github.com/realkarych/kora/issues/1361#issuecomment-6867618080) returned false for stock `0.3` against `multipleOf: 0.1` and true for the existing exact helper. That probe was not repeated. The replacement's regression must preserve mathematical truth and original canonical bytes, and must cover its complete supported numerical surface rather than extrapolate from that one case.

## Why a small adapter is selected

Changing only the returned error message leaves internal cycles untouched. Depending on the process's garbage collector would change the existing actual-release obligation and make finite concurrent admission insufficient to bound abandoned trees. Forking jsonschema or patching its module globals would enlarge dependency and isolation risk.

The proposal therefore keeps the standard keyword machinery and replaces anyOf/oneOf error aggregation and the incorrect stock uniqueness sorting, alongside the numeric type view and closed-resource execution view. The two Boolean handlers retain saved alternative order and ordinary truth conditions, but intentionally do not preserve the library's diagnostic error tree. Public kora diagnostics never promised that tree. They propagate capacity/internal failures rather than treating them as false. They do not add a work meter to every keyword. The third handler performs bounded unordered-pair comparisons using the pinned recursive equality, so native ordering cannot merge boolean and numeric equivalence classes. Fixed ordinal resource registrations preserve original targets without sending authored pointer strings into shared URL caches; both reference keyword names remain present.

This is a design assertion to verify against the complete adapted call path. It is not evidence that implementation already releases every generator, registry, temporary copy or traceback. The no-hidden-cycle premise and disposal of unexpected exception ownership remain blocking implementation checks.

## Capacity selection

The immutable artifact envelope remains 1 MiB, container depth16/root1 and 14000-bit integer magnitude. Manual request identity remains 4 MiB, container-edge depth32/root0 and signed64 integers. These are separate domains and cannot be merged by sharing a function.

The new policy adds explicit support choices: 262144 value occurrences, 65536 array items, 16384 object members, 1 MiB per value string; schema source 1 MiB/16384 nodes/64 documents/2048 positions/4096 reference edges, depth32 and narrower strings/containers; potential call depth32, 131072 calls, 128 unique items and 67108864 score units. Some schema-input values match existing preparation capacities; their reuse is proposed explicitly, not inherited authorization. V1's depth64 and 16777216 dynamic work units are not represented as equivalent to the new certificate.

These are conservative product-support limits selected for a finite first implementation. They are not tuned to a successful timing or RSS sample. A valid value can be unavailable under this profile even when every independent maximum passes. Large equality literals, nested uniqueness or repeated references make that tradeoff particularly visible. The owner is deciding this support envelope, not accepting an unmeasured latency claim.

The score uses observed dimensions and saturating arithmetic. Conversion charges use each number's bit bound, so ordinary small decimals are not charged as 32768-bit arithmetic. Branch and reference counts are structural: an acyclic chain that doubles its previous suffix doubles E/W instead of being credited once by a visited set. Shared-target memoization saves preparation work without erasing execution multiplicity.

For a schema sequence `s_n = anyOf(ref(s_(n-1)), ref(s_(n-1)))`, the conservative recurrence is `E_n = 3 + 2 * E_(n-1)` with `E_0 = 1`: each level includes the anyOf position and both intermediate reference positions. Thus E15 is131069 and E16 is262141, with call depth `2 * n + 1`: n16 exceeds both the call ceiling and depth32 before expanding/evaluating it, irrespective of truth. The score may refuse earlier. This is an arithmetic witness, not a benchmark or assertion about all failure trees.

The equality and membership scores intentionally include worst-case key comparison/lookup rather than assuming collision-free hashing. Existing byte/cardinality admission bounds the initial shape scan. The preparation score is admitted before proportional lookup/resolution/sort work. V2 does not promise the old metering schedule or CPU-instruction accounting.

The repr bound and entry formula reserve variable character/data storage separately from finite object/slot counts. A 256-MiB per-operation variable-byte ceiling is a support ceiling, not an allocation target. The owner of multiple calls must reserve each actual conservative certificate against its own aggregate budget; four operation slots do not imply four full-cap operations can run together. Python overhead, allocator retention and RSS remain measurements with stated limitations. These numbers do not qualify the execution-content installation's future concurrency by themselves.

## Compatibility and implementation split

The new output policy and the independent parameter profile both use v2 identities; reserved v1 identities remain historical/unavailable. Readers preserve exact old shapes, bytes, digests and accepted/skipped receipts. New complete constructors include the new binding only after the actual shared producer/reader exists. No database rewrite or inferred compatibility supplies missing authority.

The first implementation is the shared pure decision, followed by actual StructuredOutput composition and saved-policy checking. Parameter integration then uses that same decision with its own profile. Their proposed 750–950, 700–950 and 650–950 production-line estimates remain subject to concrete source inventory and the repository's hard 1000-line complete-diff limit. The design authorizes no over-limit exception.

Four currently isolated retirement groups contain 949, 735, 733 and681 production lines respectively. The v1 policy reader and remaining live source/reference/preparation facilities are not deleted merely because their names contain evaluator terms. Replacement contract tests keep the distinct adversarial cases from #1399. Complete operation activation remains with accepted-output storage, lossless projections, trusted runtime observations and Worker reports before scheduler settlement.

## Verification status

This document records source analysis and proposed arithmetic, not executed tests or an experimental campaign. The design change runs documentation/link/architecture checks and one complete final-head CI, and receives independent correctness/security/performance review. Actual policy acceptance and exact docs wording require the owner.

Latest available master mutation audit [43584820056](https://github.com/realkarych/kora/actions/runs/43584820056), source `d623a105c17ac682e8017a469fc8c6535990ec28`, attempt1, is partial due to its budget and covers pricing/CEL only. No applicable schema-validator finding was supplied. No new mutation campaign or numerical performance gate follows from it.

