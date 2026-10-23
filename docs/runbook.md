# Runbook

Operating Vigil by hand. Credentials first, then starting the stack and
the checks phase 1 needs. Stop, rotate and recover sections arrive with
the phases that build those things.

## Credentials

### Where secrets live

- **On the Mac mini, only in `~/.config/vigil/env`**, mode `600`, owned by
  you. It holds `NAME=value` lines, one per variable below. The GitHub App
  private key sits beside it as `~/.config/vigil/vigil-worker.pem`, also
  mode `600`.

  ```sh
  mkdir -p ~/.config/vigil && chmod 700 ~/.config/vigil
  touch ~/.config/vigil/env && chmod 600 ~/.config/vigil/env
  ```

- **vigild loads that file once at startup** and never writes it. It
  refuses to start if the file is readable by anyone but you. The CLI reads
  `VIGIL_API_TOKEN` from it too, so there is no second token file.
- **Docker Compose (Phoenix and the OTel collector) reads its own secrets**
  from the same file by interpolation:
  `docker compose --env-file ~/.config/vigil/env up -d --build`. Those are
  the only containers that hold a secret in their environment, and none of
  them runs agent code.
- **No worker container holds a provider key or a GitHub token.**
  - Model calls go through vigild's gateway (P1). A session gets a
    per-session gateway token, on its stdin, never in the environment
    `docker inspect` shows. The token works only on the gateway, only for
    that session's repo and model, and expires with the session; every token
    is revoked when vigild restarts. Token-shaped strings are redacted from
    session events.
  - vigild pushes branches and opens PRs from the host (P2). The one-hour
    installation token lives in vigild's memory for one publish and is
    revoked after it.
  - Trace-store keys stay in the collector.
- **The tower holds exactly one secret** (phase 5), `LLAMA_API_KEY`, in
  `/etc/vigil/llama.env` (root, mode `600`).
- **Nothing here is ever committed.** `.env.example` at the repo root lists
  the names with no values.
- **A Claude session (coord or worker) never reads, prints or writes any
  of these.**
  - It never opens `~/.config/vigil/` or any `.env*` file other than
    `.env.example`. The guard plugin does not protect that path by name, so
    this rule is the session's own.
  - Credentials are checked only by `vigil doctor` (below), which prints the
    check's name and pass or fail, never a value.

### Variables

The **Phase** column says when vigild first needs the variable. Until then
it can be missing. A missing optional variable switches off only its
feature: vigild logs `config: <feature> is off`, `/api/health` lists it in
`degraded`, and `vigil doctor` reports it.

| Variable | Purpose | Where to get it | Scope / limits | Phase | `vigil doctor` |
|---|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | The gateway's key for every Claude call: coord, workers, reviewer, auditor, the Haiku decider. | Console → org **vigil** → API keys. It already exists. | Org "vigil" only. Set a Console spend limit at or under the monthly credit; vigild's caps sit below it ($5/day, $2/task, $100/month, `configs/budgets.yaml`). | 1 | `GET /v1/models` → 200 |
| `GITHUB_APP_ID` | The vigil-worker App's numeric App ID: the `iss` of its JWT. Not secret, but kept with its key. | GitHub → Settings → Developer settings → GitHub Apps → vigil-worker → **App ID**. | — | 1 | `GET /app` → slug `vigil-worker` |
| `GITHUB_APP_KEY_PATH` | Path to the App's private key, normally `~/.config/vigil/vigil-worker.pem`. | Same page → **Generate a private key**. Save it beside the env file and `chmod 600` it. | Never mounted into any container. | 1 | one installation; mint + revoke a token per repo in `configs/repos.yaml`, and a `contents:read` one on claude-mods |
| `PHOENIX_SECRET` | Phoenix signs its JWTs with it. | **You generate it** (below), with the `a1` prefix. | Compose only. | 1 | — |
| `PHOENIX_ADMIN_SECRET` | The collector's bearer token for Phoenix's OTLP endpoint. It acts as Phoenix's first system user. | **You generate it** (below), with the `a1` prefix. Two runs of the command never give the same value, so it differs from `PHOENIX_SECRET`, which Phoenix requires. | Compose only: Phoenix and the collector. | 1 | Phoenix `healthz`, collector port |
| `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` | Your Phoenix login (user `admin`) on first start. | Choose it. Changing it later has no effect; change the password in Phoenix. | 127.0.0.1 / `tailscale serve` only. | 1 | — |
| `VIGIL_API_TOKEN` | Admin bearer token for the REST API and the CLI; the one-time login for the web UI. | **You generate it** (below). | Full control of vigild. | 1 | `GET /api/health` → 200 |
| `VIGIL_METRICS_TOKEN` | Read-only token: GET endpoints, `/metrics` (phase 3), and a read-only UI login. | **You generate it** (below). Hand it to Hearth yourself. | Read only. | 1 (reads), 3 (metering) | — |
| `VIGIL_HEARTBEAT_TOKEN` | Lets an outside session post heartbeats for the UI's `/sessions` page. | **You generate it** (below). | Write heartbeats; nothing else. | 3 | — |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | Jev on Workers AI (zero retention): the decider route for **private** repos only. No repo lists it while both are public, and `vigil doctor` skips these then. | Cloudflare → My Profile → API Tokens → Custom token: **Account · Workers AI · Read** and **Edit**, that one account. | Workers AI only. | 1 (optional) | skipped unless a repo lists `jev-cloudflare`; then reported missing |
| `OPENROUTER_API_KEY` | Jev on OpenRouter's Decisions API: **the decider for public repos** (owner, 2026-10-23), with Haiku as the escalation; later the OpenRouter preset (phase 4). | openrouter.ai → Keys, with a **credit limit**. Set. | Never for a private repo: vigild refuses to start if `repos.yaml` lists it for one. | 2a | `GET /api/v1/key` → 200, and one `decider:` line per repo naming its route |
| `LLAMA_API_KEY` | llama-server's `--api-key`. In **both** the env file and the tower's `/etc/vigil/llama.env`. | **You generate it** (below), then copy the same line to the tower. | Tower only, over Tailscale. | 5 | — |

### Secrets you generate (owner, 2026-10-23)

**You create every self-generated secret yourself; an agent never does.** No
Claude session (coord, worker, or a build session) generates, prints or
stores one. One command per variable, which appends a fresh value to the env
file without showing it:

```sh
printf 'VIGIL_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env
```

Do the same for `VIGIL_METRICS_TOKEN`, `VIGIL_HEARTBEAT_TOKEN` and
`LLAMA_API_KEY`. **Phoenix's two secrets keep a prefix.** Phoenix 20.19.0
still requires each to be at least 32 characters with at least one digit and
one lowercase letter (`phoenix.auth.REQUIREMENTS_FOR_PHOENIX_SECRET`, checked
in the running container on 2026-10-22). 64 hex characters almost always have
both, but not always, so the `a1` prefix guarantees it:

```sh
printf 'PHOENIX_SECRET=a1%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env
printf 'PHOENIX_ADMIN_SECRET=a1%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env
```

**To rotate one:** delete its line from `~/.config/vigil/env`, run its
command again, and restart vigild (it reads the file once, at start). For the
Phoenix secrets, also restart the trace stack
(`docker compose --env-file ~/.config/vigil/env up -d`); for `LLAMA_API_KEY`,
copy the new line to the tower's `/etc/vigil/llama.env` and restart
llama-server. Secrets someone else issues (`ANTHROPIC_API_KEY`, the GitHub
App's key, the Cloudflare token) rotate where they were issued, then the same
way: delete the line, add the new one, restart vigild.

Non-secret settings (hosts, ports, image names, the data directory) live in
`configs/*.yaml`, not here. Langfuse's variables are gone: Phoenix replaced
it (`docs/architecture.md` B23). Langfuse v4 remains an option for a machine
with about 16 GiB to spare for the Docker VM.

### The GitHub App "vigil-worker"

Made, installed and verified (coordinator, 2026-10-22):
- **Installation:** one, on all eight of your repos.
- **Permissions:** contents:write, pull_requests:write, issues:write,
  checks:read, metadata:read, artifact_metadata:read.

vigild narrows that in code (`vigild/github/app.py`, ADR-0001):
- **Repos:** it mints only for repos in `configs/repos.yaml`, plus
  claude-mods, and every claude-mods token is `{"contents": "read"}`. The
  App's installations on your other repos, including private ones like
  hearth, are never used.
- **Permissions:** each token is one repo, one hour, and only what its use
  needs.
  - Publishing a branch: contents:write and pull_requests:write.
  - Refreshing a mirror: contents:read (no host git credentials are ever
    used).
  - Checking a repo's visibility at start: metadata:read.
  - Label intake (phase 2): issues:read.
- **Revoked:** after every use. Anything GitHub grants beyond what was
  asked (other than the metadata:read it always adds) makes vigild revoke
  the token and refuse.
- **Never in a container:** no token ever enters one (P2).

One App per role is deferred: ADR-0001 says when it changes.

### Ruleset on `main` (every target repo)

Every repo the fleet works in needs a `main` ruleset that:
- requires a pull request;
- blocks force pushes and deletion;
- requires the CI checks (for vigil: `lint`, `types`, `test`; the command
  is in the phase 1 PR);
- has **no bypass actor** that includes the App.

### What a Claude session never does here

Coord and the workers never:
- create, read, print, store or commit a secret;
- open `~/.config/vigil/`;
- run `gh secret set`;
- install the App;
- edit a ruleset.

When a phase needs a credential, coord stops, posts these steps in the
status issue (#1), and waits for you.

## Running phase 1

### Docker Desktop on this Mac

- 4.94 with a 12 GiB VM. That fits Phoenix (2 GiB cap), the collector
  (256 MiB), and the sessions (4 GiB cap each, plus a 128 MiB proxy).
- **Setup step: if you keep checkouts in `~/Documents`, grant Docker
  access to it** (System Settings → Privacy & Security → Files and Folders
  → Docker → Documents). Without that grant, a bind mount from there hangs
  `docker start` until Docker Desktop restarts. Done on this Mac.
- Even so, nothing vigil runs bind-mounts from the repo: the collector's
  config is baked into its image, and vigild's data (`~/vigil-data`: clones,
  session files) lives outside the checkout.
- The CLI is at `~/.docker/bin/docker`. vigild and `scripts/build-images.py`
  put its directory (and the one it links to, where
  `docker-credential-desktop` lives) first on the docker CLI's `PATH`
  themselves, so a launchd job or a bare shell needs nothing extra.

### Build the images

```sh
uv run python scripts/build-images.py --claude-mods-repo <a local claude-mods checkout>
```

It builds:
- `vigil/egress-proxy:0.1.0`: smokescreen, checked against the release's
  SHA-256;
- `vigil/claude-executor:2.1.306`: guard@claude-mods is seeded from a
  `git archive` of the pinned SHA, so no token is needed.

### Start the trace stack

```sh
docker compose --env-file ~/.config/vigil/env up -d --build
```

- Phoenix is at http://127.0.0.1:6006. Log in as `admin` with
  `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`. For the tailnet:
  `tailscale serve --bg --https=6443 http://127.0.0.1:6006`.
- The collector listens on 127.0.0.1:4318 for vigild's own spans.
- vigild attaches the collector to each session's network as
  `otel-collector`.

### Dev images

Each repo's tests run only in its own dev image (`images/dev/<repo>.Dockerfile`,
named in `configs/repos.yaml`). **vigild builds and validates it itself**,
when the repo's lockfiles change at the base commit (the first job after a
lockfile change waits for it; ParkAgent's takes about 3 minutes cold and is
about 3.8 GB): the build installs from the lockfiles, with the network; the
validation runs every suite in `dev_suites` on a clean checkout, in the
image, with no network. There is no timer and no launchd job.

- **When it fails**, every session for that repo is refused and the inbox
  gets one item per image saying what failed (the build's tail, or each
  suite's outcome and tail), with **Rebuild** and **Dismiss**. Fix the cause
  (often the Dockerfile or the suite command in `repos.yaml`, through a
  PR), then press Rebuild, then retry the blocked job from its own item.
- **By hand**, the same build and validation at the base branch's tip:

  ```sh
  uv run python cli/vigil dev-image --repo thomasbardhi01/vigil
  uv run python cli/vigil dev-image --repo thomasbardhi01/vigil --rebuild
  ```

- **Session images.** A repo's coord and worker sessions run in its session
  image: the executor layer (`images/claude-executor/Dockerfile`) built over
  its dev image, so workers can run the real suite offline. vigild builds it
  right after the dev image, and rebuilds it when the dev image or the layer
  changes (ParkAgent's: about 4.8 GB, the layer about 25 s with a warm build
  cache). It fails, pages and rebuilds like a dev image. The generic
  `vigil/claude-executor` image (`scripts/build-images.py`) is the same layer
  over the pinned Python base; the security tests run against all three.
- **Test commands** must run pytest or vitest (directly, or through a pnpm
  script, optionally after `cd` or with `-C`): the verifier judges by their
  JUnit report, and the spec gate refuses anything else (`--filter`, npm and
  yarn scripts included). Verifier containers run one at a time
  (`container.verifier_concurrency` in `configs/vigild.yaml`); raise it only
  if the Docker VM has the memory (6 GiB each, plus tmpfs).
- **A task that needs a dependency** blocks with an inbox item listing the
  packages and the worker's manifest and lockfile diff. Until phase 2's deps
  job exists, make the dependency change yourself in its own PR (manifest and
  lockfile only), let it merge, then press **Retry** on the item: the new
  base changes the dev image key, so vigild builds the new image first.
- `dev_image_tools` is what a `test_cmd` may use; the spec gate refuses a
  test that needs anything else. `dev_excluded` says which suites can't run
  in a Linux container and why: for ParkAgent, the iOS app (Xcode).
- Builds take Debian packages from `snapshot.debian.org` as of 2026-10-08
  (the pin rule), which is slow, not broken.

### Check everything

```sh
uv run python cli/vigil doctor
```

It prints one `pass`, `fail` or `skip` line per check, never a value. Add
`--repo <owner/name>` to limit the token checks to some repos.

### Start vigild

```sh
uv run python -m vigild
```

- **At start** vigild:
  - reads each repo's real visibility through the App. It refuses to start
    if `configs/repos.yaml` says public for a repo that is private, or if it
    can't check (so the App must be configured). A repo the YAML keeps
    private that is public on GitHub only logs a warning: the YAML's stricter
    rule stays in force;
  - migrates an older `vigil.db` in place (`PRAGMA user_version`);
  - reconciles anything a previous run left behind: tokens revoked, session
    containers removed, reservations settled, mid-flight jobs blocked with a
    page in the inbox (retry, re-plan or abandon), a draft whose coord died
    paged (retry or cancel);
  - starts approved builds that never began, once the gateway is listening;
  - **resumes** building jobs a restart cut off when that's safe: their
    workers' image is unchanged, each has at least $0.05 of its cap left,
    and none has crashed `scheduler.resume_attempts` (2) times in a row.
    They pick up from their last checkpoint with their transcript.
- **A worker container that dies or is killed** (OOM, `docker kill`)
  resumes on its own the same way, up to two times; then its job blocks
  with the usual retry, re-plan or abandon.
- **When a worker fails**, its job is blocked after its wave with one inbox
  item: **Retry** runs the failed tasks again on what's left of each task's
  cap (offered only when there is at least $0.05 left), **Re-plan** sends
  the job back to its coord with the failures, **Abandon** ends it (any open
  PRs stay for you to close).
- The API is at 127.0.0.1:8787, bearer tokens only (the CLI and MCP).
- The web UI's login exists only on the Mac's Tailscale address
  (`api.tailscale_host`; unset means no UI login). Behind `tailscale serve`
  (HTTPS) the session cookie is `Secure` automatically.
- The gateway is at 127.0.0.1:8788. Containers reach it as
  `host.docker.internal:8788` through their proxy; Docker Desktop forwards
  that to the Mac's loopback.
- **The fleet** (`scheduler` in `configs/vigild.yaml`): three sessions at
  once across every job; four open fleet PRs per repo (`wip_per_repo` in
  `configs/budgets.yaml`). At the limit a job blocks with a page listing
  the stack: merge or close one, then press **Retry**.
- **Label intake** is off until a repo says `intake: true` in
  `configs/repos.yaml`. Then every five minutes vigild turns each open
  issue labelled `vigil:ready` into a job, once; `value:N` and `risk:N`
  labels score it (3 each by default). Anyone who can label issues there
  can start work, so turn it on only where that's you.
- Running it under launchd arrives with phase 2b, together with the CLI.
