# Selected Codex model TLS roots

[#1026](https://github.com/realkarych/kora/issues/1026) verifies three model TLS trust controls in the selected Linux Codex profile. The private constructor refuses the exact `CODEX_CA_CERTIFICATE`, `SSL_CERT_FILE`, and `SSL_CERT_DIR` names in both authored name collections with `codex_launch_environment_conflict`. The check uses names only. It preserves adjacent accepted names, role arguments and configuration, and both roles' exact environment assignments.

## Matched native observations

The pinned [CA bundle selector](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/http-client/src/custom_ca.rs#L384-L418) takes the Codex-specific bundle before the generic file. The [native roots loader](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/http-client/src/native_roots.rs#L40-L62) reads certificate file and directory overrides. Each isolated case uses the same selected configuration and Bash command, synthetic key, and loopback HTTPS certificate shape. Only the materialized app-server receives a fixture override.

| Fixture input | Loopback TLS and HTTP | Managed hook and Bash effect |
| --- | --- | --- |
| Default trust | Exact unknown-CA alert on the selected server, zero accepted connections or HTTP requests | No hook, receipt or ordinary marker |
| `CODEX_CA_CERTIFICATE` | Two exact synthetic-authenticated Responses requests | Exact hook receipt, tool exit zero and ordinary marker |
| `SSL_CERT_FILE` | Two exact synthetic-authenticated Responses requests | Exact hook receipt, tool exit zero and ordinary marker |
| `SSL_CERT_DIR` | Two exact synthetic-authenticated Responses requests | Exact hook receipt, tool exit zero and ordinary marker |

The default observer binds the failure to the stream protocol created by this listener with the exact SSL context. The failed TLS transport itself can already be detached by the time asyncio calls the observer. A fixed alert code triggers fixture stop. The fixture restores its temporary loop observer and checks the absence of requests, hook invocations and ordinary file effects after both direct children settle. This is transport refusal. Native terminal behavior after the refusal remains unverified. An earlier scratch run reached the turn deadline without a terminal.

The positive cases use the existing managed Bash receipt and fixed tool result check. The ordinary marker records the selected Bash and executor home. The command requires non-login mode and absence of the app-server key. The fixture generates a fresh synthetic root and leaf inside the container with bundled OpenSSL and an IP-only SAN for `127.0.0.1`. It unlinks both private key files after use. System trust is untouched. No certificate or key files enter the source, image layers or logs.

Actual runtime assembly, complete declarations and fail-closed Policy remain with [#184](https://github.com/realkarych/kora/issues/184), [#549](https://github.com/realkarych/kora/issues/549) and [#199](https://github.com/realkarych/kora/issues/199), respectively. This fixture qualifies the private name preflight and the selected Linux branch.

## Reproduction

Executable revision `102463d7ca9806c18176bd79a59ff9b8e1ae48b0`, Codex provider tree `690c6bb624825c7b37026a11d6f49cb7eaf23a8c`, fixture tree `38a2a72a946b7bb86bc0ef365b2713832f3bec1c`. The build verifies [official Codex 0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0) at `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`, with Linux amd64 musl archive SHA-256 `a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`. The Python 3.11.17 image manifest is `sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199`. Its OpenSSL CLI and library both reported 3.0.22.

Run from the checkout with GNU `timeout` and a local Docker socket with Buildx. The measured environment used Linux amd64 containers on Docker 29.9.2, Buildx 0.38.1 and LinuxKit 7.0.14 on macOS arm64. The build can fetch pinned inputs. Execution uses nonroot containers with `--network none`, no host mounts, synthetic credentials and loopback model traffic. The existing case, cleanup and attach deadlines are 30, 5 and 75 seconds. Each fixed certificate operation is separately bounded to five seconds.

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-tls-roots
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-bash-trace
```

| Mode | Cases | Direct children reaped | Exact container removals | Exit | Diagnostic wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Selected TLS roots | 4 | 8 | 4 | 0 | 35.48 s |
| Selected launch | 4 | 8 | 4 | 0 | 30.47 s |
| Selected Bash trace | 4 | 8 | 4 | 0 | 30.81 s |

Each mode closed its owned local model server and streams. Probe-container inventories were empty before the run and after each mode. The combined stdout/stderr log SHA-256 values in table order were `234c2817f0b889db7ad829ba34f3957a958ab37ea29a62946d6e73497a76fc95`, `443eed6857910d6994690604a6806cd2fc3e3b35075d85022de9d4f0613d9ee2` and `16b50f2c4fb2efb3a1878751db40c036acd0aeda6aec17fb4ba4620971fbb699`.

The first local implementation run at `be244d2` reached the turn deadline because its exception handler tried to inspect a detached TLS transport. The listener-owned protocol revision `4da7e25` observed the expected alert and reaped both processes but used the positive marker assertion for the default case. The final executable revision requires marker absence for the default. Both failed runs removed the exact container and left an empty inventory.
