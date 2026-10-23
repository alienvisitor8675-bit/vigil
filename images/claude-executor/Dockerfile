# syntax=docker/dockerfile:1
# The executor layer (coord, worker, reviewer, auditor sessions). linux/arm64.
#
# One definition, applied over a base (owner direction, review round 3):
# - over a repo's dev image (`--build-arg BASE=vigil-dev/<repo>:<key>`), vigild
#   builds that repo's session image, so a worker has the repo's real toolchain
#   and dependencies, offline, and can run its suite while it works;
# - over the default BASE, scripts/build-images.py builds the generic executor
#   image (`vigil/claude-executor`) the security tests and doctor use.
# The verifier never runs this layer: it uses the plain dev image.
#
# Build contexts: `claude_mods` (a `git archive` of the pinned SHA, so no token
# and no clone at build time; B16) and `vigil_configs` (the managed settings).
#
# Pins: CPython 3.14.8 for the harness (uv-managed, under /opt/vigil-python,
# whatever the base's own Python); Node 24.21.0 only to install the CLI, which
# is a native binary; Claude Code CLI 2.1.306 (owner exception, Q1) from a
# committed lockfile; claude-agent-sdk 0.2.165 hash-locked; uv 0.12.24; git,
# ripgrep and util-linux from Debian as of 2026-10-08 (snapshot). Bases by digest.

ARG BASE=python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

# The CLI and its whole tree from the committed lockfile (generated with
# --before=2026-10-22, the day after 2.1.306 shipped). It's a native binary:
# Node isn't in the final image.
FROM node:24.21.0-trixie-slim@sha256:173f125896c3b47ddf056734c7ea789d04595a6a08769a8f78e0df642781fb66 AS cli
COPY cli/package.json cli/package-lock.json /opt/claude-cli/
RUN cd /opt/claude-cli && npm ci --no-fund --no-audit && npm cache clean --force \
 && /opt/claude-cli/node_modules/.bin/claude --version | grep -q '^2\.1\.306'

FROM ${BASE}
USER 0:0

ARG SNAPSHOT=20261008T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} trixie main" \
      > /etc/apt/sources.list.d/snapshot.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates ripgrep util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && command -v setpriv

COPY --from=cli /opt/claude-cli /opt/claude-cli
RUN ln -sf /opt/claude-cli/node_modules/.bin/claude /usr/local/bin/claude \
 && claude --version | grep -q '^2\.1\.306'

# The harness's own Python and the SDK, hash-locked (requirements.txt), apart
# from anything the repo uses.
COPY --from=uv /uv /usr/local/bin/vigil-uv
COPY requirements.txt /tmp/requirements.txt
# (the base's own uv settings, e.g. a dev image's UV_OFFLINE, don't apply here)
ARG VIGIL_UV="env UV_OFFLINE=false UV_PYTHON_DOWNLOADS=manual UV_NO_CONFIG=1 UV_PYTHON_INSTALL_DIR=/opt/vigil-python"
RUN $VIGIL_UV vigil-uv python install 3.14.8 \
 && $VIGIL_UV vigil-uv venv --python 3.14.8 /opt/vigil-venv \
 && $VIGIL_UV vigil-uv pip install --python /opt/vigil-venv/bin/python --require-hashes --no-cache \
      -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt /usr/local/bin/vigil-uv \
 && chmod -R a+rX /opt/vigil-python /opt/vigil-venv \
 && /opt/vigil-venv/bin/python -c "import claude_agent_sdk"

# The agent uid (bypassPermissions refuses root); dev images already have it.
RUN id -u 10001 >/dev/null 2>&1 \
 || (groupadd -g 10001 vigil && useradd -u 10001 -g 10001 -M -d /tmp/home -s /bin/sh vigil)

# guard@claude-mods, seeded at build time from the pinned SHA (B16).
COPY --from=claude_mods . /opt/claude-mods
ENV CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed
RUN HOME=/tmp/seed-home claude plugin marketplace add /opt/claude-mods \
 && HOME=/tmp/seed-home claude plugin install guard@claude-mods \
 && rm -rf /tmp/seed-home \
 && chmod -R a+rX,go-w /opt/claude-seed /opt/claude-mods
ENV CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-seed \
    CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1

# Managed settings: the deny list, guard enabled. Project settings can't loosen them.
COPY --from=vigil_configs managed-settings.json /etc/claude-code/managed-settings.json

# Workers never push (P2: vigild pushes from the host). The hook refuses every
# push, and the system gitconfig points every repo at it.
COPY pre-push /opt/vigil/hooks/pre-push
RUN chmod 0555 /opt/vigil/hooks/pre-push \
 && git config --system core.hooksPath /opt/vigil/hooks \
 && git config --system safe.directory /work \
 && git config --system user.name "vigil-worker[bot]" \
 && git config --system user.email "vigil-worker[bot]@users.noreply.github.com"

COPY entrypoint.py /opt/vigil/entrypoint.py
COPY agent-cli /opt/vigil/agent-cli
RUN chmod 0555 /opt/vigil/agent-cli

# The harness is PID 1 as root (runtime.py adds only SETUID, SETGID and KILL);
# it runs the CLI, any session-prepare hook the base has, and git as uid 10001
# with no capabilities (review round 2, N1 and N11).
USER 0:0
ENV HOME=/tmp/home \
    CLAUDE_CONFIG_DIR=/tmp/home/.claude \
    CLAUDE_CODE_ENABLE_TELEMETRY=1 \
    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_LOG_TOOL_DETAILS=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PYTHONUNBUFFERED=1
LABEL dev.vigil.layer=executor
WORKDIR /work
ENTRYPOINT ["/opt/vigil-venv/bin/python", "/opt/vigil/entrypoint.py"]
