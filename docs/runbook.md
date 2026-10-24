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
| `PHOENIX_SECRET` | Phoenix signs its JWTs with it. | `printf 's%s' "$(openssl rand -hex 24)"` (at least 32 characters, with a digit and a lowercase letter). | Compose only. | 1 | — |
| `PHOENIX_ADMIN_SECRET` | The collector's bearer token for Phoenix's OTLP endpoint. It acts as Phoenix's first system user. | `printf 'a%s' "$(openssl rand -hex 24)"`. It must differ from `PHOENIX_SECRET`. | Compose only: Phoenix and the collector. | 1 | Phoenix `healthz`, collector port |
| `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` | Your Phoenix login (user `admin`) on first start. | Choose it. Changing it later has no effect; change the password in Phoenix. | 127.0.0.1 / `tailscale serve` only. | 1 | — |
| `VIGIL_API_TOKEN` | Admin bearer token for the REST API and the CLI; the one-time login for the web UI. | `openssl rand -hex 32`. | Full control of vigild. | 1 | `GET /api/health` → 200 |
| `VIGIL_METRICS_TOKEN` | Read-only token: GET endpoints, `/metrics` (phase 3), and a read-only UI login. | `openssl rand -hex 32`. Hand it to Hearth yourself. | Read only. | 1 (reads), 3 (metering) | — |
| `VIGIL_HEARTBEAT_TOKEN` | Lets an outside session post heartbeats for the UI's `/sessions` page. | `openssl rand -hex 32`. | Write heartbeats; nothing else. | 3 | — |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | Jev on Workers AI (zero retention), the default decider. Without them, Haiku decides. | Cloudflare → My Profile → API Tokens → Custom token: **Account · Workers AI · Read** and **Edit**, that one account. | Workers AI only. | 1 (optional) | reported missing |
| `OPENROUTER_API_KEY` | The OpenRouter preset and Jev's other route. | openrouter.ai → Keys, with a **credit limit**. | Never for a private repo: vigild refuses to start if `repos.yaml` lists it for one. | 4 | — |
| `LLAMA_API_KEY` | llama-server's `--api-key`. In **both** the env file and the tower's `/etc/vigil/llama.env`. | `openssl rand -hex 32`. | Tower only, over Tailscale. | 5 | — |

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
- The CLI is at `~/.docker/bin/docker`. Put it on `PATH` for launchd jobs.

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

### The output cap

Every coord and worker session caps each model call's output at
`session_max_output_tokens` in `configs/budgets.yaml` (16,384). The gateway
holds a call's worst case (input at the 1-hour cache-write rate plus the
output cap, at the model's price) against the session's reservation, so an
uncapped Opus call ($20 per million output tokens) could cost more than the
$2 coord reservation (it was $1 until PR 15) before it began. To raise the cap, edit the number
(minimum 1,024) and restart vigild; vigild refuses a job with a 422 and the
numbers if a role's worst-case first call no longer fits its reservation, and
`tests/gateway/test_first_call.py` fails the same way, so raise
`coord_session_usd` with it and keep a job's reservations under the $5 day. With the coord at $2, an Opus coord is refused only when its request reaches about 600k characters with ~$0.30 spent (at $1 it was 230-270k). A session whose reservation does not fit what is left of the day waits (events `queued`, `dequeued`) rather than failing; a wave of three workers at $2 runs two and the third waits for one to settle. A call refused mid-run (output below the floor, or out of reservation) ends that session with a page naming the cap and the shortfall; main has no checkpoint resume.
A session whose final model call stopped at the cap is marked failed
("truncated at the output cap"), not done.

### Unstable test ids and redacted keys

The verifier runs the base twice (four runs with must-fail-first), so a
verification takes about a third longer; each run still has its own
15-minute cap. A test whose id changes every run (a `test.each` title with
`crypto.randomUUID()` or a generated key) is matched by file and title
without its parameters, by count, and `tests.json` lists it under
`ran.unstable`. A random value the rule does not recognise (see B33) is
checked by count per file instead, so a deleted test still fails.

`redact` also drops PEM private keys. On start, vigild re-applies it to
stored inbox bodies, events and `tests.json` (a migration), so a key that was
stored before the fix is scrubbed on the next restart; an item's status is
not changed.

### Tasks that change a runner file

A runner file is one that decides how tests run (`runner_config` in
`configs/repos.yaml`, or a script the `test_cmd` runs). When a spec's scope
names one (ParkAgent's `scripts/check-feature-map.mjs`, vigil's `conftest.py`),
the class is flagged and handled differently:

- **At spec time** vigild runs a fresh Sonnet session (the hidden-test author,
  up to $1, twice at most) before the spec reaches your inbox. The job stays
  `spec_pending` with nothing to approve until it finishes. The spec item then
  shows, per flagged task, the runner files and the hidden tests' names and
  categories. If it says "hidden tests missing; this task can only escalate",
  the task will be built and verified but cannot pass: approve only if you want
  that, or re-plan.
- **At build time** the verifier runs the head's copy of only the runner file
  and the base's copy of every other runner file, plus the hidden tests (fail
  on the base, pass on the head). They decide; the visible test is reported but
  is not enough. `tests.json` has `runner_files`, `runner_file_changed` and
  `hidden`.
- **Hidden tests are black-box.** They never import the code under test; they run
  it as a command (`run_subject`) or call a function through vigild's driver
  (`call_subject`), on a read-only checkout, and compare exit codes, output and
  JSON values. If the task's overlap is itself a test-runner config
  (`conftest.py`, `pytest.ini`, `vitest.config.*`, ...) no author runs: the spec
  item says "owner review only" and the task escalates. A hidden-test file that
  imports in-scope code is rejected and regenerated once.
- **The host key.** Hidden tests only count with a key only you create, once:
  `printf 'VIGIL_HOST_KEY=%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env`
  (mode 600 stays). Without it every flagged task is recorded "hidden tests
  missing" and escalates; `vigil doctor` shows whether it is set, never the value.
  Authors assert through vigild's `check` helper; a bare `assert` doesn't count.
- **Where the hidden tests live.** For a vitest repo (ParkAgent) vigild picks a
  directory the project's own `include` already collects, read from the vitest
  config at the base, e.g. `server/test/vigil-hidden/<job>/<task>/`; the author
  names its test files to match the include and runs them with the repo's own
  script (`pnpm -C server test <that directory>`). If the config can't be read
  (projects/workspace, no include that fits) the task shows "hidden tests
  missing" and can only escalate.
- **If vigild restarts while the author runs** the inbox shows "vigild restarted
  while the hidden-test author was writing"; retry runs the author step only.
  A hidden run that crashes while the author's tests are checked shows the task
  as "hidden tests missing" too. `<data>/hidden-runs/*.txt` (mode 600) holds the
  full output of each hidden run, for you to read on the Mac; `tests.json` has
  counts only.
- **A retry after an escalation** still re-runs the job's other failed tasks;
  the escalated one stays held and the job is held again for it.
- **The PR** is a draft that says "Owner review required: runner file changed:
  <files>". Read the runner-file diff yourself; this class is always your look.
  You merge as always.
- **An escalation** (no hidden tests) shows as an inbox page of kind
  `escalated` and the job as `escalated`: re-plan or abandon.
- **A dispute.** If a worker fails against a hidden test and you think the test
  is wrong, run `vigil hidden show <task>` on the Mac (honours
  `VIGIL_DATA_DIR`). It prints the tests and records a `hidden_tests_shown`
  event. It is not served over the API or MCP. Once shown, the tests have been
  shown to a human, not to a model, so the rule "never replay a job whose
  hidden tests were shown to a model" does not fire on its own account; pasting
  them into a session would count.

### Check everything

```sh
uv run python cli/vigil doctor
```

It prints one `pass`, `fail` or `skip` line per check, never a value. Add
`--repo <owner/name>` to limit the token checks to some repos.

### Keep development off the production data

Only the owner's production vigild uses `~/vigil-data`. Build sessions and
branch work (vigild, `vigil doctor`, `vigil dev-image`) set `VIGIL_DATA_DIR`
to a scratch dir, e.g. `VIGIL_DATA_DIR=$(mktemp -d) uv run python cli/vigil doctor`.
It overrides `data_dir` in `configs/vigild.yaml`. Opening the live DB from
another branch's code can migrate it past steps main has never run.

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
  - migrates an older `vigil.db` in place: every `MIGRATIONS` step runs on
    every start (all are idempotent), so a DB another branch advanced repairs
    itself on restart;
  - reconciles anything a previous run left behind: tokens revoked, session
    containers removed, reservations settled, mid-flight jobs blocked with a
    page in the inbox (retry, re-plan or abandon), a draft whose coord died
    paged (retry or cancel);
  - starts approved builds that never began, once the gateway is listening.
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
- Running it under launchd arrives with phase 2, together with the CLI.
