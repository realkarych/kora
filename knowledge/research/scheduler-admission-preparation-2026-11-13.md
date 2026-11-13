# Scheduler admission preparation accounting

Status: implementation analysis for #1446. Independent performance review supports the logical reservation described here; complete admission verification remains pending. This record does not activate successful scheduler admission or replace its dependency, authority and resource checks.

The implementation is `engine/attempt_preparation_budget.py`, consumed by `engine/attempt_preparation.py`. It checks the complete allowance before namespace capture or graph parsing. Root and dependent admission remain one deliverable; the present local preparation accepts only a genuine root with no predecessor or declared input.

## Units and ownership

The allowance counts scalar payload bytes and logical object, member, iterator and frame slots. A slot is one logical unit, not a Python object header. This does not bound process heap, RSS, allocator caches, opaque Pydantic/native allocations or elapsed time. Work charges logical traversal/conversion operations, not CPU instructions.

The caller's `owned_units` remain charged throughout preparation and the subsequent writer. They cover the already-retained checked contract, optional parameter-schema arena, readiness, parameters, price and namespace source with their original provenance. The optional arena is not included in `execution_contract_read_reservation`. An earlier reader's spent work never supplies preparation credit. The graph limits are validated afresh and constrain complete canonical pair and IR widths; the returned reader reservation is not reused as this operation's charge.

The formula sums representations across stages rather than taking a smaller peak. The final candidate retains immutable source bindings and scalar freshness identities. Its caller retains operation ownership until commit, refusal or cancellation. The worker later acquires the durable sources independently.

## Representation ledger

- Decoding width `w`: `12*w`, comprising at most `4*w` scalar payload units and `8*w` container/member/read-frame slots. Syntax characters bound occurrences. Existing owners or rendering stages charge source strings separately.
- Rendering output width `w`: `36*w`. The `20*w` payload portion covers quoted text, UTF-8 conversion, decoded names, joined text and decimal bytes/text. The `16*w` slot portion covers the five write-frame fields, frame/iterator/set holders, active identities, name/fragment entries, entry pairs and traversal temporaries. Alternative scalar paths are deliberately summed. This accounts for kora-visible structures in `ir.graph_json`, not opaque encoder allocations.
- Typed width `w`: `292*w+45`. The largest inherited IR model has 20 fields. With object count `O`, member/element count `E` and numeric occurrences `D`, slots are conservatively `2*O*(6+3*20)+13*O+15*E+2*20+5`. This covers two record candidates, wrapper dictionaries/wire-name sets, detached JSON, typed dict/tuple/set conversion, provenance and root state. Payload adds `4*w+128*D`: `config.require_integer` can convert a short integral binary64 token into a 1,024-bit integer. Bounding `O`, `E`, `D` by `w` gives the coefficient. Pydantic's internal allocations are not certified.

## Complete operation inventory

The existing allowances are parameters 4 MiB, immutable Claim 12 MiB, node policy 256 KiB, composed policy 1 MiB, trusted namespace native source 64 KiB and immutable price 64 KiB. The namespace native admission dominates its canonical UTF-8 projection. Graph widths are the actual checked canonical strings. No new smaller product limit is introduced.

| Group | Representations charged |
|---|---|
| Decodes | IR twice; contract once; namespace capture twice and namespace policy layer once; node policy once; source wrapper once |
| Renders | Namespace capture; IR canonical comparison; parameters; Claim input map; price; immutable Claim; node policy twice; namespace policy; composed policy; input logical map/storage; source wrapper |
| Typed | Complete graph; namespace ToolPolicy twice; node ToolPolicy twice |
| Auxiliary | Selected IR/contract maps; parameter traversal; price model dump; native namespace/source traversal; Claim member-name sets |
| Encodings/hashing | Parameters, input text, price text, immutable payload, both node-policy payloads, namespace policy, composed policy and source wrapper |
| Diagnostics | Two collections, each conservatively charged typed and rendered representations at the existing 1 MiB diagnostic limit |

For the actual root, the input map is 2 bytes, storage is 25 bytes and the outer source wrapper is 37 ASCII bytes. Dependent integration must replace these constants with its complete approved input derivation before activation. A source witness's native quantity alone is not a preparation allowance.

Each named operation receives `32*(width+1)^2` work units, covering repeated decimal conversion and key comparisons rather than assuming linear work. Duration conversion adds an operation at IR width plus eleven digits for the maximum unit multiplier. Independent review must check the complete call sequence and these coefficients.

The compatibility path refuses nonempty node and namespace allow/deny patterns before `tool_policy_refusals` normalizes them. Metadata still consumes its allowance. Enabling nonempty policies reopens Unicode normalization accounting; this calculation makes no supported-runtime normalization-expansion claim.

## Verification obligations

`test_attempt_preparation_refuses_incomplete_fresh_allowance` exercises insufficient retained/work/source allowances, preservation of already-owned units and exact-bound admission with real checked graphs on both databases. Functional tests use actual preparation and cover replay, expiry, policy/price freshness, wait closure and rollback.

Those tests establish the implemented comparison and lack of metadata effects. They do not prove the complete allocation/conversion inventory. Independent performance review inspected the concrete call sequence and logical coefficients; its conclusion does not establish caller-owned provenance or a physical memory envelope.

`test_attempt_admission_serializes_last_slot_and_releases_failed_reservation` exercises two real runs competing for the final slot, including commit, rollback and cancellation. The committed winner keeps the follower waiting without another attempt; rollback or cancellation removes the first attempt and its source witness, pointer and events before the follower creates its attempt. Both database variants verify released locks and connections.

`test_attempt_admission_keeps_final_node_within_claim_read_allowance` checks SQLite's complete row bound after the current-attempt pointer is written. A previously admitted row that exceeds the allowance after mutation rolls the whole transition back; the exact final bound preserves Claim-source readability. PostgreSQL keeps its existing compact-source admission rather than applying SQLite's physical-row formula. Successful admission satisfies the exact open wait under the existing wait state machine.

Full-size partition coverage, dependent accepted-output authority, discovery, the actual processing-unit owner and final CI remain required before readiness.
