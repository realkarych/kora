FROM python:3.11.17-slim-bookworm@sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199 AS upstream
ADD --checksum=sha256:a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978 https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-x86_64-unknown-linux-musl.tar.gz /tmp/codex.tar.gz
RUN tar -xzf /tmp/codex.tar.gz -C /tmp codex-x86_64-unknown-linux-musl && install -m 0755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex

FROM python:3.11.17-slim-bookworm@sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199 AS probe
COPY --from=upstream /usr/local/bin/codex /usr/local/bin/codex
COPY requirements.toml /etc/codex/requirements.toml
COPY probe.py managed_hook.py interrupt_child.py /opt/kora-probe/
COPY owned_loopback.py selected_launch.py selected_proxy.py selected_tls.py /usr/local/lib/python3.11/site-packages/tests/plugins/harness/codex/
COPY app_server_rpc.py app_server_startup.py app_server_turn.py app_server_session.py app_server_tools.py app_server_usage.py app_server_errors.py app_server_messages.py pre_tool_use.py launch_configuration.py selected_startup.py /usr/local/lib/python3.11/site-packages/kora/plugins/harness/codex/
RUN install -d -m 0700 -o 65532 -g 65532 /var/lib/kora-hook-probe
USER 65532:65532
WORKDIR /tmp
RUN test "$(env -i /usr/local/bin/codex --version)" = "codex-cli 0.166.0"
ENTRYPOINT ["/usr/local/bin/python3", "-I", "/opt/kora-probe/probe.py"]

FROM probe AS interactive
USER root
RUN sed -i 's/matcher = "\^Bash\$"/matcher = ".*"/' /etc/codex/requirements.toml && grep -Fxq 'matcher = ".*"' /etc/codex/requirements.toml
USER 65532:65532

FROM probe AS one-shot
USER root
RUN printf '\n[features]\nunified_exec = false\n' >> /etc/codex/requirements.toml
USER 65532:65532

FROM one-shot AS shell-controls
USER root
RUN sed -i '1i allow_login_shell = false' /etc/codex/requirements.toml && \
    sed -i 's/matcher = "\^Bash\$"/matcher = ".*"/' /etc/codex/requirements.toml && \
    grep -Fxq 'matcher = ".*"' /etc/codex/requirements.toml && \
    test -x /bin/bash && test -x /bin/sh && \
    test "$(readlink -f /bin/bash)" != "$(readlink -f /bin/sh)" && \
    ! getent passwd 65532 && \
    printf '%s\n' 'kora-probe:x:65532:65532::/var/lib/kora-hook-probe:/bin/bash' >> /etc/passwd
USER 65532:65532

FROM probe AS default

FROM shell-controls AS selected-hook-failures
USER root
RUN sed -i 's/^timeout = 60$/timeout = 5/' /etc/codex/requirements.toml && \
    grep -Fxq 'timeout = 5' /etc/codex/requirements.toml
USER 65532:65532

FROM probe AS patch-hook
USER root
RUN sed -i 's/matcher = "\^Bash\$"/matcher = "^apply_patch$"/' /etc/codex/requirements.toml && grep -Fxq 'matcher = "^apply_patch$"' /etc/codex/requirements.toml
USER 65532:65532

FROM shell-controls AS selected-turn
COPY linux-packages /usr/local/lib/python3.11/site-packages/
COPY kora /usr/local/lib/python3.11/site-packages/kora/

FROM selected-hook-failures AS selected-shell-patch-policy
COPY linux-packages /usr/local/lib/python3.11/site-packages/
COPY kora /usr/local/lib/python3.11/site-packages/kora/
COPY selected_shell_patch.py /usr/local/lib/python3.11/site-packages/tests/plugins/harness/codex/

FROM selected-hook-failures AS selected-executor-guard
COPY linux-packages /usr/local/lib/python3.11/site-packages/
COPY probe.py guarded_executor.py selected_executor_guard.py /usr/local/lib/python3.11/site-packages/tests/plugins/harness/codex/
ENTRYPOINT ["/usr/local/bin/python3", "-I", "-m", "tests.plugins.harness.codex.selected_executor_guard"]

FROM shell-controls AS missing-managed-hook
USER root
RUN sed -i '/^\[hooks\]/,/^timeout = 60$/d' /etc/codex/requirements.toml && \
    ! grep -q '^\[hooks\]' /etc/codex/requirements.toml
USER 65532:65532

FROM shell-controls AS wrong-managed-matcher
USER root
RUN sed -i 's/^matcher = "\.\*"/matcher = "^Unrelated$"/' /etc/codex/requirements.toml && \
    grep -Fxq 'matcher = "^Unrelated$"' /etc/codex/requirements.toml
USER 65532:65532

FROM selected-turn AS selected-gated-stdio
COPY selected_gated_stdio.py selected_bound_environment.py /usr/local/lib/python3.11/site-packages/tests/plugins/harness/codex/
USER root
RUN printf '%s\n' '4b4f524100004761746564537464696f' > /etc/machine-id && chmod 0444 /etc/machine-id
USER 65532:65532

FROM selected-gated-stdio AS selected-gated-bound
COPY selected_bash.py /usr/local/lib/python3.11/site-packages/tests/plugins/harness/codex/
USER root
RUN sed -i 's|^kora-probe:x:65532:65532::/var/lib/kora-hook-probe:/bin/bash$|kora-probe:x:65532:65532::/var/lib/kora-hook-probe:/usr/bin/bash|' /etc/passwd && \
    test "$(getent passwd 65532)" = 'kora-probe:x:65532:65532::/var/lib/kora-hook-probe:/usr/bin/bash' && \
    test "$(grep -c '^[^:]*:[^:]*:65532:' /etc/passwd)" = 1 && \
    test "$(stat -c '%u:%a' /etc/passwd)" = 0:644
USER 65532:65532

FROM selected-gated-stdio AS selected-gated-missing
COPY --from=missing-managed-hook /etc/codex/requirements.toml /etc/codex/requirements.toml

FROM selected-gated-stdio AS selected-gated-matcher
COPY --from=wrong-managed-matcher /etc/codex/requirements.toml /etc/codex/requirements.toml
