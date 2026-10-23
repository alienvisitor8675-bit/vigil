# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Dev image for thomasbardhi01/parkagent (owner direction, review round 2).
# linux/arm64. vigild builds it, keyed on this file and the lockfiles below at
# the base commit, and validates it by running the suites in configs/repos.yaml
# on a clean checkout, in this image, with no network.
#
# Build context: only the manifests and lockfiles vigild copies from the base
# commit (repos.yaml `dev_lockfiles`). No ParkAgent source is in the image;
# the verifier mounts a checkout read-only at /src and `vigil-prepare` copies it
# to /work next to the dependencies installed here.
#
# Pins (two-week rule: released on or before 2026-10-08):
#   node 24.21.0 (the executor image's base, by digest); pnpm 12.4.1
#   (2026-09-10, ParkAgent's packageManager, tarball integrity checked);
#   uv 0.12.24 (2026-10-08, by digest); CPython 3.12 for data/ (its
#   .python-version) from that uv's pinned download list; Debian packages from
#   snapshot.debian.org as of 2026-10-08: PostgreSQL 17.11, PostGIS 3.5.2 (CI's
#   postgis/postgis:17-3.5), git, curl, openssl; Chromium for Playwright is
#   the build its lockfile's playwright@1.64.1 names. ParkAgent's own
#   dependencies come from its lockfiles with frozen installs, as its CI does.
#
# Not here: the iOS app. It's SwiftUI with an XcodeGen project and needs
# Xcode and an iOS simulator (CI runs it on macos-latest), which no Linux
# container has. repos.yaml and docs/architecture.md record the exclusion.

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM node:24.21.0-trixie-slim@sha256:173f125896c3b47ddf056734c7ea789d04595a6a08769a8f78e0df642781fb66

ARG SNAPSHOT=20261008T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} trixie main" \
      > /etc/apt/sources.list.d/snapshot.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git curl openssl xz-utils \
      postgresql-17=17.11-0+deb13u1 postgresql-17-postgis-3=3.5.2+dfsg-1 \
 && rm -rf /var/lib/apt/lists/*

# pnpm, exactly ParkAgent's packageManager version, from a checked tarball.
ARG PNPM_INTEGRITY=sha512-LoHjmdc/6DkNqyXgaqeIq3pZCCSNL1o3D4K0gRR6ano2e/gEj5pv22Rg8hpm8FQt7bi5TKLIcjWWdBkgsWVtTA==
RUN cd /tmp && npm pack --silent pnpm@12.4.1 >/dev/null \
 && got="sha512-$(openssl dgst -sha512 -binary pnpm-12.4.1.tgz | base64 -w0)" \
 && [ "$got" = "$PNPM_INTEGRITY" ] \
 && npm install -g --no-audit --no-fund ./pnpm-12.4.1.tgz && rm pnpm-12.4.1.tgz \
 && npm cache clean --force && pnpm --version | grep -qx 12.4.1

COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/data-venv \
    UV_FROZEN=1 \
    UV_NO_CACHE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHECKPOINT_DISABLE=1 \
    PRISMA_HIDE_UPDATE_MESSAGE=1 \
    pnpm_config_verify_deps_before_run=false \
    CI=1

# Node dependencies, frozen, installed at /work (where the checkout will be,
# so pnpm's recorded paths hold) and then moved to /opt/deps; vigil-prepare
# copies them back next to the checkout.
WORKDIR /work
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY server/package.json server/package.json
COPY executor/package.json executor/package.json
RUN pnpm install --frozen-lockfile \
 && pnpm -C executor exec playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache /root/.local/share/pnpm/store \
 && mkdir -p /opt/deps/server /opt/deps/executor \
 && mv node_modules /opt/deps/ && mv server/node_modules /opt/deps/server/ \
 && mv executor/node_modules /opt/deps/executor/ \
 && chmod -R a+rX /opt/deps /ms-playwright
WORKDIR /opt/deps

# data/'s Python deps, frozen, into a venv outside the checkout.
COPY data/pyproject.toml data/uv.lock data/.python-version data/
RUN cd data && uv python install "$(cat .python-version)" \
 && uv sync --frozen --no-install-project \
 && chmod -R a+rX /opt/python /opt/data-venv

# vigil-session-prepare: puts the dependencies above next to whatever is at
# /work and starts a PostgreSQL with PostGIS on loopback. A session runs it
# before the agent (the session image is this image plus the executor layer),
# so a worker can run the real suites offline; vigil-prepare, the verifier's
# first step, runs it after copying the read-only checkout from /src.
COPY --chmod=0555 <<'EOF' /usr/local/bin/vigil-session-prepare
#!/bin/sh
set -eu
for d in . server executor; do
  [ -d "/work/$d" ] || continue  # a checkout without that package
  # always the image's copy: whatever a checkout or a session left is replaced
  rm -rf "/work/$d/node_modules"
  cp -a "/opt/deps/$d/node_modules" "/work/$d/"
done
PGDATA=/tmp/pg; export PGDATA
if [ ! -d "$PGDATA" ]; then
  /usr/lib/postgresql/17/bin/initdb -U ci --auth=trust --no-sync -D "$PGDATA" >/dev/null
  /usr/lib/postgresql/17/bin/pg_ctl -s -w -D "$PGDATA" -l /tmp/pg.log \
    -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp" start
  for db in ci pa_pg_test; do /usr/lib/postgresql/17/bin/createdb -h 127.0.0.1 -U ci "$db"; done
fi
EOF
COPY --chmod=0555 <<'EOF' /usr/local/bin/vigil-prepare
#!/bin/sh
set -eu
cp -a /src/. /work/
exec /usr/local/bin/vigil-session-prepare
EOF

RUN groupadd -g 10001 vigil && useradd -u 10001 -g 10001 -M -d /tmp/home -s /bin/sh vigil
USER 10001:10001
ENV HOME=/tmp/home
WORKDIR /work
