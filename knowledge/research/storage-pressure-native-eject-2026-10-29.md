# Native release of owned macOS pressure images

This fixture implementation change is tracked by [issue #419](https://github.com/realkarych/kora/issues/419), following the bounded release-plan trigger in [#302](https://github.com/realkarych/kora/issues/302). The owner authorized routine fixes and bounded MVP verification on 2026-10-29. Production storage, public protocols, recovery manifests, supported platforms and privileges are unchanged.

## Failure and limits

[CI 40858024983](https://github.com/realkarych/kora/actions/runs/40858024983/job/123393868455), head `50890893e567a19963477ed20d500ae5fb317c15`, failed the final cleanup of `test_failed_unmount_retains_ownership_until_the_blocker_closes`. Its intentional blocker phase completed; after the child closed, `hdiutil detach` returned 16 with a busy eject. The volume was unmounted, the image/device binding unchanged, and the complete same-user census found no holder. Block and raw device identities remained bound. The artifact reports 207 passing cases and one failure, with no errors or skips. That execution remains failed; neither the ultimate dissenter nor a production-storage defect is established.

The local macOS manuals describe ordinary `hdiutil detach` and `diskutil eject` as similar disk-release operations. The replacement uses one ordinary `/usr/sbin/diskutil eject` on the freshly discovered, validated whole device. It adds no force option, fallback, retry, privilege or extra mutating command. A zero exit or success message alone does not establish release: existing post-discovery absence, private-directory validation and cleanup must still hold before the image is removed or resources are reported closed.

## Diagnostics and resource boundary

The existing journaled effect retains the same process identity, inflight/exit records, settlement, 30-second command deadline and combined 1 MiB stdout/stderr capture bound. Refusal remains a failure and keeps its ownership evidence. Raw stdout now accompanies raw stderr in private no-follow diagnostic files and the explicitly named retained artifacts. It is not rendered into the exception, diagnostic notes or operational logs; existing stderr rendering remains bounded.

`diskutil eject` has no documented structured refusal format or guarantee that every refusal includes a dissenter PID/status. Missing or ambiguous attribution remains unknown. Refusal text and reported PIDs confer no signalling or cleanup authority. This change preserves whatever the single native operation reports; it does not implement a new parser, guarantee complete attribution or identify the cause of the historical busy eject.

The existing real blocked-volume, unknown-image and recovery cases remain required. The failed-command test retains its two populations while checking both private streams and both persistence-failure paths, preserving the original error, exact exit status and bounded safe display. No workload, numerical assertion, hard deadline or qualification population is removed. One complete final-head CI is required under the [MVP verification policy](bounded-mvp-verification-2026-10-29.md); no repeated campaign is added.
