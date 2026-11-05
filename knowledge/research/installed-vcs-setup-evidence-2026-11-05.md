# Installed VCS preparation evidence

Checked on 2026-11-05 for [#776](https://github.com/realkarych/kora/issues/776).
Base `536020e9e7188b7a3664a2b2855e5392c7837969`, executable source
`d6af0d0a7bb510842308a33a3d2228e89db88b23`. Two test/fixture files, 297
additions and 26 deletions, zero production. The complete executable patch
SHA-256 is `71216b1acfdccfb034952c8250124e54ed348c13a260bfed6acadd1fedf1352d`.
This research record changes no executable input.

## Original failure and question

[PR #771 CI 42089714439](https://github.com/realkarych/kora/actions/runs/42089714439),
attempt 1, failed the macOS Python 3.11.17 remainder integration group at
`test_installed_provider_supports_local_tooling[vcs-fixtures_vcs-None-13]`.
The requested feature was `49ca72fe78f2aebef2cc82ec26e16ce8bf763efa`;
checkout `a120708197d0642c458f96e2a7cf54133da67b3a` combined target
`536020e9e7188b7a3664a2b2855e5392c7837969` and that feature into tree
`46755b984a1b558fa1694494d47f42579e684e57`. The feature changes only
three knowledge files. The target's saved retry changes affect no VCS fixture,
CLI command, lifetime owner, dependency or CI implementation.

The failed artifact 13444509129 has an integration XML SHA-256
`2ce65010c9a99f4c8e486c3a6decd9a190c7b58fb079c4502bd977ddaa79bfdd`.
CLI exit was one, `refused`, completed zero/expected thirteen, with actual
CLOSED and zero unresolved resources. Provider-loaded evidence and the expected
case count do not establish fixture preparation, construction or exercise.
Integration stopped with one failure among 283 cases; measurements and other
passed 2,172 and 9,565. All conformance jobs passed. Five dependent gates
failed because a successful macOS remainder receipt was absent; total 34
successful and six failed jobs. No hard job timeout or first-group lifetime
stop occurred. Whole workflow 879 seconds is diagnostic.

One focused unchanged local diagnostic passed the same installed VCS case,
macOS 27.2 arm64/Python 3.11.17 with uv 0.13.1 and pnpm 12.14.0. XML suite
12.862 seconds, case 12.831 seconds; hosted failed case 12.630 seconds. These
unlike-host times do not attribute the failure. Initial local setup lacked the
existing build group and exact tool versions and did not execute the case;
documented locked setup corrected that. The hosted failure remains failed and
unattributed. There was no unchanged CI retry.

The named evidence gap is distinguishing observed preparation entry and
completion from a known failure in one of seven native Git steps. A fixture
observation adds this distinction without guessing a timeout or altering the
original work/closure result. Missing entry is unobserved, not proof that
preparation never ran. Completed preparation precedes provider construction
and does not establish any of the thirteen cases passed.

## Ownership and observed states

The parent test creates a private canonical 0700 directory and 0600 regular
single-link file, pins the original close-on-exec descriptor through the child
invocation, and passes exact device/inode through existing fixture data. The
child opens the supplied leaf relative to a no-follow directory anchor with
nonblocking/no-follow/close-on-exec flags, matches actual descriptor identity,
UID/type/mode/link and exact prefix size, and checks a bounded append and short
write. It closes the leaf and directory descriptors before continuing Git.
The parent independently verifies its retained descriptor and reads at most
129 bytes for a 128-byte record cap. Only closed byte codes are decoded and
reported. Invalid, incomplete or unavailable evidence establishes no cause.
The bound assumes the one cooperative installed fixture writer; it is not
hostile same-user containment or a production file authority.

At most two short records hold observed entry and completion or the known
Git step with a typed deadline, positive nonzero, signal or OS I/O reason.
Unknown failures preserve only observed prefix. Sink errors are advisory and
never replace the original typed Git exception or suppress Git work. The
original seven actual commands, inputs/order and five-second command deadlines
remain, as do CLI/invocation/cleanup/job limits, case counts and workloads.
No observation is opened for fixture users without explicit selection. There
is no new task, process, lock, production diagnostic or public result field.

An occupied regular remote leaf provokes real first-step Git nonzero in the
registered startup tree. This happens before adapter preparation returns;
the existing parent lifetime owner closes the native domain and removes that
owned tree. Actual CLOSED/zero unresolved and no retained journal remain
asserted. This covers the early private directory/file shape, not every late
native Git directory or failure. Neither raw argv leaf nor the distinct
nonsecret configuration marker enters public outputs or new phase records.

The replacement case keeps the original inode under another name and creates
a different regular file of the same mode at the selected leaf. The actual
installed VCS run still completes all thirteen with CLOSED, and both leaves
remain empty. The parent observes no entry from its original descriptor. This
proves refusal of the substituted diagnostic sink without making that refusal
execution authority. It does not attribute the historical failure.

## Local evidence

Documented locked build setup used build group, PostgreSQL and CEL extras, exact
uv 0.13.1 and pnpm 12.14.0, and Python 3.11.17 on macOS 27.2 arm64. The fixture
builds and installs a real core wheel and external fixture without code-host or
model calls. One precommit focused run used a byte-identical executable patch:

```sh
npm exec --yes --package=pnpm@12.14.0 -- uvx --from uv==0.13.1 uv run --no-sync pytest -q \
  'tests/cli/test_conformance.py::test_installed_provider_supports_local_tooling[vcs-fixtures_vcs-None-13]' \
  tests/cli/test_conformance.py::test_installed_vcs_reports_git_preparation_refusal_without_exposing_inputs \
  tests/cli/test_conformance.py::test_installed_vcs_ignores_replaced_preparation_evidence_file \
  --junitxml=/tmp/kora-776-focused.xml
```

Exit zero, three passes, no failures/errors/skips. Wall 16.61 seconds, XML
16.613. SHA-256 `04ad16a421e93bdbe842e6d966af082c2a5ed845c302a0cead99e26ac84f9b88`.
Both ordinary success and replacement completed thirteen actual checks; the
occupied-remote case refused with zero/thirteen, actual CLOSED and the fixed
nonzero step. The unchanged neighboring cases run without an observer.

```sh
KORA_TEST_DATABASES=sqlite npm exec --yes --package=pnpm@12.14.0 -- \
  uvx --from uv==0.13.1 uv run --no-sync pytest -q tests/architecture \
  tests/conformance/test_installed_conformance.py::test_installed_vcs_rejects_alternate_executor_receivers \
  tests/conformance/test_installed_conformance.py::test_installed_vcs_retains_root_until_cancelled_native_work_finishes \
  --junitxml=/tmp/kora-776-local.xml
```

At clean executable source, exit zero: 934 passes, 930 architecture and four
installed receiver/cancel cases, no failures/errors/skips. Wall 50.82 seconds,
XML 50.594. SHA-256 `73551b431cac43a045e8ee5bb9a4af7ae7e63a96d93988a89aedc8dd73e01d0e`.
Both invocations used `KORA_BUILD_CONSTRAINT_DIR=/tmp/kora-771-build-constraints`
from the documented locked setup.
JUnit does not embed Git/command provenance; association is coordinator-recorded.
Timings remain diagnostics, without speed or general stability claims.

Scoped Ruff format/check with ignore-noqa, strict Pyright and diff whitespace
passed for both files. Import ordering and deprecated contextmanager return
typing were corrected during authoring; no checks or settings were weakened.

## Review and follow-up

Independent correctness/security passed the exact design and two-file
implementation plus actual retained focused/neighbor JUnit. The source checks
retain advisory observation, controlled descriptor identity, native failure and
cleanup obligations. No reviewer executed candidate code, installed, edited,
published or delegated. Reviewer/evidence provenance and final-head CI remain
with the implementing issue/PR.

TM-06/TM-07 cover fixed content-safe observation; TM-09/TM-10 actual owned
closure and byte cap; TM-11 descriptor substitution; TM-12 evidence integrity.
No production authority, threat-model edit, new numerical method, deadline,
platform or gate change. Existing release qualification, first-group lifetime
stop and two-unsuccessful-repair owner rules remain. The new observation changes
the fixture input; original failure remains failed and unattributed. After
actual green fixture merge, #771 requires its own complete final-head CI.
If the failure repeats, use only the observed stage/reason to select a bounded
cause-specific follow-up, without retrying unchanged until green.

Latest successful master audit 41297342266 is partial pricing/CEL only, source
`6b6d2bcbba8372114cb4f2429cc8c1102fee0be1`. Later 42013930066 at
`3f26b587bc6697dd5ece88e4fda9580b439aa3eb` failed before mutation execution.
No applicable VCS fixture result or additional audit campaign is supplied.
