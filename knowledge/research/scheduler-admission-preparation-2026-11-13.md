# Scheduler admission preparation accounting

Status: implementation analysis for #1446. Independent performance review supports the logical reservation described here; complete admission verification remains pending. This record does not activate successful scheduler admission or replace its dependency, authority and resource checks.

The implementation is `engine/attempt_preparation_budget.py`, consumed by `engine/attempt_preparation.py`. It checks the complete allowance before the caller traverses or materializes inputs, captures namespace values or parses the graph. The helper's preliminary count-work admission precedes its own name/path-length traversal. Root and dependent admission remain one deliverable. Local preparation accepts genuine empty-input roots, control-only dependent nodes and conditional absent-only inputs from declared optional ordinary producers. Required or present inputs still require the real accepted-output integration.

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

For the actual root, the input map is 2 bytes, storage is 25 bytes and the outer source wrapper is 37 ASCII bytes. An absent-only reservation accepts the selected readiness input tuple and replaces these widths with conservative bounds. With `n` inputs and `text_width` equal to the sum of input-name and selector-path character lengths, the logical map width is `L = 2 + 128*n + 6*text_width`, ordered storage is `W = L + 23 + 7*n`, and the ASCII source wrapper is bounded by `S = 6*W + 8`. Fixed syntax fits within the per-input term; escaping and UTF-8 conversion fit within the text multiplier. The additional auxiliary width `L` covers declaration/version maps, input records and logical/member/Claim maps. Strings already retained by readiness remain borrowed. Empty inputs retain the complete original reservation, with no additional auxiliary operation.

The helper obtains `n` without traversal and checks `32*(n+1)^2` against the fresh work allowance before traversing names and paths. It performs no encoding or collection construction during that traversal and includes the preflight charge once in total work. The caller passes the exact selected input tuple before traversing it or constructing maps. This accounting extension grants no absent-input authority. The prepared candidate borrows the pinned predecessor descriptors and retains at most two absent producer paths; the readiness owner remains charged throughout. Final admission decides from fresh locked producer states without scanning declarations or parsing payloads. Accepted present inputs require their own complete derivation. A source witness's native quantity alone is not a preparation allowance.

Each named operation receives `32*(width+1)^2` work units, covering repeated decimal conversion and key comparisons rather than assuming linear work. Duration conversion adds an operation at IR width plus eleven digits for the maximum unit multiplier. Independent review must check the complete call sequence and these coefficients.

The compatibility path refuses nonempty node and namespace allow/deny patterns before `tool_policy_refusals` normalizes them. Metadata still consumes its allowance. Enabling nonempty policies reopens Unicode normalization accounting; this calculation makes no supported-runtime normalization-expansion claim.

## Verification obligations

`test_attempt_preparation_refuses_incomplete_fresh_allowance` exercises insufficient retained/work/source allowances, preservation of already-owned units and exact-bound admission with real checked graphs on both databases. Functional tests use actual preparation and cover replay, expiry, policy/price freshness, wait closure and rollback.

Those tests establish the implemented comparison and lack of metadata effects. They do not prove the complete allocation/conversion inventory. Independent performance review inspected the concrete call sequence and logical coefficients; its conclusion does not establish caller-owned provenance or a physical memory envelope.

`test_optional_input_preparation_charges_generated_maps_and_strings` checks the absent-only budget helper and actual Claim preparation with a real pinned dependent graph on both databases: exact allowance, one-unit-short retained/work refusal and the preliminary count-work refusal. Neither reservation nor detached preparation acquires a metadata connection. The source tuple, generated maps and final candidate remain part of the same operation. Independent performance review accepted the helper's widths, inventory and preflight ordering; the caller delta and its complete ownership require matching implementation review.

The optional admission tests exercise fresh producer authority, immutable binding history, original target identity, compatible producer replacement, recovery occupancy and enclosing cancellation/expiry. Concurrency cases cover producer clear first, a later return to the same permitted unsuccessful state, admission first, rollback and cancellation using real serialized writers. The fixture writes model producer transitions at that lock boundary; they do not implement or qualify the complete clear API. The owner-approved conditional-absence rule makes a new decision from current facts, rather than claiming to detect every intervening clear. Successful producers still require actual accepted-output evidence.

`test_attempt_admission_serializes_last_slot_and_releases_failed_reservation` exercises two real runs competing for the final slot, including commit, rollback and cancellation. The committed winner keeps the follower waiting without another attempt; rollback or cancellation removes the first attempt and its source witness, pointer and events before the follower creates its attempt. Both database variants verify released locks and connections.

`test_attempt_admission_keeps_final_node_within_claim_read_allowance` checks SQLite's complete row bound after the current-attempt pointer is written. A previously admitted row that exceeds the allowance after mutation rolls the whole transition back; the exact final bound preserves Claim-source readability. PostgreSQL keeps its existing compact-source admission rather than applying SQLite's physical-row formula. Successful admission satisfies the exact open wait under the existing wait state machine.

Full-size partition coverage, dependent accepted-output authority, discovery, the actual processing-unit owner and final CI remain required before readiness.
