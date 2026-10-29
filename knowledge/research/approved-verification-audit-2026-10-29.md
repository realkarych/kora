# Approved contributor-verification audit

The project owner approved the named changes below on 2026-10-29 and authorized implementation under [issue #434](https://github.com/realkarych/kora/issues/434). This amendment supersedes only the named verification-policy obligations. [AGENTS.md](../../AGENTS.md#development-verification) remains the canonical contributor policy; the [verification router](../../.claude/skills/kora-verify/SKILL.md) selects independent coverage.

## Numerical observations

The following completed-run comparisons become diagnostics. Retain signed observations, original reference values, comparison boundaries and explicit exceedance flags. They provide no passing latency, RSS, plateau or leak-freedom guarantee. Historical failures retain their original results.

| Measurement | Diagnostic comparison | Preserved blocking evidence |
|---|---|---|
| Pricing database workload | Authoritative post-cleanup total below 256 seconds | Both databases, every workload, integrity/recovery, actual process/container/factory release, bounded private/public evidence and protective deadlines |
| Metadata and logging campaigns | Completed totals below 120 and 300 seconds | All behavior, resource, admission, content and recovery checks |
| CEL main campaign and suite | Completed totals at most 150 and 180 seconds | Every evaluator/resource assertion and actual execution/cleanup deadlines |
| Compact CEL cases | Each cold/warm sample at most 100 milliseconds | Ten cases, four real calls each, exact results, logical cost/admission, ten-second children and 240-second suite watchdog |
| CEL diagnostic shutdown | Observed shutdown at most 50 milliseconds | Configured shutdown deadlines, one-second actual exits, writer/drain/full-pipe/queue/resource assertions |
| IR configuration workers | Twelve retained-process-heap deltas below 65,536 bytes | Zero observed live models, stable schemas, exact thread/descriptor release, complete workloads and counters |
| Individual IR validation, failure series and VcsRef actions | Post-GC process-heap deltas below 65,536 bytes | Correct acceptance/refusal, input limits and complete samples |
| Repeated Diagnostic and VcsRef value workloads | Process-heap checkpoint deltas below 65,536 bytes | All cycles, traced/untraced children, exact counters, outcomes and resource release |
| Accounting value lifecycle | Process-heap growth at most 262,144 bytes and RSS growth at most 8,388,608 bytes | All 126 children, 48 actions, 1,000 ordinary or three large cycles, zero observed live result models, arithmetic/refusals and exact resources |
| Protocol models, history/hash-chain rows, policy decisions and released policy cache | Named process-heap retained/recurring/released comparisons below 65,536 bytes | Semantic/fuel/input checks, descriptor release, policy peaks and cache capacity |
| Provider loading | Process-heap delta below 262,144 bytes | Loading/refusal/discovery, stable modules/descriptors, diagnostic drain and child completion |
| Logging configure/shutdown | Total heap below 1 MiB, later heap at most 64 KiB, RSS below 16 MiB | All cycles, thread/descriptor release, queue/registry/content limits, shutdown and actual exit |

The IR inventory retains all 359 test identities and 10,668 samples. Its 1,227 required proof entries now distinguish observations from resource-release checks; renamed memory-observation entries do not claim that the former numerical threshold passed. The reader independently derives configuration and accounting diagnostics from validated raw checkpoints and requires matching signed action/repeated-lifecycle observations, references and exceedance flags. Other measurements retain observations and exceedances in their report environment.

The protocol secret-registration 256 KiB and size-scaled ownership bounds, policy 16 MiB peaks and 5 MiB cache-entry bounds, and CEL's separately attributed/controlled heap/native/image checks remain blocking. Product quotas and deterministic work/space limits remain blocking. A demonstrated live owned resource, corruption, authorization failure or material unbounded component growth still requires a fix. No runtime cache is cleared or baseline recalibrated by this amendment.

## Verification execution and review

The pricing command's cooperative-cleanup default remains 60 seconds. Its adverse regression supplies a shorter positive duration while retaining real signals, expiration, kill/reap, descendants, containers and exact cleanup. This removes the full-duration test wait, not the default or escalation coverage.

One qualifying independent result covers each selected component/profile obligation. Matching local or native results suffice without an additional native invocation solely as a checkpoint. Correctness owns routine simplicity and test value; separate condensation addresses a named disputed architectural question. Logging selects security and performance only for their actual changed risks. Independence, affected-delta reassessment and required final-head CI remain mandatory.

The anti-suppression checker protects effective verification commands, discovery, strictness, warnings, overrides and allowance scopes. Named display-only additions and independent convenience scripts are allowed; unsafe lifecycle/pre/post hooks, unknown coverage-affecting keys and malformed types refuse. Both CI Python helpers have a narrowly scoped Ruff S603 exception: they require subprocess argument arrays for validated Git revisions, fixed HTTPS downloads and an integrity-checked private executable. Executable lookup is explicit; no shell, inline directive, broad exception, mock or global lint disable is introduced. The repository root is an explicit guarded pytest import path for standalone CI helper tests; those helpers enter strict Pyright coverage.

## CI admission

All twelve ordinary test lanes and three OS/Python/database environments remain. One successful complete expected collection per environment includes host-fixture cases and a checked summary/count. Each cohort and lane supplies actual successful JUnit identities. The validator requires a nonempty disjoint union equal to that complete inventory, exact run/attempt/revisions/database provenance, hashes, successful exits and the named local-runtime host fixtures. Missing, failed, skipped, cancelled, duplicate or mismatched evidence refuses admission.

One job validates the artifacts; three lightweight jobs retain the protected Scaffold status names and require its successful result and explicit verification output. Optional lock-contention and runner-spawn observations have a separate bounded job outside mandatory status dependencies. Their ordinary lock-deadline and resource-release regressions remain mandatory. Frontend setup remains wherever commands or transitive fixtures build or consume it, including storage's real installed wheel; local-runtime's direct Python lane omits it. Optional runtime jobs install the locked source editably so isolated runner children can import it without frontend builds.

The editorial path is deliberately conservative. Approved-base classifier code can recognize unchanged plain-prose wrapping or terminal-newline formatting in explicitly allowed non-contract documents. Semantic edits, code examples, links, Markdown structure, instructions, contracts, configuration, unknown paths, missing history, renames, deletions, mode changes and symlinks use full CI. Missing classification/proof never qualifies protected statuses. Documentation/example/link/diagram and security workflows remain required. This path does not classify every typo automatically.

Trivial editorial corrections may use a PR without a separate issue. Code, contracts, settings and substantive changes retain issues and approved plans. The 1,000-line business-logic cap remains; only a prior explicit owner decision may permit a demonstrated indivisible exception with complete tests, documentation, parity and review.

## Secret-scanner integrity and coverage

Gitleaks 8.30.1 runs directly rather than through the wrapper's truncated PR-commit API request. PR scanning binds the checkout to the actual head and scans all commits introduced after the base, with full history, side-parent traversal and first-parent merge patches. Missing revisions, shallow history and mismatched heads refuse. Push ranges and scheduled/manual full-history scans remain.

The Linux x64 archive SHA-256 is `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`; executable SHA-256 is `88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509`. The [official release API](https://api.github.com/repos/gitleaks/gitleaks/releases/tags/v8.30.1) supplied the archive digest. The downloaded archive matched it before its single regular executable was read and hashed. Version and both pins change together under review.

Downloads and cached archives are checked before extraction or execution; missing or mismatched pins refuse. A verified executable is copied into a private temporary directory and checked before launch. Scanner output is suppressed and redaction remains enabled. The published summary contains only scanner version, range, head and exit code, not matched content, paths or commit messages. A required Linux scanner probe exercises long histories, deleted side-branch secrets, actual head, merge-resolution introductions and a clean range. Read-only permissions and protective deadlines remain.

## Local environment

Superpowers and gstack were removed from the owner's local environment. The SessionStart sync hook now fetches and reports state without moving local branches, updating another worktree, merging or rebasing. A backup precedes the local edit. Real temporary-repository checks cover successful fetch and fetch failure while another clean worktree and local refs remain unchanged, including a configured fetch refspec targeting a local branch. An explicit validated remote-tracking destination and empty refmap prevent that configuration from moving local heads. The comments/docstrings prohibition and administrator bypass settings are unchanged.
