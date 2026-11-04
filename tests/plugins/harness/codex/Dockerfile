FROM python:3.11.17-slim-bookworm@sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199 AS upstream
ADD --checksum=sha256:a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978 https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-x86_64-unknown-linux-musl.tar.gz /tmp/codex.tar.gz
RUN tar -xzf /tmp/codex.tar.gz -C /tmp codex-x86_64-unknown-linux-musl && install -m 0755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex

FROM python:3.11.17-slim-bookworm@sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199 AS probe
COPY --from=upstream /usr/local/bin/codex /usr/local/bin/codex
COPY requirements.toml /etc/codex/requirements.toml
COPY probe.py managed_hook.py /opt/kora-probe/
COPY app_server_rpc.py app_server_startup.py app_server_turn.py app_server_usage.py pre_tool_use.py /usr/local/lib/python3.11/site-packages/kora/plugins/harness/codex/
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

FROM probe AS patch-hook
USER root
RUN sed -i 's/matcher = "\^Bash\$"/matcher = "^apply_patch$"/' /etc/codex/requirements.toml && grep -Fxq 'matcher = "^apply_patch$"' /etc/codex/requirements.toml
USER 65532:65532
