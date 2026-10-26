# StructuredOutput publication CI repairs

Checked: 2026-10-27. [Issue #246](https://github.com/realkarych/kora/issues/246) publishes the immutable StructuredOutput contract in [PR #254](https://github.com/realkarych/kora/pull/254). The project owner authorized repairing both observed CI blockers without weakening checks. The [repair plan](https://github.com/realkarych/kora/issues/246#issuecomment-6429572243) keeps runtime implementation and aggregate-limit decisions outside this change.

## Received bootstrap descriptors at test teardown

[CI 40414018517, Linux Python 3.11 general lane](https://github.com/realkarych/kora/actions/runs/40414018517/job/121891630990) failed `test_bridge_storage_reconstruction_closes_actual_receivers[cancel]` with `HelperError: effect_unsettled` from the test owner's final mutation release. Its retained second-group JUnit records one failure and 609 passes; the other groups exited successfully. The failure is retained as failed evidence.

The test owner starts bootstrap asynchronously even when the adapter never uses the lifetime. `IOHelper.execute()` can receive three SCM_RIGHTS descriptors before observing helper exit; `MutationOwner.bootstrap()` adopts them only after the execution returns successfully. Forced test teardown can therefore reap and close the helper while leaving received descriptors unadopted. The strict `release()` correctly refuses their retained owner.

A real configured probe pauses bootstrap after sending its response and descriptors. An owned FIFO lease and explicit condition wait ensure that all three descriptors are present while the helper is still active. On the original finalizer, disposal deterministically reports `effect_unsettled` with zero live helpers, zero pending mutations, one retained helper and three received descriptors. This reproduces the missing ownership transfer without sleeps, mocks or repeated campaigns.

The test finalizer now asserts the existing `MutationOwner.dispose_settled()` operation. It first refuses pending mutations or unclosed helpers, then closes unadopted descriptors and invokes the unchanged strict release. The regression proves actual process reaping, closed channel and anchor, three invalid descriptors, zero retained origins/helpers/pending work and cleanup tasks, and an unchanged `UNKNOWN` recovery result. Its fallback cleanup retains the probe, FIFO and descriptor owners through settlement even when setup or an assertion fails. The finalizer's five-second bound is unchanged. No production lifetime behavior, authority or recovery guarantee changes.

`test_failure_disposal_closes_unadopted_bootstrap_descriptors` failed on the original finalizer and passed after the repair. The existing cleanup, pending-helper, adapter-bridge and storage-bridge tests remain active. The separately recorded pre-identity parent-exit fixture gap in #274 is not reclassified or claimed fixed by this descriptor repair.

## Browser dependency installation route

[The browser job in the same run](https://github.com/realkarych/kora/actions/runs/40414018517/job/121891630679) was cancelled during setup. Ubuntu package acquisition from `azure.archive.ubuntu.com` fetched 25.9 MB in 579 seconds at 44.8 kB/s. Chrome installation began only at 22:50:26 UTC after the job started at 22:40:14; the ten-minute job deadline stopped it before browser tests ran. Missing browser artifacts and cancelled status remain non-passing evidence. Local browser success does not replace that job.

The browser job now requires its existing `/etc/apt/apt-mirrors.txt` and routes it to the [Ubuntu HTTPS archive](https://archive.ubuntu.com/ubuntu/). The [APT mirror transport documentation](https://manpages.ubuntu.com/manpages/noble/man1/apt-transport-mirror.1.html) defines the URI-per-line mirror list consumed by the existing `mirror+file` source. Ubuntu signing keys, package authentication, distribution sources and the separate security mirror remain unchanged. This changes the acquisition route, not the installed dependency set or trust principal; it makes no unmeasured throughput guarantee.

The full pinned `playwright install --with-deps chromium`, frozen frontend install, source checks/build, functional/resource browser suite, read-only GitHub permissions, mandatory artifact and ten-minute hard job deadline remain intact. `test_browser_setup_preserves_authenticated_installation_and_hard_checks` guards that complete setup/check/publication boundary. It failed against the old route and passed after the workflow repair. Actual hosted installation and browser execution must still complete successfully within the same deadline.

## Evidence and limits

The two new regressions failed before their corresponding repairs and passed afterward. The focused cleanup/pending-helper/bridge/storage-bridge and workflow selection passed 43 tests locally; final revision-matched command and CI results are recorded in the PR. Strict format/lint/type checks, complete ordinary tests, Linux/macOS, both metadata databases and all required final-head checks remain mandatory. Shared-host elapsed times are diagnostics; no new performance method, retry policy, deadline or acceptance threshold is introduced.

Accepted #306 storage-case directory/native-work ownership is integrated independently; this change does not duplicate it or activate #215. StructuredOutput schema/descriptor/vector bytes, the seven exact approved documentation fragments, VcsRef files and the canonical IR examples remain unchanged. The historical 628-second workflow failure remains failed; the subsequently approved whole-workflow feedback target does not waive the hard cleanup or browser obligations repaired here.
