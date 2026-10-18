# Structured logging plan review

Date: 2026-10-18. Scope: [issue #91](https://github.com/realkarych/kora/issues/91) and its [implementation plan](https://github.com/realkarych/kora/issues/91#issuecomment-6251727951), covering exactly sixteen documentation and verification-policy paths. Planning review passed; the project owner subsequently approved the direction and implementation plan on 2026-10-18, recorded in the [approval comment](https://github.com/realkarych/kora/issues/91#issuecomment-6251750074). This record does not attest to later PR wording, implementation review or runtime behavior.

## Reviewed inputs and independence

- Repository baseline: `0c1ee5bdc7f390509a42bf75191dde706924d15a`, inspected through a coordinator-created `git archive` snapshot of `origin/master`.
- Final plan SHA-256: `2557956afe412ad4e88695b7084318db6d8963133fb5460e4483cf99bed3c55a`.
- Issue body SHA-256: `9ec84f3c22244a0af1f8a0fc8a09f8a213daff9dbc7d31fa32602b27b63dabbb`.
- Initial plan SHA-256 before review amendments: `7875833f8b322b60d5034b95e80924a42fd4dc0d5e59280b9191243081b0db98`.

Three independent fresh-context Codex reviewers read the repository instructions and their respective [correctness](../../.claude/skills/kora-verify/references/correctness.md), [security](../../.claude/skills/kora-verify/references/security.md) and [performance](../../.claude/skills/kora-verify/references/performance.md) profiles, inspected affected contracts and remained read-only. They received the proposed scope and evidence rather than the implementation conversation or a desired verdict. Each re-read the complete amended plan and unchanged issue body, checking local hashes before returning its final assessment.

The coordinator fetched the published issue and plan and verified matching text; the published plan's raw hash matched the reviewed hash. Reviewers did not independently retrieve GitHub text or authenticate the archive against the remote. The [published review summary](https://github.com/realkarych/kora/issues/91#issuecomment-6251737569) retains the pre-approval status and evidence limitations. Independent review is evidence for the design, not an executable gate or a claim of deterministic instruction enforcement.

## Findings and disposition

| Review | Incorporated findings | Final planning verdict |
|---|---|---|
| Correctness | Preserve cached error bodies and their original request IDs while generating a fresh transport ID; exercise replay through another API instance. Require a single writer with framed bounded child channels, partial-write handling and slow-reader tests. Constrain new diagnostic metric dimensions without removing existing bounded operational dimensions. | Passed; no unresolved actionable findings in the final plan. |
| Security | Test exclusion of unregistered nonsecret body/prompt/transcript markers independently of secret masking, including framework/startup/error paths. Exercise real candidate and descendant forgery of diagnostic-shaped JSON, descriptor separation and launcher-bound context. Preserve the local runtime's same-user isolation limitation. | Passed; no unresolved actionable findings in the final plan. |
| Performance | Bound secret registration count/bytes, rotation, retained matcher state and matching work. Preserve coverage for output awaiting sanitization; fail required secret admission before exposure when coverage cannot be established. Test churn/exhaustion, count process and merger memory, and verify actual process exit without delaying execution stops. | Passed for owner review; no unresolved actionable findings in the final plan. |

All amendments stayed within the sixteen-path design/policy scope. Review found no material inconsistency with the issue or accepted baseline contracts. The final plan preserves the decided stack, separate streams, existing API outcome authority, dual-database obligations where applicable, Linux/macOS support and stricter baseline verification gates. It requires a component/event/context matrix, named activation tests and an owning issue for every pending row.

The coordinator also ran `npx --yes markdownlint-cli@0.45.0 --config baseline/.markdownlint.json plan.md issue.md` against the final planning inputs: exit 0. This was formatting evidence only. Repository tracked files were unchanged during planning; the scaffold inventory was inspected, not executed as proof of an unimplemented logging design.

## Remaining obligations

The design PR requires fresh independent reviews of its full final diff and every current scaffold/documentation CI check at its final committed revision. Its evidence belongs in the PR and must not be inferred from this planning record. Verification-policy changes require explicit owner review, and `docs/` wording requires line-by-line owner review before merge. Plan approval is not approval of unseen wording.

Runtime obligations remain **pending implementation**: schema/level/configuration checks, concurrency and context reset, request-ID replay, stdout separation, safe content and exceptions, real protected-runtime channel isolation, bounded redaction state, multiprocess framing, stalled-sink progress/shutdown, noise/loss accounting and measured overhead. [The normative specification](../specs/observability.md#7-component-coverage-and-activation) assigns activation ownership to issue #91 until named component implementation issues assume the corresponding rows. Closing the contract issue does not report those runtime checks as passed. No browser interaction is changed by this design; existing shell checks retain their current activation status.
