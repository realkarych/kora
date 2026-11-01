# Process-scan refusal diagnostics

Issue [#561](https://github.com/realkarych/kora/issues/561) adds a closed diagnostic reason to an existing local-runtime refusal. It does not repair [#460](https://github.com/realkarych/kora/issues/460), qualify the failed #539 measurement, or establish a new termination or performance guarantee.

## Boundaries and ownership

The concrete native identity reader returns either the complete matching tuple or one `ProcessScanFailure`. Its existing `processes_carrying` method projects one detailed invocation back to the unchanged tuple/None interface. `IdentityReader` and provider/public interfaces are unchanged. The enum adds no resource owner, buffer, callback or persistent last-result state.

| Existing observation boundary | Diagnostic value |
|---|---|
| The reader is used by a different process | `reader_process_changed` |
| PID listing raises its already handled OSError or returns incomplete | `pid_listing_incomplete` |
| The outer per-PID scan deadline is reached | `scan_deadline` |
| Ownership or argv matching returns unknown | `owner_or_argv_unknown` |

The listing reason still combines capacity, native-list shape and lower deadline refusals. The ownership/argv reason does not distinguish access, decoding or individual syscalls. No PID, path, argv, errno, partial-match count or exception text leaves this boundary.

The runtime treats every enum as the same conservative `listing_incomplete` refusal. The reason travels through the current `SweepRefused` to the existing single reporting owner. It is emitted only for that outcome when present; separate pinning refusals and successful or unrelated outcomes omit it. The original error text, fence, complete-match accounting, scan/grace clocks, identity checks, exit watches, signals, disappearance confirmation and release owners are unchanged.

The measurement failure handler retains its original error and capture-failure cause. Its extracted projection selects the last observed retirement event, validates the existing safe fields and retains only an applicable exact closed string. Missing, malformed or inapplicable reasons become null without losing otherwise valid fields. It does not search backwards for an earlier valid event when the last event is invalid. Production result-to-event propagation is same-call; the measurement remains **last observed**, requiring separate attribution evidence.

## Verification scope

- Real platform regressions cover fork ownership refusal, the legacy conservative return and own-vector exclusion. Real capacity refusal retains the original error, fence and living carrier assertions before reopening the registry at ordinary capacity and confirming successful retirement without a stale reason.
- Real report/capture calls on one runtime instance cover each typed reason, reasonless listing refusal, unrelated refusal, success, fixed exception text, content exclusion and descriptor release.
- Parser regressions cover all closed reasons, inapplicable outcomes, missing and hostile values, invalid original fields, last-event selection and missing retirement evidence.
- Existing starting, cancellation, close and exact-release assertions remain active. The `scan_deadline` and `owner_or_argv_unknown` producer branches receive independent source review; these tests do not reproduce native failure causes.
- Focused checks and complete final-head CI supply execution evidence. Local-runtime diagnostic jobs must be inspected individually even though the workflow declares them nonblocking. No new measurement workload, profile, repeated campaign or unchanged-head rerun is introduced.

Independent correctness and security design reviews covered the reader-to-runtime and result-to-event-to-note boundaries against base `d54e1946d48d92159ff262c438bf57fd2d19db49`. Both passed before implementation. Their source results and executable outcomes are recorded in the delivery PR. No separate performance profile is selected because native calls, traversal, buffers, concurrency, resource ownership and numerical methods are unchanged; exact release remains blocking.

Local macOS/Python 3.11.17 checks passed before source handoff: 55 platform/starting/diagnostic cases and 75 existing registry/resource/architecture cases. Four host-fixture cases were deselected by the unchanged ordinary local selection; prepared Linux/macOS CI retains their coverage. After correcting a long fixture line and explicitly typing empty hostile parser inputs, the 34 affected cases passed again. Full Ruff/format, strict Pyright, Markdown lint and whitespace checks passed. Initial lint/type failures remain recorded in the PR; no assertion, deadline or configuration was weakened.

The locked local environment was synchronized with uv 0.11.7. Its installer does not support CI's `--require-build-hashes` option, so the first editable install command failed before building. The successful editable install used `--no-deps --no-build-isolation` with the already synchronized locked build group. Dependencies and CI commands were unchanged. Final-head CI remains required; no local resource measurement was run.

Security coverage concerns TM-06/TM-07 content exclusion, TM-09 identity/effect authority and TM-12 truthful evidence. Actors and cooperative-local limitations are unchanged, so no threat-model amendment is needed. The historical #460 diagnosis and failed #539 run retain their original outcomes and unknown native subcause. Further platform or scan-policy changes need their own plan and decision.
