# Bounded MVP verification

The project owner approved this contributor-policy amendment on 2026-10-29 under [issue #412](https://github.com/realkarych/kora/issues/412). Agent-developed MVP changes need a green final-revision CI and focused verification without repeated whole-pipeline campaigns.

## Replaced merge requirements

| Earlier requirement | Replacement |
|---|---|
| Ten consecutive complete successful ordinary CI executions for #311/#359 and #310/#384 | One successful complete ordinary CI execution on the final PR revision, including every mandatory job and supported environment |
| Fifty consecutive installed-listing test-file executions on each supported OS for #311/#359 | Existing deterministic observation, cancellation and collection regressions, together with ordinary Linux/macOS CI |
| Extra fresh full local-tooling or storage qualification demanded solely before merging an individual PR, including dependent refactors | Ordinary required behavior, integrity, recovery, cancellation, shutdown and exact owned-resource checks; existing nightly/manual exact-release qualification remains separate |
| Three serial interleaved base/head workflow pairs, pipeline median below 450 seconds, IR shard imbalance at most 30%, and each measurement no more than 30 seconds beyond the slowest Scaffold job for #271/#410 and the inherited #268 method | Current final-head CI and exact workload/evidence preservation; retained job/pipeline timings and balance comparisons are diagnostics against the existing ten-minute feedback target |

These replacements also apply to dependent open PRs whose descriptions inherited the superseded merge holds. Update their acceptance records explicitly; an old requirement must not remain an implicit blocker. Other genuine dependencies and unfinished issue acceptance still apply.

## Preserved checks and limitations

All mandatory CI jobs must pass on the final PR revision. Strict static checks, Linux/macOS, both metadata databases, real components, deterministic synchronization and the prohibition on mocks, skips and suppressed failures remain unchanged. Contract correctness, integrity, authorization, immutable identity, admission bounds, crash/replay recovery and actual resource release remain blocking. Exercise relevant failure, cancellation and shutdown paths. No component workload, assertion, hard test/job/child/cleanup deadline, dependency or runtime behavior changes in this amendment.

Repetition inside an existing behavioral or resource test is preserved. Scheduled/manual local-tooling and storage studies retain their current workloads, numerical assertions, evidence and exact-release qualification requirements. This amendment removes redundant PR campaigns, not those release obligations. A demonstrated live resource, corruption, authorization defect or unbounded growth still requires a fix; fewer executions do not establish stronger stability or leak-freedom guarantees.

Retain historical failures and their actual revisions. Do not retry an unexplained failed execution until it passes, discard adverse observations or relabel old evidence. Targeted local checks answer changed-input questions; one successful final-head CI supplies complete-suite evidence. Reuse supported independent conclusions when their relevant inputs match, and obtain only missing or affected review. Additional investigation must answer a concrete unresolved correctness or trust-boundary question rather than create another delivery stage.

After two unsuccessful repair iterations for one problem, stop that repair and ask the project owner for a decision. Independent work may continue. An unresolved required failure remains a merge blocker; escalation is not a waiver.
