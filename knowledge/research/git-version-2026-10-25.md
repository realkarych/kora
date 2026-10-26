# Minimum Git version for the slice-1 git provider

Checked on 2026-10-25 for [issue #159](https://github.com/realkarych/kora/issues/159).

The git provider (`kora.plugins.vcs.git`) refuses at start-up a Git older than 2.43.0, as `config_error`. The version check runs on the absolute path resolved from the configured tool search path, and every plan step runs that same path.

| option the provider uses | first release |
|---|---|
| `attr.tree`, set to the empty tree so no attributes are read from published trees | 2.43.0 ([v2.43.0 announcement](https://lkml.rescloud.iu.edu/2311.2/03909.html), topic `jc/attr-tree-config`) |
| `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>` | 2.31.0 |
| `--end-of-options` on `rev-parse`, `fetch`, `push` and `ls-remote` | 2.24.0 and later |
| `git bundle create --quiet` | 2.31.0 |
| `safe.directory` | 2.35.2 |
| `core.fsmonitor` as a boolean | 2.36.0 |

The newest requirement is `attr.tree`, so 2.43.0 is the minimum. Git 2.45.3 changed the default of `attr.tree` in bare repositories back to no tree ([2.45.3 release notes](https://www.kernel.org/pub/software/scm/git/docs/RelNotes/2.45.3.adoc)); the provider always sets it explicitly, so the default does not matter.

Versions seen on 2026-10-25:

- macOS development machine: Git 2.50.1 (Apple Git-155), on which the tests ran locally.
- GitHub `ubuntu-24.04` runners: the distribution Git is 2.43.0 or newer.
- GitHub `macos-15` runners: the Xcode Git in `/usr/bin` can be older than 2.43.0; the tests resolve the first `git` on `PATH`, which is the Homebrew Git preinstalled on the image. If the CI run shows an older Git first on `PATH`, the macOS job must install a newer one and this record must name it.

`util-linux` `prlimit` is required on Linux only and is part of the `ubuntu-24.04` image; macOS has no `prlimit` and does not enforce `RLIMIT_AS`, which the specification records as a residual exposure.
