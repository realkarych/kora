# Local workload delivery verification — 2026-11-01

## Scope and authority

Issue #611 delivers the local workload to the ready runner and retains it without executing commands. The approved implementation plan is [the issue plan](https://github.com/realkarych/kora/issues/611#issuecomment-6560423246); [owner approval](https://github.com/realkarych/kora/issues/611#issuecomment-6560483401) accepts 16 total connections, 8 observers and the bounded same-user churn residual. [Documentation approval](https://github.com/realkarych/kora/issues/611#issuecomment-6560983549) covers the threat model and implementation plan wording.

The tested implementation is an uncommitted change over `2ecae3de0b568953aefad17e001d3045ac06bccf`, not a final PR revision. The refresh found upstream/master at `6134fd1`: #610 condenses historical scaffold research and #613 adds internal engine start-capacity checks. They do not change local runtime source or its governing contracts; their integration has not been tested here. Production changes total 544 added/deleted lines. Tests, measurements and documentation are excluded from that count.

## Coverage and ownership

| Changed boundary | Independent profile ownership | Blocking evidence |
|---|---|---|
| Registry digest/CAS → worker delivery → runner retention | Correctness: persisted identity, repeat, takeover and sticky expiry | Registry transition and real delivery tests; legacy file remains unchanged on refusal |
| Kernel identity/current head → attach and workload authority | Security: classification, fresh authorization and safe diagnostics | Wrong peer/head, inherited socket, original attach deadline, negative envelopes and DEBUG content checks |
| Connection/executor → receive/decode/drain/shutdown | Performance and correctness: actual resource lifetime | 16/8 admission, one decode slot, 64 KiB drain backing buffer, cancellation/shutdown and frame/message timeout followed by successful delivery |

Independent reviews use separate read-only contexts for the three profiles. The common boundary justifies grouping registry, worker and serving within each profile. Unchanged native attestation and process signaling delimit the scope. No metadata database or public API changes occur; browser and separate condensation reviews are not applicable. Journal, grants, command execution and durable recovery remain owned by #196/#197.

## Local evidence

Environment: macOS arm64, Python 3.11.17, frozen project environment. Local artifacts are retained under `/private/tmp/kora611-*`; they require attachment to a future PR and are not durable CI links.

- Runtime, registry and architecture suite: retained `final-local.log`, 1,346 passed and 10 host-fixture cases deselected. Later changes add tests and measurement instrumentation, not production behavior.
- `uv run --no-sync pytest tests/plugins/runtime/local/test_delivery.py tests/plugins/runtime/local/test_control_frames.py -q`: `final-delivery.log`, 109 passed.
- After DEBUG capture and explicit exited-event assertions, `uv run --no-sync pytest tests/plugins/runtime/local/test_delivery.py -q`: `final-diagnostics.log`, 45 passed. Each case checks descriptors, tasks, executor threads and zombie children after explicit completion.
- `ruff check --ignore-noqa .`, `ruff format --check .`, and `pyright` results are retained in the corresponding `final-*` logs. A line-length finding in the measurement probe was repaired; earlier failing output is not passing evidence.
- Complete final-head Linux/macOS CI and the ten host-fixture cases are **unverified**. These are required before merge. Local results do not replace them.

The saved `reviewed-inputs.json` records SHA-256 for each changed source/test/document input, and `reviewed.patch` preserves tracked changes. The measurement JSON has no commit revision; its producer is this uncommitted implementation, not upstream/master. The only subsequent probe edit wraps the `ps` invocation across lines without changing its operations. Measurement reuse is limited to unchanged producer behavior.

## Diagnostics and limitations

The existing codec probe runs three independent subprocess samples for maximal string-heavy, dense valid, hostile invalid and traced maximal workloads. Raw observations are in `codec-stages.json`; `codec-stages.log` records four passing test cases. These are diagnostics, not numerical acceptance gates.

Send and receive wall intervals overlap and must not be added. Send includes section hashing; receive CPU covers both coroutines in one process. The existing summary seconds remains receive plus decode CPU, excluding logical digest. Decode and logical digest have separate wall/CPU observations. Peak RSS is cumulative: no increment does not mean zero allocation. Current RSS is sampled after releasing the section while retaining the decoded workload and original sender payload; it includes allocator retention and is not the workload's live size. For refused input no digest runs and no workload is held. The codec omits attestation, registry and executor queues; its timings cannot be subtracted from real-runtime submit or called exact cross-process attribution.

Untraced string-heavy samples: send 74–76 ms, decode 162–168 ms, digest 18–20 ms; current RSS approximately 129 MB. Dense samples: send 76–79 ms, decode 1.30–1.33 s, digest 94–101 ms; current RSS 328–332 MB. Hostile invalid input was refused, with peak RSS 868–870 MB and current RSS 453–455 MB. The latter amplification remains visible under the accepted no-schema-precheck decision; this study establishes no new RSS or decode-time guarantee.

Separate real-runtime observations in `delivery-cost-final.log` report first/repeat delivery, worker-loop lag and runner RSS: string-heavy 0.551/0.101 s and 103,344 KiB; dense 1.892/0.243 s and 314,688 KiB. Maximum observed worker-loop lag was 15.4/49.9 ms respectively. These are local single-host observations without a new threshold. The original `delivery-cost.log` failed because logical hashing unnecessarily reapplied a generic 1 MiB JSON parser limit. The implementation now hashes the validated model directly; affected reruns passed. Preserve that historical failure rather than relabeling it.

A final PR must attach raw artifacts, actual independent review results, current strict-check results and final-revision CI. This record does not claim merge readiness.
