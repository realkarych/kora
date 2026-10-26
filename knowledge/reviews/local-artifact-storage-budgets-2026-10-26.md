# Local artifact storage numerical budget derivation

Status: proposed verification policy for [#255](https://github.com/realkarych/kora/issues/255), pending explicit owner review and final-revision CI. The [fixed configuration](../../tests/measurements/storage_budgets.json) is not approved by this record.

## Evidence and limitations

The derivation retains all 24 CI producer reports from four source revisions: 21 complete producers and three failed partial producers. Valid captured adverse observations from failed producers remain in resource and maximum-cycle envelopes. Failed producers and their campaigns remain failed. Legacy p95 uses only complete 100-cycle or three-cycle groups. No successful run was repeated solely for variance. The complete six-producer campaign at `0ab3b8c5e197b3d1309dde4285544bab46ae7f63` passed its storage gate; its general macOS scaffold failed the separately repaired short-command identity regression. These are derivation inputs, not final-head proof.

The [per-producer, per-workload resource observations](local-artifact-storage-budget-observations-2026-10-26.csv) preserve each maximum and positive growth excess. Three-cycle late windows are explicitly not applicable. Raw reports, checkpoints, snapshots, execution records and qualification exports remain in the linked campaign artifacts. Input hashes below bind this derivation to the inspected reports.

Python allocations after GC still increase between cycles 50 and 100. For the final raw workload these increases are 1,943/2,463/4,566 bytes on Linux and 2,545/2,237/2,507 bytes on macOS. Allocation frames and the bounded owner diagnostics do not establish retaining ownership or a plateau. All unexplained retention is charged. Passing these budgets means compliance with the declared workloads and allowances; it does not prove leak freedom.

## Prospective allowance

The proposed engineering tolerance is 25% above the adverse observed envelope, rounded upward by a fixed unit. It is a finite prospective regression allowance requiring owner approval, not a statistical variance estimate or confidence interval. Historical sources differ; their adverse observations conservatively remain in the envelope. Final committed evidence must also pass without automatic threshold adjustment. A zero observed growth envelope retains a zero growth allowance.

| Quantity | Upward rounding unit |
|---|---|
| Retained Python and tracer memory | 1 KiB |
| Native RSS and settled control storage | 4 KiB |
| Sampled traced Python peak | 64 KiB |
| Sampled unique-inode phase overhead | 4 KiB |
| Sampled allocated-block phase overhead | 1 MiB |
| Cycle and online inventory-page timings | 10 ms |
| Inventory query work | One SQLite VM step |

Every producer is evaluated independently. No pooling, averaging, compensation between workloads or thresholds derived during the gate is allowed. All comparisons are inclusive: an observation fails when it is greater than its configured ceiling. Fixed schema version 1, exact Linux/macOS coverage, all 14 workload entries and all applicable bounds are mandatory. Missing, unknown, duplicate, coercible or nonfinite configuration refuses. The configuration and evaluator participate in the existing source digest.

## Metric semantics

Absolute settled Python is the maximum captured `python_post_gc_bytes`, including the warmed baseline. Warmed growth compares every captured value with that workload's own baseline. Late endpoint growth is cycle 100 minus cycle 50; late excursion is the largest captured cycle after 50 minus cycle 50. Negative differences create no excess. Three-cycle groups omit only late-window arithmetic.

Python peak is the maximum recorded measured-cycle peak, separately bounding current traced allocations. Peaks reset every cycle but only declared resource points are captured. This is a sampled peak, not an observation of every unsampled cycle. Baseline Python peaks are excluded because they can inherit earlier workload peaks. Native current RSS is measured before explicit GC, sampled phase RSS covers declared phase observations, and cumulative process high-water RSS is separate. Phase-RSS warmed growth uses baseline current RSS. Tracer-owned memory has its own absolute and growth ceilings.

Settled database, journal, unique-file and allocated-block quantities each have absolute and growth limits. Sampled phase storage deducts its deterministic live-inode payload term before applying separately visible overhead: small raw is 3N; prepared is N+C with C=N+1 except C=N−1 at 256 MiB; alias is 2N; other completed raw/replay/expired traces are 2N; interrupted traces are N; empty lost-response is zero. Hard links count one inode. Residual allocated-block variation remains charged as overhead rather than being called another retained payload copy. Phase sampling does not establish an unobserved operation peak.

The existing legacy p95 formula remains `sorted[max(0, int(n * 0.95) - 1)]`. For three cycles this is the median. Every maximum workload therefore also has a mandatory maximum-of-all-three-cycle ceiling, including adverse valid partial cycles in its derivation. Online inventory ceilings apply to every `InventoryPage`, including ordinary, final/empty, restart, probe and concurrent pages. Offline seeding is excluded. VM steps measure the instrumented indexed query, not all surrounding API or transaction work.

Counter availability is checked separately from ceilings. Exact ownership identities and zero disposable jobs/readers/connections/rows/receipts/reservations/private payloads remain unchanged. The 480-second process deadline, exact sample/producer inventories and ten-minute whole-pipeline obligation remain mandatory. A budget cannot rehabilitate incomplete or invalid raw evidence.

## Resource envelope and proposed ceilings

All entries are bytes. Each cell is observed envelope / proposed ceiling.

| Platform | Metric | Absolute | Warmed growth | Late endpoint growth | Late excursion growth |
|---|---|---:|---:|---:|---:|
| darwin | `allocated_bytes` | 61,440 / 77,824 | 0 / 0 | 0 / 0 | 0 / 0 |
| darwin | `cumulative_maxrss_bytes` | 68,632,576 / 85,790,720 | 4,292,608 / 5,365,760 | 1,409,024 / 1,761,280 | 1,409,024 / 1,761,280 |
| darwin | `database_bytes` | 57,344 / 73,728 | 0 / 0 | 0 / 0 | 0 / 0 |
| darwin | `journal_bytes` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| darwin | `phase_peak_rss_bytes` | 68,632,576 / 85,790,720 | 4,341,760 / 5,427,200 | 1,409,024 / 1,761,280 | 1,409,024 / 1,761,280 |
| darwin | `python_post_gc_bytes` | 110,633 / 139,264 | 30,017 / 37,888 | 5,294 / 7,168 | 5,294 / 7,168 |
| darwin | `rss_bytes` | 68,468,736 / 85,585,920 | 4,177,920 / 5,222,400 | 1,409,024 / 1,761,280 | 1,409,024 / 1,761,280 |
| darwin | `tracer_bytes` | 484,816 / 606,208 | 198,400 / 248,832 | 5,408 / 7,168 | 5,632 / 7,168 |
| darwin | `unique_bytes` | 57,905 / 73,728 | 0 / 0 | 0 / 0 | 0 / 0 |
| linux | `allocated_bytes` | 65,536 / 81,920 | 4,096 / 8,192 | 0 / 0 | 0 / 0 |
| linux | `cumulative_maxrss_bytes` | 113,668,096 / 142,086,144 | 0 / 0 | 0 / 0 | 0 / 0 |
| linux | `database_bytes` | 57,344 / 73,728 | 0 / 0 | 0 / 0 | 0 / 0 |
| linux | `journal_bytes` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| linux | `phase_peak_rss_bytes` | 75,128,832 / 93,913,088 | 7,200,768 / 9,003,008 | 700,416 / 876,544 | 700,416 / 876,544 |
| linux | `python_post_gc_bytes` | 104,059 / 131,072 | 33,220 / 41,984 | 5,695 / 7,168 | 5,695 / 7,168 |
| linux | `rss_bytes` | 75,038,720 / 93,798,400 | 7,122,944 / 8,904,704 | 684,032 / 856,064 | 684,032 / 856,064 |
| linux | `tracer_bytes` | 481,104 / 602,112 | 236,288 / 295,936 | 25,600 / 32,768 | 25,600 / 32,768 |
| linux | `unique_bytes` | 57,851 / 73,728 | 0 / 0 | 0 / 0 | 0 / 0 |

| Platform | Additional quantity | Observed | Proposed ceiling |
|---|---|---:|---:|
| darwin | `python_peak_bytes` | 782339 | 983040 |
| darwin | `phase_unique_overhead_bytes` | 127537 | 159744 |
| darwin | `phase_allocated_overhead_bytes` | 16715777 | 20971520 |
| darwin | `inventory_query_steps` | 783 | 979 |
| darwin | `inventory_page_seconds` | 0.2729424590000349 | 0.35000000000000003 |
| linux | `python_peak_bytes` | 1666297 | 2097152 |
| linux | `phase_unique_overhead_bytes` | 127483 | 159744 |
| linux | `phase_allocated_overhead_bytes` | 131073 | 1048576 |
| linux | `inventory_query_steps` | 783 | 979 |
| linux | `inventory_page_seconds` | 0.11406064700000229 | 0.15 |

## Timing envelope and proposed ceilings

All values are seconds. Maximum-cycle ceilings are not applicable to 100-cycle workloads.

| Platform | Workload | Observed legacy p95 | Proposed legacy p95 | Observed maximum cycle | Proposed maximum cycle |
|---|---|---:|---:|---:|---:|
| darwin | `storage.alias.16777216` | 1.470819876 | 1.84 | 1.806753209 | 2.26 |
| darwin | `storage.alias.268435456` | 15.849714667 | 19.82 | 17.852278626 | 22.32 |
| darwin | `storage.alias.65536` | 0.752168250 | 0.95 | not applicable | not applicable |
| darwin | `storage.cancelled.65536` | 0.191368708 | 0.24 | not applicable | not applicable |
| darwin | `storage.expired.1` | 0.495332500 | 0.62 | not applicable | not applicable |
| darwin | `storage.lost_response.0` | 0.539817791 | 0.68 | not applicable | not applicable |
| darwin | `storage.prepared.16777216` | 1.513405917 | 1.90 | 1.699670626 | 2.13 |
| darwin | `storage.prepared.268435456` | 17.576052250 | 21.98 | 17.850724166 | 22.32 |
| darwin | `storage.prepared.65536` | 0.740540792 | 0.93 | not applicable | not applicable |
| darwin | `storage.raw.16777216` | 0.841213584 | 1.06 | 1.193773250 | 1.50 |
| darwin | `storage.raw.268435456` | 9.788150792 | 12.24 | 11.527918125 | 14.41 |
| darwin | `storage.raw.65536` | 1.014455291 | 1.27 | not applicable | not applicable |
| darwin | `storage.replay_conflict.65536` | 0.516028876 | 0.65 | not applicable | not applicable |
| darwin | `storage.source_error.65536` | 0.208351958 | 0.27 | not applicable | not applicable |
| linux | `storage.alias.16777216` | 0.969938975 | 1.22 | 1.403759098 | 1.76 |
| linux | `storage.alias.268435456` | 11.512412008 | 14.40 | 12.074599148 | 15.10 |
| linux | `storage.alias.65536` | 0.988565665 | 1.24 | not applicable | not applicable |
| linux | `storage.cancelled.65536` | 0.390587739 | 0.49 | not applicable | not applicable |
| linux | `storage.expired.1` | 0.531215764 | 0.67 | not applicable | not applicable |
| linux | `storage.lost_response.0` | 0.666452062 | 0.84 | not applicable | not applicable |
| linux | `storage.prepared.16777216` | 0.902469567 | 1.13 | 1.791046909 | 2.24 |
| linux | `storage.prepared.268435456` | 10.925917148 | 13.66 | 11.673567748 | 14.60 |
| linux | `storage.prepared.65536` | 1.038079228 | 1.30 | not applicable | not applicable |
| linux | `storage.raw.16777216` | 0.731047083 | 0.92 | 0.807374429 | 1.01 |
| linux | `storage.raw.268435456` | 7.361965815 | 9.21 | 8.072063440 | 10.10 |
| linux | `storage.raw.65536` | 1.313229179 | 1.65 | not applicable | not applicable |
| linux | `storage.replay_conflict.65536` | 0.485425507 | 0.61 | not applicable | not applicable |
| linux | `storage.source_error.65536` | 0.286598342 | 0.36 | not applicable | not applicable |

## Raw input provenance

Campaign artifacts: [9748347](https://github.com/realkarych/kora/actions/runs/40048330495), [cc81bd3](https://github.com/realkarych/kora/actions/runs/40049708015), [54f1f1e](https://github.com/realkarych/kora/actions/runs/40237348393), [0ab3b8c](https://github.com/realkarych/kora/actions/runs/40239508649).

| Report | Source/status | SHA-256 |
|---|---|---|
| `ci-9748347/linux-0/report.json` | `9748347: complete` | `118f0c6acf881f8cce1d0ff32c10deed3fd3563a2bd8c11cf7678b8a66233132` |
| `ci-9748347/linux-1/report.json` | `9748347: complete` | `5b0dfc4dc93734e5664bea2434e34dc63a57bab772076bb14227f4a8d589e3e5` |
| `ci-9748347/linux-2/report.json` | `9748347: complete` | `19dfaec664b08b9919c03d926a915dbefdde311705ff8f28ebcec109e99eb9ae` |
| `ci-9748347/macos/storage-9748347427b3745298b571bfd93b2456f8c02671-darwin-0/report.json` | `9748347: complete` | `ba33323122c85afdadb7c5129af32fe035cc066a3ed132a0edeb61ee5800411c` |
| `ci-9748347/macos/storage-9748347427b3745298b571bfd93b2456f8c02671-darwin-1/report.json` | `9748347: complete` | `fb647bf3ff2a965d4ddea268d65550de67a518b121d49a05b6d8ab6351826b1b` |
| `ci-9748347/macos/storage-9748347427b3745298b571bfd93b2456f8c02671-darwin-2/partial-report.json` | `9748347: failed partial` | `efd757c08cd7db9f3cf4a696d1e78b3bed6908893821d9c7a80277fc5a6ef1dd` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-darwin-1/report.json` | `cc81bd3: complete` | `ce5c81641df45f264561c9afb403e99063b7fa2a036d4c62c29d14480eddf8c0` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-darwin-2/report.json` | `cc81bd3: complete` | `6f78406bb46ce532f2ddf84dce852074cfd948252d199bb5487d279f9098930c` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-linux-0/report.json` | `cc81bd3: complete` | `2f769978bdf63c86e5bea990322f11ba1c6acf7142f3c947452b7c08e499abd7` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-linux-1/report.json` | `cc81bd3: complete` | `85f76ffeaba19a31db79ead5a77a27d9d9aff48e017f63837d6b0880d80f4b0d` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-linux-2/report.json` | `cc81bd3: complete` | `9a540b4843c873d76c543d87047aeae040194be17fa8bd0c4407d1cb9d1ffaa2` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-darwin-0/partial-report.json` | `cc81bd3: failed partial` | `a042b7a40d07c8e705a1b8a19bc60946bc9e72f769f3629d2874cff4702c3434` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-darwin-0/report.json` | `54f1f1e: complete` | `73b5bcd397cbc2331240fa858826871d9a3347c722c4a0dab8145b58b98b74a6` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-darwin-1/report.json` | `54f1f1e: complete` | `5d852ce166c2e448fd977733a39dce47ba7a087a24212c281d8f3dcc39bd47f2` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-linux-0/report.json` | `54f1f1e: complete` | `1c071325d5ed9cb32f6f37764f1442b2a61e9184ca44d2b57948f9d2210cb1a7` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-linux-1/report.json` | `54f1f1e: complete` | `237813deebf7b43b712bcd739c9199178ef434262cb371c25d9d1d8ae212028f` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-linux-2/report.json` | `54f1f1e: complete` | `a0c2d3630e59df9faefe97f7a254f0980538554fc0308d5caae2da32693e8ae5` |
| `ci-54f1f1e/numeric/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-darwin-2/partial-report.json` | `54f1f1e: failed partial` | `a26a76fdc608d2aec4a763522fbbb7a22eb6340bac52885f394a33822db89cf6` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-darwin-0/report.json` | `0ab3b8c: complete` | `275575614202aee770cd9526a228088d2afea250c5b4ee184f176783e5747069` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-darwin-1/report.json` | `0ab3b8c: complete` | `a8032f36f31c5c3c3813df585248c0e4fed3ed1da3ccd7de961e4fa387f41fde` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-darwin-2/report.json` | `0ab3b8c: complete` | `3d11174b8588f1ff4dff81ac1d8f4f989bdf1153c7ffc3fe85d9291b3f168eb3` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-linux-0/report.json` | `0ab3b8c: complete` | `2097364c39baffddc72811d043f370603be6853b38b230cd6766c5907aaa0ff3` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-linux-1/report.json` | `0ab3b8c: complete` | `a9dd62073f3ddfbd015845873a6ee7c4ed071735b55d9d2651d2ffd28f868651` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-linux-2/report.json` | `0ab3b8c: complete` | `559505c3a2ffd91ea0dc01c3acb84b9f5c110aad4e7aa787f8d17df9b5df66dc` |
| `ci-9748347/linux-qualification/boundaries.json` | `9748347427b3745298b571bfd93b2456f8c02671: {"error": 0, "failure": 9, "skipped": 0, "testcase": 38}` | `ca8e45eb03a53707cc950a35bf811d45acea8a62cb9286f4d54b37ed5257ad45` |
| Paired qualification JUnit | `linux` | `ab07765300fa1e147003b93dee92b10c7645c3e30fbebb35e8fe6d62ef66b05d` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-linux-qualification/boundaries.json` | `cc81bd396f798b65ec665081213f9619b8af4dba: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `19ffaddf728c0e52eedb589fd5715704aff3f0c455b9c71cd2c5a8cee768966f` |
| Paired qualification JUnit | `linux` | `81fb8c6948ddc32fc1c8b50901f514eb5832685cedfba79d407d4a19fa1bf545` |
| `ci-54f1f1e/all/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-linux-qualification/boundaries.json` | `54f1f1e9f86553affd9a02fe38222855b6ca92ad: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `d1a9477f08ff112c1eea6b11556d798abba90ee39d10bc81c59f389842a492a2` |
| Paired qualification JUnit | `linux` | `97216de19bfc85598c590a8d2727ad800bcbb2e65409e6dd10e932df7307048f` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-linux-qualification/boundaries.json` | `0ab3b8c5e197b3d1309dde4285544bab46ae7f63: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `6f2fd76be81e6c0774f3b106e0d2e2fef45b5892f52e35d8b59c94b9de692d41` |
| Paired qualification JUnit | `linux` | `9c0e5468a1115560eaf45550355852387c1d84127670f5339b2f2bacf07cd173` |
| `ci-9748347/macos/storage-9748347427b3745298b571bfd93b2456f8c02671-darwin-qualification/boundaries.json` | `9748347427b3745298b571bfd93b2456f8c02671: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `c6289ba39eed963b313130b4bb5f67eb069bc55e4847c7ce1d152f4ba0c09d11` |
| Paired qualification JUnit | `darwin` | `a96ac022bc36159999e9344a42bf596ad826cf413c222cda400172e3c831094f` |
| `ci-cc81bd3/all/storage-cc81bd396f798b65ec665081213f9619b8af4dba-darwin-qualification/boundaries.json` | `cc81bd396f798b65ec665081213f9619b8af4dba: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `39acea07fd0d2c3d47d40275364cccb9829452374fcf4711743e0051f22ee96b` |
| Paired qualification JUnit | `darwin` | `5cdcf60ab78b384779d4fdefb15facd64be2b45d6d631f54144bdec18bf13426` |
| `ci-54f1f1e/all/storage-54f1f1e9f86553affd9a02fe38222855b6ca92ad-darwin-qualification/boundaries.json` | `54f1f1e9f86553affd9a02fe38222855b6ca92ad: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `74b04eaf92b7bed5ac2b3598aae37ce45576d2faf82f6c6383d0a3e7edfe6773` |
| Paired qualification JUnit | `darwin` | `aa299297eeacdd18394268acf2e58d89454fbd3463f7ceb8dc7e8de8b0c6408b` |
| `ci-0ab3b8c/all/storage-0ab3b8c5e197b3d1309dde4285544bab46ae7f63-darwin-qualification/boundaries.json` | `0ab3b8c5e197b3d1309dde4285544bab46ae7f63: {"error": 0, "failure": 0, "skipped": 0, "testcase": 38}` | `4320ad23942dfd2d0afe8d8d940d7b1b2da31fad4f7b82e734a9b405f98fe8dc` |
| Paired qualification JUnit | `darwin` | `599140ddc80e5ac35f519a83c30029e8a15b8105a544031573bbd23119b3c18c` |

Observation CSV SHA-256: `5887e3dd6cbe91684724f38815980e29fd074bec13ec8bfc4fdf0cabae100168`.

## Verification status

Typed configuration, arithmetic and refusal tests are implemented. Independent assessments and exact numerical owner approval remain pending. Final evidence must match the committed source/configuration digest and revision. This record does not approve a status-protection or automatic-review setting.

## Required-status activation proposal

The existing master protection was read on 2026-10-26. The proposed change preserves `strict: false` and every existing required context with GitHub Actions app ID 15368, and adds only `Required storage evidence` for that same app. Linear history and the separate active CodeQL ruleset remain unchanged. Explicit owner review is pending; this record does not apply repository settings. Re-read protection immediately before applying the addition and preserve any subsequently accepted contexts.

| Required context after the proposed addition |
|---|
| Markdown lint |
| Links and anchors |
| Mermaid diagrams |
| Example graph documents |
| GitHub templates |
| Scaffold (ubuntu-24.04, Python 3.11.17) |
| Scaffold (ubuntu-24.04, Python 3.15.0) |
| Scaffold (macos-15, Python 3.11.17) |
| CodeQL (python) |
| CodeQL (javascript-typescript) |
| CodeQL (actions) |
| Secret scan |
| Dependencies introduced by the pull request / osv-scan |
| CEL evaluator lifetime |
| Console browser checks |
| Required storage evidence |

## Owner-approved completion disposition

The owner approved the named [#255 completion amendment](https://github.com/realkarych/kora/issues/255#issuecomment-6405709610) on 2026-10-26. The table and retained observations above are historical derivation inputs, not a requirement to promote every numerical reference to a product guarantee. The configuration remains unchanged. Process-wide Python/tracer/RSS growth and cycle/inventory elapsed timings are diagnostics with raw values, references and exceedance flags. Captured Python peak/current, every physical storage bound, actual indexed query work, exact closure, finite quotas, source/counter inventories and all deadlines remain blocking. No old failed campaign is relabeled passed, and no general leak-freedom/native-RSS/throughput claim is added. [The current implementation record](local-artifact-storage-2026-10-23.md#approved-completion-scope-and-verification-interpretation) identifies retained guarantees and the finite follow-up.

The same approved plan authorizes the single required-status addition described above. Application remains separate from this historical proposal and must re-read/preserve current protection immediately before adding Required storage evidence for Actions app15368. Final settings and source-matched CI evidence are recorded in PR #258.
