# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Dev image for thomasbardhi01/vigil (owner direction, review round 2).
# linux/arm64. vigild builds it from pyproject.toml and uv.lock at the base
# commit and validates it by running the offline suite on a clean checkout,
# in this image, with no network (configs/repos.yaml).
#
# Pins (two-week rule: released on or before 2026-10-08): CPython 3.14.8 (the
# executor image's base, by digest; pyproject requires exactly 3.14.8);
# uv 0.12.24 (2026-10-08, by digest); git from Debian trixie as of
# 2026-10-08 (snapshot). vigil's dependencies come from uv.lock, frozen, and
# its exclude-newer already holds them to the same date.

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

ARG SNAPSHOT=20261008T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} trixie main" \
      > /etc/apt/sources.list.d/snapshot.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1 \
    UV_NO_CACHE=1 \
    UV_OFFLINE=1

WORKDIR /opt/deps
COPY pyproject.toml uv.lock ./
RUN UV_OFFLINE=0 uv sync --frozen --no-install-project && chmod -R a+rX /opt/venv

# vigil-prepare: the verifier's first step; the checkout (read-only at /src)
# is copied into /work, the container's tmpfs.
COPY --chmod=0555 <<'EOF' /usr/local/bin/vigil-prepare
#!/bin/sh
set -eu
cp -a /src/. /work/
EOF

RUN groupadd -g 10001 vigil && useradd -u 10001 -g 10001 -M -d /tmp/home -s /bin/sh vigil
USER 10001:10001
ENV HOME=/tmp/home
WORKDIR /work
