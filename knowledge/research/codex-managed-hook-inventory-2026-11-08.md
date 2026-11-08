# Selected Codex managed hook inventory — 2026-11-08

[#1042](https://github.com/realkarych/kora/issues/1042) checks the selected thread's reported managed hook before handing the thread to the turn caller. The bound expectation contains the known system hook's directory, command, timeout and normalized declaration fingerprint. The startup helper borrows the same RPC, deadline, message budget and before-write stop check for the new request.

The request contains exactly the acknowledged `threadId`. The pinned thread-specific catalogue returns one entry with the selected checkout. It reflects the thread's hook snapshot and the ready selected environments. The private comparison requires the known synchronous catch-all `PreToolUse` command, enabled and managed with the exact system key, configuration values and fingerprint. Its errors use fixed categories and omit peer response fields. A missing or mismatched reply refuses before the caller can issue `turn/start`. [Pinned catalogue implementation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/catalog_processor.rs#L626-L650)

The catalogue filters bundled built-in hooks. Its selected-thread branch supplies an empty error list. The fingerprint covers the normalized hook declaration and does not measure the script bytes. The bound launch owner retains physical installation and configuration checks. The inventory supplies no Policy decision, guarantee of invocation, before-effect fence or domain closure. [Fingerprint source](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/hooks/src/engine/discovery.rs#L981-L999)

## Discovery

Four fresh selected official 0.166.0 Linux amd64 pairs listed hooks on an acknowledged thread without starting a turn. The original image reported one enabled managed command at `/opt/kora-probe` with `.*`, 60 seconds and fingerprint `sha256:ccdc59d5bf14a68a82a519397df8fd7f46c6b7daefe5a12198365b6d4aa434f6`. Removing the declaration returned an empty list. Changing only its matcher returned the altered matcher and a different fingerprint. The existing five-second stage reported five seconds and fingerprint `sha256:5f52a9101194f0b251dfc933d2b60b224d35886081a5bcd35d23d27a2c979b9c`.

All four scratch cases checked zero model requests, hook invocations and marker effects, reaped eight native children and removed all four exact containers. The three-case log SHA-256 was `77405c4011193c61d54a48816a54f3bea50974e90be260b74a6ebabc7c208c75`. The short-timeout log SHA-256 was `e499a7edd3415305e82382fc9cb8b8423c3db27338390d3e30b0d14ab2f2dec6`.

## Implemented check

The positive selected case verified two authenticated requests to the synthetic Responses endpoint, the exact managed Bash receipt and the selected workspace marker. The missing and wrong-matcher cases both returned `selected_startup_hook_inventory` before a model request, hook invocation or marker. The preserved startup controls still refuse the wrong executor bearer and Bash spelling before inventory or turn. The selected turn and both patch routes retain their separate native result and effect checks. The three native hook timeout controls still report the existing fail-open effect after a valid inventory.

| Mode | Cases | Direct native children reaped | Exact containers removed | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected hook inventory | 3 | 6 | 3 | 0 | 25.43 s |
| Selected startup | 3 | 6 | 3 | 0 | 13.72 s |
| Selected turn events | 2 | 4 | 2 | 0 | 24.14 s |
| Bash hook failures | 4 | 8 | 4 | 0 | 36.20 s |
| Direct patch hook failures | 4 | 8 | 4 | 0 | 35.72 s |
| Shell-routed patch Policy | 4 | 8 | 4 | 0 | 46.31 s |

The timeout helpers settled independently and all before and after container inventories were empty. Review found that the first inventory run did not arm the invocation sentinel for the two refusal cases. The fixed fixture supplies the known decision file in both so an unexpected launch can reach its sentinel before decode. It keeps the zero-request budget. The five other mode case sets are disjoint from those two names and retain identical producer paths. The superseded inventory log SHA-256 was `554a9663694047408f3ad719f684c60184b17bcb46a35016ea1c190a7072f884`. Log SHA-256 values in table order were `23fd791973c9588d24895205f7d2ccad512e4c8c25608cb4f581b0ca52474f62`, `45f7927fcf896d6beb37929af20810577a53b94fca223e3c0bf98c8250d82fa9`, `650dfcfb003acbb9ff185a0d169f473812e557f65c8587e33981f06fd23e31fa`, `69b9bcaceea09baf3ece2a055c1d9bd7cde015454b55492e2c5d0eae6b042c61`, `4f8afc9dcce283cdd012d3a5f456f1f9ebbaaf5824df35658dc67faf1f85e62e` and `782bb2638306f9108446f7119e6c234696b62813c6bd68df65c3b439047350d0`.

The focused Codex, Policy and architecture selection passed 999 tests on macOS arm64 with CPython 3.11.17. The real socketpair cases compare the matcher and fingerprint independently, cover missing and extra hooks, wrong source and owners, exact primitives and explicit nulls, warning/error canaries, remote failure and deadline, cancellation in flight and stop before the hook request or turn. They verify no turn or event on the adverse composed paths and continued caller ownership of the transport. Lock, Ruff, strict Pyright and shell syntax checks passed.

## Reproduce

Run from a repository checkout with a local Docker socket, Buildx and GNU `timeout`. Locked modes also require uv and CPython 3.11.17. The Linux amd64 cases use official Codex 0.166.0, a synthetic model key, loopback Responses and an isolated container network. The fixture pin, archive checksum and Python image manifest match the [selected shell patch fixture](codex-shell-routed-patch-policy-2026-11-08.md#reproduce).

The dedicated inventory mode was tested at `7501a62e109d3f2c5e415787daefdc7781c53735`, fixture tree `e5e8c98f6c0f029f68cb33a32978d3bae33a5a4d`, with an empty Git status before and after. The remaining modes ran at `10ee626fa9e23fe74039a0644d41b54702b8b35b`, fixture tree `e3c2f1f0910005e442e1c1f33ad860f322de50a8`. Both revisions have source tree `6c664965a817fb7350c1028d773bd18b74527087` and lock blob `8ef2bf841b99cedbb0fda606cbf2687cb57e3f97`. The selected turn and shell-routed Policy modes package Git archives and locked wheels. The other modes package the exact checkout fixture and private source files. The measured host was macOS arm64 with Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14. The current case, cleanup and attach deadlines were 30, 5 and 75 seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-inventory
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-startup
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-turn
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-patch-hook-failures
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-shell-patch-policy
```
