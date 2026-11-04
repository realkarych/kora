# Exact native output construction, 2026-11-04

[Issue #679](https://github.com/realkarych/kora/issues/679) implements the owner-approved [native format](../specs/data-model.md#saved-exact-native-builtin-output-contract) under #675. It composes the real pinned-IR/type/output checkers with fixed native sources. It does not implement complete graph publication, native payload projection or execution.

## Source and identity

`engine.graph_native_sources` acquires the exact packaged AgentSession policy/checker files, reserving their complete 1,174/5,855 raw bytes before opening either file. These lengths are immutable publication identities, not configurable product limits. Each scoped read requests at most the expected size plus one byte; length/hash mismatch fails before decoding. Descriptor-to-policy and verified-policy-to-checker hashes use the same captured bytes then parsed. Filesystem/decoder failures yield fixed content-free exceptions without rendered private context. There is no caller-selected production path, network, provider, SDK or retained cache.

`engine.graph_native_outputs` derives exact agent session/publication and command result roles, root or stored body, from pinned IR and checked types. Declaration schema omission is mandatory. Custom/list/wrapper/inline or wrong-role cases cannot inherit this representation. Existing IR model/output rules remain independent; this component adds no public graph-error classification. A separate structured-output configuration cannot change a native leaf.

The complete seven-member canonical value retains native definition/behavior and explicit checker/validation selectors. AgentSession retains complete published policy/checker data and the original checker root independently of the logical validation schema. The reader admits the saved value, checks its digest, derives expected meaning and compares exact text. Rehashing substitutions does not make them valid. Only immutable canonical text and a 64-hex SHA-256 escape the call; no current-default or historical repair occurs.

## Admission and ownership

The explicit caller bound covers I + T + P + R + V: canonical IR, complete types, output selector, complete generated/saved record and published view bytes. V is 7,029 for AgentSession and zero for CommandResult/VcsRef. Reader saved-R admission precedes dependent derivation; V admission precedes either new policy/checker file read. Existing logical-schema catalogue acquisition inside type verification is unchanged and separately owned.

Expected and saved record buffers have separate finite allowances under the same logical bound, so the implementation retains a constant number of snapshots, not a single physical buffer. Source read sentinels and the shared array parser's six-character wrapper are fixed overhead. Indexes, closure and sort work derive from admitted inputs. No heap/RSS/time guarantee, public limit or new measurement campaign is selected. Whole consumers still own output count, repeated whole-IR/type checking and hashing, concurrency/frequency and aggregate retained state.

The call owns scoped file handles and private values only. It opens no database transaction or native process. Physical persistence tests close the actual read unit before checking and verify final connection closure. They store a partial leaf payload for codec fidelity, not a new complete outer graph format or execution authority.

## Verification and activation limits

Focused tests cover all exact root/body roles; separate structured-config isolation; wider representation refusal; rehashed field/type/view/policy/root/behavior/order substitutions; mandatory nulls; digest and exact/one-under input/source admission; and detached results. Actual file tests cover successful/missing/directory/truncated/oversized/corrupt sources and descriptor balance. Wheel checks prove byte agreement and actual installed-core-only construction/checking, including refusal after real installed-source corruption without a vendor SDK.

Real CEL checking from the saved AgentSession checker accepts published metadata paths and rejects transcript/denials/full logical nesting, including original local-reference resolution. This is static checker integration, not payload projection or current data access. SQLite/PostgreSQL persistence exercises native value/order/digest and original IR scalar fidelity after read-unit release, owner-scoped reads and final connection closure. Existing StructuredOutput regressions retain their behavior after the narrow shared-helper extraction.

Pure identity/source helpers add no sink. Existing IR validators retain their structured content-free events; DEBUG tests exclude authored content and stdout and verify writer shutdown. Full publication/public refusal diagnostics remain #593. TM-04/TM-10, TM-06/TM-07 and TM-08/TM-11/TM-12 retain admission, content, identity and verification ownership.

The implementing PR records actual focused/static/installed-package checks, independent correctness/security/performance source reviews and complete final-head Linux/macOS CI. Linux supplies PostgreSQL; both platforms supply SQLite. Historical failures remain failures. No unchanged rerun is authorized by this implementation. Full output inventory/source binding, lists/custom/wrapper composition, native provider/projector/provenance/retention, remaining graph sections and execution keep their separate activation requirements.
