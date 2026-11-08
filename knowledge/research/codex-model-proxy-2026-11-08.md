# Selected Codex model proxy environment on Linux

[#1049](https://github.com/realkarych/kora/issues/1049) exercises proxy selection
in the API-key, two-role Codex profile. The model destination stays at the
same configured loopback origin within each attempt. A separately owned
listener acts as the only possible proxy. The fixture uses a synthetic provider
key and scripted responses inside a network-disabled Linux amd64 container.

## Native input

The probe uses the official Codex 0.166.0 Linux x86-64 musl release at
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` and verifies its archive SHA-256
`a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978`.
The pinned [native routing selector](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/http-client/src/outbound_proxy.rs#L381-L402)
describes the scheme-specific names, fallback, and bypass. Its
[lookup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/http-client/src/outbound_proxy.rs#L891-L896)
checks the exact uppercase and lowercase spellings.

The unprivileged fixture materializes the private app-server and exec-server
recipes. It inserts the synthetic key and each test proxy value only in
app-server. The TLS cases use disposable roots of the same shape. The listener
refuses any CONNECT outside the exact local model origin and checks every
CONNECT header block for the synthetic key. It opens no socket to a refused
target. Each expected TLS accept consumes one matching live tunnel socket port.
HTTP proxy requests use the exact absolute URI and receive the scripted reply
at the alternate listener. TLS model authentication is checked at the TLS
origin through the same bounded Responses fixture.

## Reproduction

```sh
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-proxy
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-launch
sh tests/plugins/harness/codex/run_hook_probe.sh --check-selected-tls-roots
uv run --no-sync pytest -q tests/plugins/harness/codex/test_launch_configuration.py tests/plugins/harness/codex/test_selected_proxy.py
```

The probe host needs a local Docker daemon, Buildx and GNU `timeout`. The build
uses the pinned public release. The running container needs neither account
credentials nor access to a real model provider.

## Results

The executable fixture inputs at `8a927ebdeaeedf822149aa20b179205ada4e7846`
passed all 19 cases on Docker 29.9.2 with Buildx 0.38.1 and LinuxKit 7.0.14 on
macOS arm64. Each command built a Linux amd64 container from the named
checkout inputs. All 38 direct native children were reaped, all 19 exact
containers were removed, and the probe inventories were empty before the
matrix and after each mode.

| Cases | Result |
| --- | --- |
| HTTP control and the two matching bypass cases | Two exact authenticated requests at the configured origin and zero proxy model requests. |
| Both HTTP proxy spellings and both catch-all spellings | Two exact authenticated absolute-form requests at the alternate listener and zero at the configured origin. |
| HTTPS control | Two authenticated requests directly to the configured TLS origin, with zero model tunnels or cleartext requests. |
| Both HTTPS proxy spellings | Two CONNECTs with no synthetic key in their headers and two authenticated TLS requests matched to the live tunnels, with zero direct or cleartext model requests. |
| Other optional CONNECT attempts | The catch-all and HTTPS proxy override cases each refused three separate CONNECTs. Every CONNECT was checked for key absence. |

Each proxy case observed the same managed Bash command and ordinary marker.
The Bash command checks that the app-server's key is absent from its
environment before writing the marker. Both listeners register accepted
sockets before asynchronous setup and wait for descriptor closure and
handler settlement. TLS handshakes keep the five-second limit within the
remaining turn time. The local regression verifies refusal of another
loopback destination and EOF at both ends of an interrupted tunnel.
A separate local regression stops an accepted connection before TLS
handshake completion and verifies no application handler ran. It exercises
a transport that a post-handshake application callback would never receive.

| Mode | Cases | Wall time | Log SHA-256 |
| --- | --- | --- | --- |
| `--check-selected-proxy` | 10 | 82.67 s | `e225aea4cde200020a8b6257662f2c9e47c0862d03d6bb4ff3e59aeefb1500b9` |
| `--check-selected-launch` | 5 | 39.01 s | `e858779d6384f54bc2b2021e45992bdfc9f49876b3fdf08fac9ac23a06cd414f` |
| `--check-selected-tls-roots` | 4 | 33.81 s | `a9f1598e069b086fb0a26e6d55e3b86826f0e8a100e60dc8b054526a4d9ed0a2` |

## Incomplete TLS connection teardown

On 2026-11-08, the macOS Scaffold test at `6b39cb7` returned from owner close
and then received `ConnectionResetError` on the peer read. The transport
factory barrier establishes accepted TLS setup. It leaves the kernel's
receive queue state unspecified. TCP can reset a connection closed with
unread bytes. [CI artifact](https://github.com/realkarych/kora/actions/runs/42479383693/artifacts/13617197202) ·
[TCP close semantics](https://www.rfc-editor.org/rfc/rfc9293.html#section-3.6.1).

[#1088](https://github.com/realkarych/kora/issues/1088) checks the owner directly
under the same five-second deadline. Both idle and incomplete-record cases
observe one tracked connection, a closed listener, a closed accepted socket,
a finished serving task and zero handler calls. The raw peer receives zero
bytes at EOF in the idle case. The partial record also permits the peer read
to report a TCP reset. The established CONNECT test still requires EOF at
both ends. Fixture shutdown behavior stays the same.

At `09a0fe3ef17485f64127be276f66d17b030d8a76`, the focused proxy and architecture
selection passed 21 tests on macOS arm64 with CPython 3.11.17. The three proxy
tests passed on Linux arm64 with the same interpreter and a disconnected
container network. Output SHA-256
`bf22e88f0e3a0e89eddb09d252b1661a48c9dfc4bd5aa177b5efca41f2700a18`.
An immediate Docker inventory check raced automatic container removal. The
follow-up inventory confirmed that the container was gone.

## Boundaries

Both authored name collections refuse the exact eight names before the
constructor can return either role recipe. The recipes omit these variables.
The tests preserve adjacent and mixed-case name acceptance in the private
constructor. Installation routing, runtime egress and the placement of the
provider credential remain governed by the existing environment contract.
The result describes the selected native Linux configuration and loopback
routes. System proxy discovery, macOS native execution, complete declarations,
production launch and before-effect tool Policy retain their own qualification.
