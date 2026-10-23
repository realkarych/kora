# Local conformance lifetime design review — 2026-10-23

Approved baseline for review: `18a229427afecba74e2e8d402027508c739a9a81`.

Reviewed ABI draft: SHA-256 `930a8eba7ffb7b7bb544b928d39c4d3c13a726ec8aef82b6612312f030de8209`.

Eight distinct fresh reviewer contexts covered the two affected components and four additive profiles. Each read the applicable approved instructions, profile and contracts, operated read-only, and returned a scoped result. Changed conclusions were re-reviewed after corrections. No reviewer executed candidate code or represented design review as a runtime result.

| Component | Correctness | Security | Performance | Condensation |
| --- | --- | --- | --- | --- |
| Conformance parent, command and adapter bridge | Passed | Passed | Passed | Passed |
| Local-runtime/testing-runner launch and closure boundary | Passed | Passed | Passed | Passed |

The reviewed design resolves the following concrete concerns:

- The parent records the exact runtime domain/root/attempt/nonce/digest before effects and can use the existing registry/retirement/fallback machinery after pytest exits.
- Detached process identity is final before activation; managed harness children preserve runner/SDK parentage and reaping.
- Shared async assertions preserve direct-injection interfaces and the exact selected provider while command execution uses one resource-owning loop.
- Recovery has an exclusive current owner. An old filesystem helper retains mutation exclusion through settlement, and one ordered publisher prevents stale snapshots within a generation.
- Stalled filesystem I/O cannot hold parent escalation/exit indefinitely; uncertainty keeps owned effects and bounded recovery information.
- Transport records contain typed identities, with private OS handles confined to the controller. Child output uses capture/null endpoints, never user output or lifetime control.
- Runtime release retains tombstones. Separate disposable-environment destruction requires every producer and older effect closed, with no unknown recovery/publication and no borrowed/production root.
- The runtime interface uses the same acquisition operation; there is no additional launch state machine. Cleanup uses the invocation ID as its journal key, without a second public identifier or settlement watermark.

The project owner [approved the plan and exact documents](https://github.com/realkarych/kora/issues/231#issuecomment-6367958968) on 2026-10-23. The [published plan and review evidence](https://github.com/realkarych/kora/issues/231#issuecomment-6367818635) retain the reviewed draft. The contract is [conformance-lifetime.md](../specs/conformance-lifetime.md); ADR-0005 records the decision. Integration preserves the subsequently merged IR amendment and VcsRef revision-1 contract in master `85577d2`.

## Readiness evidence ownership

The design PR recomputes this same two-component/four-profile matrix from its complete diff and records fresh independent results, exact reviewed revision and CI links in its description. This planning record does not certify that future revision. No source, test, dependency, CI or verification-policy change is part of this design.

The command parent owns admission, IPC, recovery, loop ownership and the adapter/configured-case boundary. The local-runtime/testing-runner assignment owns the transitive parent/registry/runner/SDK launch and closure boundary. Provider discovery, secret/context admission and direct-injection assertion contracts stay unchanged; their implementation changes are selected again under #215. Browser checks are not applicable to this design because no console interaction changes.

New lifetime implementation, installed-distribution process/filesystem cases and resource measurements are pending #215; actual runtime/testing integration is pending work items 7/14. Existing executable checks and final-revision CI remain mandatory for the design PR. No design-review result substitutes for that runtime evidence.
