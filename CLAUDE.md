# Vigil

Self-hosted orchestrator that runs isolated AI coding sessions. `vigild`
runs natively on the Mac mini; sessions run in linux/arm64 containers under
Docker Desktop; model inference (phase 5) is on the Linux tower. The spec
is `docs/design.md`; how it is built is `docs/architecture.md`; every
feature's code, tests and status is `docs/feature-map.md`; decisions after
phase 1 are ADRs in `docs/decisions/`. Read the feature map before
exploring. `AGENTS.md` is a symlink to this file.

## Rules
- **A human merges.** A Claude session never merges, approves, pushes to
  `main`, runs `gh workflow run`, or calls a write endpoint of `gh api`.
  Work on a `feat/*` branch and open a draft PR.
- **Tests first.** Write the failing test, run it red, then the code.
- **Docs in the same PR.** A change to a flow updates
  `docs/architecture.md` (CI checks it: `scripts/check-docs-same-pr.py`);
  a change to a feature updates its row in `docs/feature-map.md`. Every
  API and gateway route needs a row (CI checks it).
- **Pins.** Third-party versions are exact and at least two weeks old
  (`[tool.uv] exclude-newer` enforces it for PyPI; images are pinned by
  digest). Record each pin in `docs/sources.md`. The one exception is
  Claude Code CLI 2.1.306 in the executor image (owner, 2026-10-22).
- **No new dependency** without a one-line reason in the PR. One way to do
  each thing: `httpx2` is the only HTTP client, stdlib `sqlite3` the only DB
  layer, pydantic the only schema source (`schemas/*.json` are generated).
- **Money.** Every hosted model call goes through vigild's gateway
  (`vigild/gateway/`), which meters it and refuses a call that would cross
  a budget. Nothing else holds a provider key. Caps: $5/day, $2/task,
  $100/month, all hard stops (`configs/budgets.yaml`).
- **GitHub.** Only vigild talks to GitHub, from the host: it checks scope
  and pushes, and opens draft PRs (P2). Workers never hold a GitHub token.
  vigild mints only for repos in `configs/repos.yaml` (plus claude-mods,
  read-only).
- **Private repos** see only backends `configs/repos.yaml` lists for them;
  vigild refuses to start on any hosted entry for a private repo other than
  `jev-cloudflare`, plus `anthropic` for `thomasbardhi01/vigil` only
  (hard-coded in `vigild/repos.py`, not configurable).
- **Provenance.** Every stored artifact and DB row carries
  `provenance: claude | local | human`. The training exporter takes
  `local` only.

## Secrets
- Secrets live only in `~/.config/vigil/env` (mode 600) and the App
  `.pem` beside it. vigild loads the env file once at start
  (`vigild/config.py`). A session never opens, prints or copies anything
  under `~/.config/vigil/`, or any `.env*` file other than `.env.example`.
  The guard plugin does **not** protect that path by name.
- The repo's deny rules block the Write tool on `.env.example`; change it
  with `cp` from a temp file.
- No secret in a test fixture, log, commit, or container. A session gets
  only a per-session gateway token, on its stdin.
- Credentials are checked only by `vigil doctor`: names and pass/fail.

## Never touch
- `thomasbardhi01/parkagent`: no `gh` call, clone, issue or PR from a
  build session, and no `vigil doctor --repo` on it. ParkAgent runs happen
  only through vigild, after the owner confirms.
- `.claude/settings.json`: the owner edits it by hand.

## Layout
- `vigild/` the daemon:
  - `api/`: REST and auth;
  - `contract/`: models and schema export;
  - `intake/`: spec gate, scoring, globs;
  - `jobs/`: model, FSM, tree, artifacts, service, runner, acceptance;
  - `router/`, `decider/`, `gateway/`, `budget.py`, `metering/`, `packets/`;
  - `github/`: App tokens, publish;
  - `executors/`: runtime, egress;
  - `workspace.py`, `devimage.py`, `telemetry/`, `doctor.py`, `kpis.py`,
    `server.py`.
- `cli/vigil/` the `vigil` CLI (`doctor`, `dev-image` in phase 1).
- `configs/` policy as code. `prompts/` role prompts. `schemas/` generated.
- `images/` Dockerfiles: `claude-executor`, `egress-proxy`,
  `otel-collector`.
- `compose.yaml`: Phoenix + the OTel collector.

## Running checks
```sh
uv python install 3.14.8
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, vigild/ and cli/
uv run pytest -q                          # unit + integration, no Docker, no hosted calls
uv run python scripts/check-feature-map.py
uv run python scripts/check-claude-md.py
uv run python scripts/check-docs-same-pr.py origin/main
```
On the Mac, with Docker Desktop running (`export PATH="$HOME/.docker/bin:$PATH"`):
```sh
uv run python scripts/build-images.py --claude-mods-repo <claude-mods checkout>
uv run pytest -m docker                   # real containers; includes the Phoenix test
uv run pytest -m "docker and live"        # also live Haiku turns through the gateway (cents)
```
`pytest` deselects `docker` and `live` by default. Paste their output into
the PR when a change touches containers, the gateway or the runner; never
claim they passed if they didn't run.

Run things from the repo (it is not an installable package):
`uv run python -m vigild`, `uv run python cli/vigil doctor`.

Regenerate `schemas/*.json` after changing a contract model:
`uv run python -m vigild.contract.export` (a test fails if they drift).

## Gotchas
- **Never run host git in a clone a session has touched**, and never read a
  file a session wrote with plain `open()`. A worker can write its `.git`
  (hooks, `commondir`, `alternates`, `diff.external`) and plant symlinks.
  All host git goes through `vigild/gitsafe.py`; a session's result comes
  back as a bundle into vigild's vault (`Workspace.import_result`), and its
  files are read as vault blobs or with `vigild/safefile.py`.
- **Test results come only from the verifier** (`vigild/verifier.py`): fresh
  vault checkouts with the base's runner files, the repo's plain dev image
  (never the session image), no network, three runs (base, before, after)
  compared by their JUnit reports. Never read a test outcome from anything a
  session wrote, and never trust a worker report's test fields.
- Sessions run in the repo's session image: one executor layer
  (`images/claude-executor/Dockerfile`) over its dev image. Keep the layer
  the only executor definition; the harness is root, the agent uid 10001.
- Parse shell lines with `vigild/shellwords.py`, nothing else.
- Secrets are scrubbed by one function, `vigild.redact.redact`; use it for
  anything a session or model produced that vigild stores or publishes.
- A new DB column is a new step at the end of `vigild/db.py` `MIGRATIONS`
  (append only), not just an edit to `SCHEMA`. `migrate()` runs **every**
  step on every start, whatever `user_version` says, so each must be
  idempotent. Never renumber or skip one.
- Build sessions and branch work set `VIGIL_DATA_DIR` to a scratch dir; only
  the owner's production vigild uses `~/vigil-data` (runbook).
- Scope globs are strict (`vigild/intake/paths.py` `in_scope`); money-path
  detection keeps guard's loose rules (`matches`). Don't mix them up.
- Never bind-mount from the repo into a container; keep data dirs under `~`
  or a temp dir (pytest's `tmp_path` is fine). Docker Desktop needs Files and
  Folders access to wherever they live (runbook).
- Containers reach vigild's gateway as `host.docker.internal:8788`
  (192.168.65.254) through the smokescreen sidecar, which allows exactly
  that address:port. DNS is not the egress boundary; tests assert that
  connections fail.
- GitHub adds `metadata: read` to every installation token; anything else
  extra is refused and revoked.
- Python is exactly 3.14.8. uv locally may be newer than CI's 0.12.24; keep
  `uv.lock` committed and `--locked` green.
- `starlette.testclient` uses `httpx2`; don't add `httpx`.
- The job FSM (`vigild/jobs/fsm.py`) is the only place a job's state
  changes. Inbox approval by the owner is the only exit from
  `spec_pending`; every transition writes an `events` row.
- Detection globs are guard's and route's: a glob without `/` matches the
  basename anywhere; with `/` it matches the path's tail;
  case-insensitive (`vigild/intake/paths.py`).
- Untrusted text (issue bodies, PR comments, logs) enters a packet only
  through `packets.fence()`, whose nonce the content can't contain.
- The guard plugin also scans heredocs: a Bash command whose text names a
  guarded command (even inside a string) is refused. Write such text with
  the Write tool.
- Day and month boundaries for budgets are UTC.
