# CEL binding crate lock copy and its OSV override

Checked: 2026-10-21, for [issue #168](https://github.com/realkarych/kora/issues/168), on top of `master` at `4fedb80`.

## The copy

The `common-expression-language` 0.10.0 Python binding bundles Rust crates in its native module. Lockfile scanning of `uv.lock` cannot see them, so kora scans a committed copy of the binding's own `Cargo.lock` ([decision 13 of issue #160](https://github.com/realkarych/kora/issues/160#issuecomment-6299408634), with the path decided on [issue #162](https://github.com/realkarych/kora/issues/162)).

| Property | Value |
|---|---|
| Path | `.github/osv/common-expression-language/Cargo.lock` |
| Size | 19,150 bytes |
| Git blob | `3d28dc2e7e21162f895c135c44c7ebfd5652ab6a` |
| SHA-256 | `7115bab240a98243c913e2c97c1ba79ba94b501c8f1370f10820ad155ca107cd` |
| Upstream | [hardbyte/python-common-expression-language](https://github.com/hardbyte/python-common-expression-language) tag `v0.10.0`; the GitHub contents API reports blob `3d28dc2e…` and 19,150 bytes for `Cargo.lock` at that tag |
| Release sdist | `common_expression_language-0.10.0.tar.gz`, 231,354 bytes, SHA-256 `bb2b6a2e50094219e4366cf36d342d74140c97414545912b34a7e8b8b95c92e6` (PyPI JSON API); the planning reviews of #160 and #165 extracted it and found the same `Cargo.lock` blob |

To reproduce: `git hash-object` on the copy, and `gh api "repos/hardbyte/python-common-expression-language/contents/Cargo.lock?ref=v0.10.0" -q .sha`.

The copy lists 83 packages: 82 from the crates.io registry, each with a checksum, and the binding's own root crate, which has no `source`. There are no git sources. Two packages are named `cel`:

- the root crate `cel` 0.10.0, which is the binding itself;
- the crates.io package `cel` 0.14.5, the CEL interpreter the binding wraps.

## The advisory and the override

[GHSA-wxwx-9fh7-5mrw](https://osv.dev/vulnerability/GHSA-wxwx-9fh7-5mrw) (CVE-2025-62162, "cel-rust May Panic During Parsing of Invalid CEL Expressions") affects the crates.io package `cel` from 0.10.0 up to the fix in 0.11.4 (OSV API, 2026-10-21). The root crate matches it only because its name and version collide with that range; the interpreter crate 0.14.5 is outside it.

`.github/osv/common-expression-language/osv-scanner.toml` holds one `[[PackageOverrides]]` entry: `name = "cel"`, `version = "0.10.0"`, `ecosystem = "crates.io"`, `ignore = true`, with its reason. osv-scanner reads this file only for lockfiles in its own directory. In its matching, an absent `name`, `version` or `ecosystem` matches every package, and `nameIsRegex`, `group`, `effectiveUntil` and `[[IgnoredVulns]]` widen or change the ignore. So `tests/architecture/test_osv_override.py` pins the entry's exact keys and values, requires it to match exactly one package of the copy, and requires that package to have no `source`. It also pins the copy's SHA-256 and requires that no other `osv-scanner.toml` exists in the repository.

## Local scan evidence

Run with `ghcr.io/google/osv-scanner-action:v2.7.0`, the image the pinned reusable workflows use. Its index digest is `sha256:d54770fe0b09493894e76a02aaed9f51a0513fa57c5186452a755d257b0068a6`, and the linux/arm64 manifest used was `sha256:09db012ac36bb3cade4feb2f75c896ed0165940adf1655e1a9bc47c6dd989ce7`. The argument was `--lockfile=.github/osv/common-expression-language/Cargo.lock`.

| Case | Exit | Result |
|---|---|---|
| copy with the override | 0 | "found 83 packages"; "Loaded filter from …/osv-scanner.toml"; "Package crates.io/cel/0.10.0 has been filtered out"; "Filtered 1 ignored package/s"; no finding |
| copy without the override | 1 | exactly GHSA-wxwx-9fh7-5mrw on `cel` 0.10.0 |
| scratch copy with `cel` 0.14.5 changed to 0.11.0, with the override | 1 | GHSA-wxwx-9fh7-5mrw on `cel` 0.11.0, so the override does not reach the registry crate |
| a base checkout without the copy | 127 | "failed to resolve path" |
| the reporter comparing a base with the copy against the head | 0 | "No issues found" |

The fourth row is why the copy lands before any workflow scans it. The OSV pull-request job scans the base branch with the pull request's own `scan-args`. If the copy and the scan argument arrived in the same change, that required check would fail. A path without the version keeps the base resolvable when a later pin replaces the file's content.

## Open follow-ups

- [Issue #165](https://github.com/realkarych/kora/issues/165) adds the copy to both OSV jobs. It ties the override's version and the copy's root version to the binding pin, and checks that the installed wheel's SBOM is covered by the copy.
- Vulnerability alerts are enabled on the repository. If the committed `Cargo.lock` raises a Dependabot alert for the root crate, the alert and its handling are added here.
