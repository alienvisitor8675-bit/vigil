# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Dev image for the phase 2a done-when's test repo (tests/test_done_when_2a.py):
# a uv project, so a deps job has a lockfile to update. Not used for any real
# repo. vigild builds it from pyproject.toml and uv.lock at the base commit,
# like vigil's own (images/dev/vigil.Dockerfile, the same pins), and puts the
# project's environment on PATH so `pytest` and `python` see its packages.

FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1 \
    UV_NO_CACHE=1 \
    UV_OFFLINE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /opt/deps
COPY pyproject.toml uv.lock ./
# --no-build: wheels only, no build backend runs at image build (coord decision 11)
RUN UV_OFFLINE=0 uv sync --frozen --no-build --no-install-project && chmod -R a+rX /opt/venv

COPY --chmod=0555 <<'EOF' /usr/local/bin/vigil-prepare
#!/bin/sh
set -eu
cp -a /src/. /work/
EOF

RUN groupadd -g 10001 vigil && useradd -u 10001 -g 10001 -M -d /tmp/home -s /bin/sh vigil
USER 10001:10001
ENV HOME=/tmp/home
WORKDIR /work
