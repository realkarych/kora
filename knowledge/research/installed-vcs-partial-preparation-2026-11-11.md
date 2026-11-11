# Installed VCS partial preparation cleanup

Checked on 2026-11-11 for [#1355](https://github.com/realkarych/kora/issues/1355), part of [#1347](https://github.com/realkarych/kora/issues/1347). Base `08e0fcd`. This record follows the [Git step timing record](installed-vcs-git-timing-2026-11-07.md) and the [Git selection record](installed-vcs-git-selection-2026-11-07.md).

## Cited failures

Each failure cited in #1355 ran before the fix for its cause reached `master`:

| Run | Created (UTC) | Lane | Symptom | Fixed by | Fix merged (UTC) |
| --- | --- | --- | --- | --- | --- |
| [42252187432](https://github.com/realkarych/kora/actions/runs/42252187432) | 2026-11-06 16:25 | macOS 15, Python 3.11.17, remainder | first `init_remote` deadline, `refused` | [#932](https://github.com/realkarych/kora/pull/932) | 2026-11-07 16:35 |
| [42318769802](https://github.com/realkarych/kora/actions/runs/42318769802) | 2026-11-07 00:31 | same | same | #932 | 2026-11-07 16:35 |
| [42388182157](https://github.com/realkarych/kora/actions/runs/42388182157) | 2026-11-07 15:48 | same | same, timing `-1/5024/200` ms | #932 | 2026-11-07 16:35 |
| [42396129006](https://github.com/realkarych/kora/actions/runs/42396129006) | 2026-11-07 17:24 | Ubuntu 24.04, Python 3.11.17, remainder | zero-deadline case reports `cleanup_unknown` | [#942](https://github.com/realkarych/kora/pull/942) | 2026-11-07 18:02 |

The macOS deadline came from the `/usr/bin/git` launcher shim, which the fixture found on the child's `PATH=/bin:/usr/bin`; #932 selects Git from the configured `tool_path`. The Linux classification came from 0755 directories that Git created under the inherited umask after a failed preparation; the supervisor's strict startup-tree remover refuses them, and #942 runs fixture Git under umask 0077.

After #932, the first `init_remote` start on hosted macOS 15 (Python 3.11.17, conformance lane, `surface.xml` property `vcs_preparation_timing_first`) was 15–33 ms in eight sampled runs, against 2–3 s through the shim:

| Run | Artifact | Created (UTC) | First `init_remote` start / total / gap (ms) |
| --- | --- | --- | --- |
| [42877590481](https://github.com/realkarych/kora/actions/runs/42877590481) | 13794145654 | 2026-11-10 18:40 | `15 / 21 / 21` |
| [42879575868](https://github.com/realkarych/kora/actions/runs/42879575868) | 13793577957 | 2026-11-10 18:52 | `31 / 45 / 44` |
| [42883788838](https://github.com/realkarych/kora/actions/runs/42883788838) | 13795619883 | 2026-11-10 19:17 | `20 / 51 / 51` |
| [42891308738](https://github.com/realkarych/kora/actions/runs/42891308738) | 13800635556 | 2026-11-10 20:02 | `18 / 31 / 30` |
| [42891350194](https://github.com/realkarych/kora/actions/runs/42891350194) | 13798439611 | 2026-11-10 20:02 | `20 / 33 / 33` |
| [42895281531](https://github.com/realkarych/kora/actions/runs/42895281531) | 13800224401 | 2026-11-10 20:26 | `32 / 51 / 51` |
| [42900025185](https://github.com/realkarych/kora/actions/runs/42900025185) | 13804290095 | 2026-11-10 20:55 | `33 / 46 / 45` |
| [42903075931](https://github.com/realkarych/kora/actions/runs/42903075931) | 13804124512 | 2026-11-10 21:14 | `24 / 53 / 52` |

Between #942 merging and 2026-11-11 01:28 UTC, 459 `CI` runs were created: 223 succeeded and 236 failed or were cancelled. The failed and cancelled runs had 945 failed or cancelled scaffold test jobs. 543 of those jobs had a lane artifact, 545 artifacts from 187 runs in all; the rest stopped before upload. No retained log in those artifacts reports `test_installed_provider_supports_local_tooling[vcs-…]` or any `test_installed_vcs_*` case as failed or errored. The same search finds both cited failures in their artifacts. A lane stops at its first failure, so a case that never ran after an earlier failure is not covered.

## Remaining defect

When `prepare()` raises, the conformance bridge has no prepared adapter to clean up, so the supervisor's strict remover receives whatever Git left in the fixture's root. That remover refuses symbolic links (`foreign_tree_entry`) as well as non-0700 directories, and the invocation then reports `cleanup_unknown` instead of `refused`.

Git's repository initialization probes symbolic-link support inside the new repository: it creates a link `t??????` to `testing` and unlinks it immediately. A Git process killed at the fixture's deadline between those two calls leaves the link.

Local check on macOS 27.2.1 arm64 with Apple Git 2.54.0 (`/Library/Developer/CommandLineTools/usr/bin/git`) and umask 0077: a complete `git init -q --bare remote.git` took 9.1–11.2 ms (5th percentile to maximum of 200 runs). Of 4,000 runs killed with SIGKILL at a uniformly random point 4–10.5 ms after start, 1,456 had already finished and 2,544 were killed; 33 of the killed runs (1.3%) left a link such as `remote.git/tM4Anm5 -> testing`. No other entry kind, no non-0700 directory and no multiply linked file appeared. A separate 4,000 runs killed within the first 6 ms left nothing, so a zero-second deadline, which kills almost immediately, reaches the probe only when the kill is delayed, for example on a loaded runner. The probe is part of Git's own initialization, so the Linux and macOS paths are the same. This is a local measurement of one Git build; it does not establish a hosted failure rate.

## Change

The fixture removes its own partial preparation. After recording the root's identity, `prepare()` registers a disposal that checks the root's device, inode and owner through a no-follow descriptor and removes it with `shutil.rmtree` through the parent descriptor, which unlinks links without following them. The disposal is released when preparation completes; from then on the adapter's existing cleanup owns the root. If the disposal itself fails, its error propagates with the Git failure as context, the invocation still fails, and the supervisor classifies whatever remains. The supervisor's remover, the deadlines, the Git commands and the timing format are unchanged.

`test_installed_vcs_disposes_partial_preparation_with_git_link` occupies the remote destination with a dangling link to `testing`, and real Git refuses the occupied destination with `git_init_remote_nonzero`. A regular-file occupant would fail the same way, so the test alone does not show that the link was created. The run before the fixture change does: it reached `git_init_remote_nonzero` and then failed with `cleanup_unknown`. After the change, the invocation reports `refused` with closed cleanup and zero unresolved resources.

Two direct modes of the installed fixture's disposal checks cover the widened removal. `partial_links` places links to a directory and a file outside the root and a hard link to that file inside it; disposal removes the root and leaves the outside content and its link count intact. `partial_substitution` replaces the root with another directory after its identity was recorded; disposal refuses and both directories remain. The identity is checked twice, when the root is opened and again just before removal; removing both checks makes `partial_substitution` fail, while either one alone still refuses.

With this change the occupied `gone` case from #942 closes through fixture disposal instead of the supervisor's remover, so it no longer detects directory modes, and no remaining test fails if fixture Git loses umask 0077. Umask 0077 stays as untested defence in depth. After a successful preparation the adapter's cleanup removes the root without checking modes, so Git-created directories reach the supervisor's mode-checking remover only when the child is stopped before its own cleanup finishes or when the fixture's cleanup or disposal fails. The root itself is already a verified 0700 directory.
