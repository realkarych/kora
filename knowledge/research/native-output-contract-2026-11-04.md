# Exact native builtin effective-output decision

The project owner accepted the [plan under #675](https://github.com/realkarych/kora/issues/675#issuecomment-6617141788) on 2026-11-04. [Data-model section 11.1](../specs/data-model.md#saved-exact-native-builtin-output-contract) is normative. This contract-only publication has zero production changes and implements no package source, constructor, reader, projection or execution path.

## Scope and alternatives

Use an exact seven-member by-value value for revision-1 AgentSession, CommandResult and VcsRef direct roles without an authored output schema. The selected type closure, descriptor and behavior retain native validation; checker selection is recorded separately. AgentSession includes its complete published policy and checker schema, while VcsRef/CommandResult explicitly select ordinary declared-schema checking under the fixed execution semantics.

The alternative designs a unified native inline/custom effective composition first. The chosen slice supplies the ordinary published roles while retaining wider requirements. Authored empty schemas are not omission, and named descendants/lists/wrappers/exports do not silently lose constraints. Unsupported composition is unavailable through this constructor rather than a newly invalid IR form. Full installation/public error behavior retains its accepted owners.

The original native validation schemas and AgentSession checker are distinct documents. AgentSession's checker local references use its own root; the full logical value cannot replace the permanent metadata allowlist. Native validation and fixed checker view grant no current data access, source authenticity, capture/publication provenance or execution authority.

## Identity and source evidence

The complete value participates in the existing effective SHA-256 identity and graph/deployment identities. Published policy/schema object order and derived field order are preserved. There is no current-alias repair, historical backfill, new builtin revision, second digest, migration or numerical default. By-value definitions can repeat between outputs; the whole consumer owns their aggregate admission and source binding.

At decision time, the package catalogue contained main artifact schemas, not the AgentSession checker/policy files. The actual source implementation must include exact published files and verify source/wheel agreement. Descriptor-to-policy and policy-to-checker hashes must verify the same trusted bytes used for parsing. Raw acquisition, mismatch/failure, decoding and hash buffers require explicit finite admission before growth; existing payload/CEL/preparation caps do not prove it. No external dependency is selected by this decision.

## Required implementation and activation checks

- Actual role and omission selection; explicit schema/custom/list/wrapper cases cannot be accepted as the narrow native case.
- Exact native revision/schema/descriptor/behavior and package-byte agreement, including corrupt/mismatched file failure without uncontrolled allocation.
- Complete AgentSession policy/checker values, own-root reference resolution, and actual saved-view CEL allowed/excluded field checks; the logical schema cannot substitute for its view.
- Rehashed type/source/policy/view/role substitution refusal, mandatory fields/order, immutable result and no historical fallback.
- SQLite/PostgreSQL scalar/order/value/digest round trips with checking after actual read-unit release and final connection closure.
- Complete output-count/work/retention/concurrency admission, plus native provider, projection, transfer, provenance, retention and execution consumers before activation.

Independent correctness/security/performance proposal review covered the final format and existing published sources. The accepted proposal SHA-256 is `3548fc5e52c5be4acff908de7e90f85c163f1b1bf53f64722b05298500a55caa`; issue review records identify inspected inputs and bounded clarifications. Owner approval and contract publication do not prove runtime checks. Actual-diff review and one complete final-head CI remain mandatory for publication; no unchanged CI rerun is authorized.

Existing TM-04/TM-10, TM-06/TM-07 and TM-08/TM-11/TM-12 cover resource admission, content disclosure, immutable identity and trusted evidence. No sink, credential, privilege or operational permission is added. #593 retains complete graph construction, checking, publication/start and activation diagnostics; #138 and the native work-item owners retain payload/provider/projection composition. Retained pricing/CEL mutation findings add no campaign or new merge hold.

## Implementation follow-up

[#679](https://github.com/realkarych/kora/issues/679) implements the exact direct leaves and trusted packaged AgentSession sources; its [implementation record](native-output-construction-2026-11-04.md) identifies real builder/reader, saved-view CEL and both-database evidence and remaining activation limits. The publication above selected no numerical server default or complete-execution authority.
