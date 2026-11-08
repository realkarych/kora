# Lychee release asset and offline link qualification, 2026-11-08

[Issue #1073](https://github.com/realkarych/kora/issues/1073) replaces the Lychee composite action's acquisition step while preserving the required Linux job, its deadline, the complete Markdown inventory and the offline destination configuration.

## Pinned inputs

| Input | Identity |
|---|---|
| Version and platform | Lychee 0.24.2, x86-64 Linux GNU |
| Release asset | [GitHub asset 409959156](https://api.github.com/repos/lycheeverse/lychee/releases/assets/409959156) |
| Archive size | 7,626,970 bytes |
| Archive SHA-256 | `1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a` |
| Member | `lychee-x86_64-unknown-linux-gnu/lychee` |
| Executable size | 19,827,680 bytes |
| Executable SHA-256 | `87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d` |

The authenticated investigation retrieved and inspected those exact Linux bytes. Unauthenticated local observations included a redirect and a separate HTTP refusal. The credential-free hosted qualification remains an acceptance check. The [release asset API](https://docs.github.com/en/rest/releases/assets#get-a-release-asset) describes the direct response and redirect alternatives.

## Verification boundary

The job reads the current Git NUL inventory. It gives curl and Lychee a private home and temporary directory without inherited credentials. Curl 8.4 or newer applies a transfer ceiling with or without a known response size. Each of the at most two transfer processes has its own curl timer and an outer watchdog. A redirected request stays within the admitted HTTPS asset host, and the URL goes to curl through private standard input. The archive must pass exact size and digest verification before tar inspection. Only the checked regular executable member is written.

The checker uses literal relative inputs. The positive total comes from the generated summary table, and the process must also succeed. The job retains the Markdown report with its run link, and pauses workflow command interpretation while Lychee and its report can emit filenames or link details. Fixed acquisition errors exclude response content and signed URLs.

The local behavior probe used installed Lychee 0.24.2 on macOS. The full repository inventory and a valid local anchor qualified. A missing file, missing anchor and zero-link inventory each refused. A separate inventory checked a URL-shaped filename, a filename containing glob metacharacters and filenames with spaces and a newline. It reported four links. An ignored broken-link glob decoy contributed no result, and the owned loopback HTTP server observed zero requests. Stop and resume records enclosed each generated report in the probe. The macOS probe does not establish hosted Linux acquisition.


The exact-input invocation includes hidden paths because Git may select hidden Markdown. A mixed inventory containing a hidden glob-like filename with a broken link originally reported only the good link. The amended invocation reported both selected links and rejected the broken one. Hidden and ordinary ignored decoys remained outside the inventory. Force-tracked paths still qualified for checking through the same literal inputs even when repository ignore rules matched them.


A selected `alias.md` symlink to a directory reproduced a different input expansion. The raw checker reported two links with hidden traversal enabled, including one from a hidden Markdown file outside the Git inventory. The final wrapper refuses a selected path that resolves to a directory before starting Lychee. This keeps an individual file alias from admitting recursive traversal.
