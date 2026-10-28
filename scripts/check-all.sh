#!/usr/bin/env bash
# Run every pre-push check on the committed HEAD, in a clean temporary
# worktree. A check run on uncommitted changes proves nothing.
#
# Usage: scripts/check-all.sh [--tests]     (BASE=<ref> overrides origin/main)
# Exit: 0 all passed, 1 first failing step, 2 uncommitted tracked changes.
set -u

run_tests=0
for a in "$@"; do
  case "$a" in
    --tests) run_tests=1 ;;
    *) echo "usage: $0 [--tests]" >&2; exit 2 ;;
  esac
done

repo=$(git rev-parse --show-toplevel) || exit 2
cd "$repo" || exit 2
base=${BASE:-origin/main}
# Tests point this at the repo's own tool environment.
read -r -a UVRUN <<< "${CHECK_ALL_UV:-uv run}"

dirty=$(git status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then
  echo "check-all: uncommitted changes to tracked files; commit them first:" >&2
  echo "$dirty" >&2
  exit 2
fi

sha=$(git rev-parse HEAD)
tmp=$(mktemp -d)
cleanup() {
  cd "$repo" && git worktree remove --force "$tmp/wt" >/dev/null 2>&1
  rm -rf "$tmp"
}
trap cleanup EXIT
git worktree add --detach "$tmp/wt" "$sha" >/dev/null || exit 2
cd "$tmp/wt" || exit 2
echo "check-all: HEAD $sha in $tmp/wt"

# CHECK_ALL_STEPS (comma list of step names) limits the run; tests use it.
only=${CHECK_ALL_STEPS:-}
step() {
  local name=$1
  shift
  if [ -n "$only" ] && [[ ",$only," != *",$name,"* ]]; then return 0; fi
  if "$@"; then
    echo "PASS $name"
  else
    echo "FAIL $name"
    exit 1
  fi
}

step sync uv sync --locked
step ruff-check "${UVRUN[@]}" ruff check .
step ruff-format "${UVRUN[@]}" ruff format --check .
step mypy "${UVRUN[@]}" mypy
step feature-map "${UVRUN[@]}" python scripts/check-feature-map.py
step claude-md "${UVRUN[@]}" python scripts/check-claude-md.py
step docs-same-pr "${UVRUN[@]}" python scripts/check-docs-same-pr.py "$base"
if [ "$run_tests" = 1 ]; then
  step pytest "${UVRUN[@]}" pytest -q -n auto
fi
echo "check-all: all passed"
