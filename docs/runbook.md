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
- **The tower holds exactly one secret**, `TOWER_API_KEY`, in
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
| `ANTHROPIC_API_KEY` | The gateway's key for every Claude call: coord, workers, reviewer, auditor, the Haiku decider. | Console → org **vigil** → API keys. It already exists. | Org "vigil" only. Set a Console spend limit at or under the monthly credit; vigild's caps sit below it ($10/day, $2/task, $100/month, `configs/budgets.yaml`). | 1 | `GET /v1/models` → 200 |
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
| `TOWER_API_KEY` | llama-server's `--api-key`. In **both** the env file and the tower's `/etc/vigil/llama.env`. | **You generate it** (below), then copy the same line to the tower. | Tower only, over Tailscale. | 5 | `vigil doctor`: `backend: tower-qwen` passes when `GET /v1/models` lists the model; `missing` without the key |

### The decider: Jev (owner, 2026-10-23)

- **Routes.** A repo's `decider:` lists `jev`: Jev on OpenRouter while the
  repo is public, on Cloudflare while it's private (read live, once per
  decision; unreadable, or anything but a plain true/false from GitHub,
  means private). Haiku is the escalation where it's allowed.
- **Answers.** A `choice` gives the option and every option's probability, a
  `score` a number on 1-5, a `noul` yes/no with the probability of yes; the
  thresholds (act at 0.85, escalate at 0.6) read that probability, so a sure
  "no" waits for you. `GET /api/decider/report` shows, per question type, how
  confident the answers are and how many went to Haiku; read it after the
  first 50 real ParkAgent decisions before changing a threshold.
- **Cloudflare credit.** Cloudflare bills Jev through AI Gateway credit:
  $0.042 per million input tokens, output free, plus a 5% fee when you buy
  credit (vigild meters $0.0441/M). When the credit runs out Cloudflare
  answers error 2021, "Insufficient balance": vigild trips the
  `decider_credit` breaker and pages once, "Cloudflare Jev credit is empty
  (2021); decisions now on Haiku fallback" (or "waiting for the owner"). Buy
  credit in the Cloudflare dashboard (AI Gateway), then press **Reset**; until
  then that route is off and Haiku's answers count as fallback.
- **Fallback cap.** Haiku answers that stand in for Jev (drift, failure,
  credit) are capped at $1 per repo per UTC day
  (`decider_fallback_per_repo_usd` in `configs/budgets.yaml`). Jev costs
  cents a day, so crossing a dollar means it has drifted or is failing: the
  call is refused before it is sent, and "breaker tripped:
  decider_fallback_cap (repo:<repo>)" holds that repo's dispatch. Look at
  the Jev pages, then press **Reset**.
- **Version pin.** Each Jev route has a `version_pin` in
  `configs/backends.yaml` (Cloudflare `jev-1.13.0`, OpenRouter
  `typesafe/jev-1.13-20260917`). A reply with another version, or none, pages
  once per new version: "Jev version drift: <route> returned <version>,
  pinned <pin>; decisions now on Haiku fallback" (or "waiting for the
  owner"), with today's fallback spend. **A version bump happens only by PR,
  after the decider eval set has been replayed against the new version.**
  OpenRouter stays pinned to `typesafe/jev-1.13`; Cloudflare's
  `typesafe/jev` is a floating alias, held by the pin; never
  `typesafe/jev-1.14.0` (beta).

### Secrets you generate (owner, 2026-10-23)

**You create every self-generated secret yourself; an agent never does.** No
Claude session (coord, worker, or a build session) generates, prints or
stores one. One command per variable, which appends a fresh value to the env
file without showing it:

```sh
printf 'VIGIL_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env
```

Do the same for `VIGIL_METRICS_TOKEN`, `VIGIL_HEARTBEAT_TOKEN` and
`TOWER_API_KEY`. **Phoenix's two secrets keep a prefix.** Phoenix 20.19.0
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
(`docker compose --env-file ~/.config/vigil/env up -d`); for `TOWER_API_KEY`,
copy the new line to the tower's `/etc/vigil/llama.env` and restart
llama-server. Secrets someone else issues (`ANTHROPIC_API_KEY`, the GitHub
App's key, the Cloudflare token) rotate where they were issued, then the same
way: delete the line, add the new one, restart vigild.

Non-secret settings (hosts, ports, image names, the data directory) live in
`configs/*.yaml`, not here. Langfuse's variables are gone: Phoenix replaced
it (`docs/architecture.md` decision `trace-store`). Langfuse v4 remains an option for a machine
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

### One machine with Docker (the default)

vigild, its sessions and its graders all use the Docker on the machine vigild
runs on. `configs/sandbox_hosts.yaml` lists one host, `local`, and that is all
you need: no context, no key, no second listener. Everything below describes
this setup unless it says "sandbox host". Add a sandbox host only when a
workload needs another architecture or more room (ADR-0004); see "Add a
sandbox host" below.

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
named in `configs/repos.yaml`). **Building it is an explicit step**
(ADR-0004): a session or grader never builds, so after the repo's lockfiles
change at the base commit, the next job is refused with `build <image> first`
and an inbox item with **Rebuild**, until you press it or run
`vigil dev-image` (ParkAgent's takes about 3 minutes cold and is about
3.8 GB). The build installs from the lockfiles, with the network; the
validation runs every suite in `dev_suites` on a clean checkout, in the
image, with no network. A deps PR you merge is the one exception: vigild's own deps-merge handler
builds the parent's new image and resumes the parent, with no press from
you. There is no timer and no launchd job.

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
  its dev image, so workers can run the real suite offline. `vigil dev-image` builds it
  right after the dev image, and Rebuild builds it again when the dev image or the layer
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
  packages and the worker's manifest and lockfile diff, and vigild files a
  **deps job** in *Specs to approve*. Approving it lets the package manager
  run: no model, a lockfile-only update in a container that reaches the
  registries and nothing else. It fails, with the list, if anything new in
  the lockfile is under 14 days old, or if a manifest changed outside its
  dependency fields. Then vigild rebuilds the dev image on the new lockfiles,
  runs every suite offline, and opens the PR. **Review it yourself** (deps
  PRs always need you) and merge it: within two minutes vigild's PR poll sees
  the merge, and the blocked job resumes on its own, on the new image.
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
`coord_session_usd` with it and keep a job's reservations under the $25 day. With the coord at $2, an Opus coord is refused only when its request reaches about 600k characters with ~$0.30 spent (at $1 it was 230-270k). A session whose reservation does not fit what is left of the day waits (events `queued`, `dequeued`) rather than failing; a wave of three workers at $2 runs two and the third waits for one to settle. A call refused mid-run (output below the floor, or out of reservation) ends that session with a page naming the cap and the shortfall (a crashed or killed worker, by contrast, resumes from its last checkpoint).
A session whose final model call stopped at the cap is marked failed
("truncated at the output cap"), not done.

### Unstable test ids and redacted keys

The verifier runs the base twice (four runs with must-fail-first), so a
verification takes about a third longer; each run still has its own
15-minute cap. A test whose id changes every run (a `test.each` title with
`crypto.randomUUID()` or a generated key) is matched by file and title
without its parameters, by count, and `tests.json` lists it under
`ran.unstable`. A random value the rule does not recognise (see decision `verifier`) is
checked by count per file instead, so a deleted test still fails.

`redact` also drops PEM private keys. On start, vigild re-applies it to
stored inbox bodies, events and `tests.json` (a migration), so a key that was
stored before the fix is scrubbed on the next restart; an item's status is
not changed.

### Tasks that change a runner file

A runner file is one that decides how tests run (`runner_config` in
`configs/repos.yaml`, or a script the `test_cmd` runs). When a spec's scope
names one (ParkAgent's `scripts/check-feature-map.mjs`, vigil's `conftest.py`),
the class is flagged and handled differently.

**Hidden tests are disabled until ADR-0003's work merges** (`docs/decisions/ADR-0003-hidden-tests-threat-model.md`). `hidden_tests.enabled` is
off for every repo (`RepoPolicy.hidden_tests` in vigild's `configs/repos.yaml`; a target
repo cannot set it; `vigil doctor` shows it per repo). Today, then:

- No hidden-test author runs, nothing is reserved for one, no hidden run happens, and
  `VIGIL_HOST_KEY` is not needed.
- A flagged task is built and verified on its visible tests, then opens a draft PR titled
  "Owner review required: runner file changed; hidden tests disabled" whose body lists
  the runner files. Read the runner-file diff yourself: you are the oracle. You merge as
  always. The spec item says hidden tests are disabled for the task.
- Nothing escalates for want of hidden tests.
- **Replan with a note** works whatever the switch says. When the spec is not right but
  not wrong enough to throw away, send it back from the spec item (or from a failed
  build's page) with a note, 8 KB at most. The coord amends the approved spec and lists
  what it changed; the new item shows that list and a diff. It is never an approval.
  ```sh
  curl -sS -X POST -H "Authorization: Bearer $VIGIL_API_TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -n --rawfile note note.md '{note: $note}')" \
    http://127.0.0.1:8787/api/inbox/<item id>/replan
  ```
  Over 8 KB is refused (413); an empty note is refused (422); the spec item's replan
  needs a note. (With the switch on, the hidden-test author also gets the amended spec
  and the lines of your note under an item or heading with the word "author" in its
  title, or the whole note if none has it.)

The rest of this section describes what the code does **if the switch is turned on**; it
is not in use, and its sandbox is under review in ADR-0003, so it states no guarantee.

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
  imports in-scope code is rejected and regenerated once. In the verifier the code
  under test runs as another uid through a small root-owned broker (see ADR-0003 for
  the open work on this). The dev image needs `setpriv` (util-linux);
  without it a hidden run is `unknown` and the task escalates.
- **The host key.** Hidden tests only count with a key only you create, once:
  `printf 'VIGIL_HOST_KEY=%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env`
  (mode 600 stays; at least 64 hex characters). Without it every flagged task is recorded "hidden tests
  missing" and escalates; `vigil doctor` shows whether it is set, never the value.
  Authors assert through vigild's `check` helper; a bare `assert` doesn't count.
- **Where the hidden tests live.** For a vitest repo (ParkAgent) vigild picks a
  directory the project's own `include` already collects, read from the vitest
  config at the base, e.g. `server/test/vigil-hidden/<job>/<task>/`; the author
  names its test files to match the include and runs them with the repo's own
  script (`pnpm -C server test <that directory>`). If the config can't be read
  (projects/workspace, no include that fits) the task shows "hidden tests
  missing" and can only escalate.
- **Fixtures.** The hidden-test helpers' `make_fixture({"dir/a.txt": "text"})`
  (`makeFixture` in vitest) returns a fresh directory under `/vigil-fx`, a tmpfs owned by
  the test's uid, with directories at 0755 and files at 0644, for the code under test to
  read. The helpers are the way a hidden test makes a fixture.
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

### Send a ready PR back with findings

A job's review can come back clean while the audit lists findings the reviewer missed,
and the *Ready to merge* item then has no merge-blocking finding to act on. The item
lists, per PR, every review and audit finding as `selectable` (ids `review:N` and
`audit:N`, or the plain `index`). To send the PR back, pick the ones to fix and add a
note if you like (8 KB at most):

```sh
curl -sS -X POST -H "Authorization: Bearer $VIGIL_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"task_id": "pa-0003-w1", "findings": ["audit:0", "audit:1"], "note": "Docs only."}' \
  http://127.0.0.1:8787/api/inbox/<ready item id>/fix
```

The task's worker fixes them on the same branch and PR (the standard fix round: verifier,
re-review and re-audit follow) and a new *Ready to merge* item appears, or a page if the
round fails. It runs on what is left of the task's cap (`max_cost_usd` or $2, whichever
is less, minus its spend); under $0.05 left the call is refused with a 409 saying so.
It is also refused (409) for a merged or closed PR, a job that isn't ready, or findings
that aren't the task's; over 8 KB of note is a 413. Each owner fix counts as a review
round, so the second trips routing rule 5 as any fix round does; the 200 then carries a
`notice` ("this round runs on <preset> (escalated)") and so does the `fix_with_findings`
event. A task that has used its `routing.escalate_review_rounds` (2) is refused with a
409: replan, or merge as is. Only a `ready` job takes a fix (an `in_review` one is refused).

### Add a sandbox host

Optional (ADR-0004). A sandbox host is another machine whose Docker runs
sessions and graders, for example an x86 box for benchmark tasks. It is not a
model host (`configs/backends.yaml`). **Do not touch the machine before you
have read this section.** The owner's own x86 machine has a step-by-step
checklist in `docs/deploy/owner-x86-sandbox.md`.

**What the SSH key is.** The key vigild uses equals root on the sandbox host:
whoever holds it can drive that host's Docker daemon. So it lives only on the
vigild machine, the sandbox host holds no secret, and the host's
`authorized_keys` entry for it is restricted to speaking the Docker API. The
exact line (one line; put the public key last):

```
command="docker system dial-stdio",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 <the public key> vigil-sandbox
```

**Steps, in order.**
1. **A user and the restricted key.** A dedicated user (say `vigil`) in the
   `docker` group, with the line above in its `~/.ssh/authorized_keys`.
   Nothing else is in that file.
2. **userns-remap.** In `/etc/docker/daemon.json` set
   `"userns-remap": "default"` and restart Docker. Rootless Docker is not
   supported until `tests/security/test_threats.py` passes under it.
   `docker info` must then list `name=userns` under Security Options.
3. **Pin the engine.** Install an exact Docker Engine version, hold it
   (`apt-mark hold docker-ce`), and write the same string as `engine_pin`.
   An upgrade is a deliberate edit of the pin.
4. **The slice.** A systemd slice, `vigil.slice`, with `MemoryMax=` and
   `CPUQuota=` set to the host's `memory_gb` and `cpus` (a drop-in under
   `/etc/systemd/system/vigil.slice.d/`; `systemctl daemon-reload`). Docker
   must use the systemd cgroup driver (`"exec-opts":
   ["native.cgroupdriver=systemd"]`). vigild sets `cgroup_parent` to it on
   every container it creates there.
5. **No GPU for sandboxes.** The default runtime stays `runc`; no
   `nvidia` runtime as default, and vigild never passes a device request.
6. **The Docker context on the vigild machine.**
   `docker context create <name> --docker "host=ssh://vigil@<private address>"`.
   The endpoint is `ssh://`, never `tcp://`, and must resolve to a private
   address (RFC 1918, or the 100.64.0.0/10 range of a private overlay).
7. **Images.** Build the executor and proxy images on the sandbox host
   (run `scripts/build-images.py` with `DOCKER_CONTEXT=<name>` set, so the
   Docker CLI builds there); a session or grader
   never builds or pulls, and a missing image refuses with
   `build <image> first`.
8. **The gateway listener.** Set `gateway_bind` to vigild's own private
   address and the host's `gateway_url` to `http://<gateway_bind>:8788`.
   With a remote host listed, vigild also listens there and accepts only the
   hosts' `private_address` values; any other source is dropped before HTTP.
   Keep the private network's own access rules on as well, as defence in
   depth. The loopback listener is unchanged.
9. **The config entry.** Add the host to `configs/sandbox_hosts.yaml` (the
   commented template there lists every field) and set `uses: [bench]`.
10. **Check it.**
    ```sh
    uv run python cli/vigil doctor --repo thomasbardhi01/vigil
    ```
    Doctor prints one line per rule for each remote host (`sandbox host
    <name>: rule 1` … `rule 6`):
    1. ssh only, a private target, and the context's own target and port tried
       three ways, each **refused**: a plain command (the session must act like
       `docker system dial-stdio` with stdin closed), a pty (`-tt`) and a port
       forward (`-N -L`). A broken key or an unreachable host is a failure;
    2. the engine equals `engine_pin`, `userns` is present, rootless is
       refused, and the architecture matches;
    3. egress: a probe container reaches the gateway's `GET /health` and fails
       to reach the internet, the host's own other ports, the other private
       addresses and every model endpoint in `backends.yaml`;
    4. no GPU: the default runtime is not a GPU runtime and the probe sees no
       `/dev/nvidia*`;
    5. the probe reads the slice's memory and CPU limits and fails if they are
       missing or larger than the config;
    6. the disk budget: usage of vigil-labelled images and containers against
       `disk_budget_gb`. vigild prunes only its own labelled items, least
       recently used first, when usage passes the budget: stopped session
       containers of jobs that are merged, done or abandoned (or that no job
       references), and images no container uses. It never touches a running
       container, a live job's session (its result may not be imported yet) or
       a verifier or deps container. If the budget can't be met without live
       work, the new session is refused and doctor says so.

    vigild refuses to start while a configured remote host fails a rule.

**Emulation.** With no amd64 bench host, a bench task runs on `local`
under emulation, labelled `emulated` (`bench.allow_emulation: true`, the
pilot setting). Once an x86 sandbox host passes doctor, turn it off by hand.

**Not built: remote file transfer.** Copying a worker's clone and packet
to a remote host over the Docker API is not written. A remote host
therefore **cannot run a session or a grader yet**: a remote placement is
refused in code, before anything runs, with a reason on a Resume page.
`local` is unaffected. Adding a host and passing doctor is still worth
doing now; nothing runs there until the transfer is built (bench slice 1b
needs it).

### Submit and kill a batch

`POST /api/batch` (admin) makes up to 10 jobs at once. The body is a name and a list of
job requests, each exactly what `POST /api/jobs` takes (`repo`, `title`, `request`, `why`,
`value`, `risk`, and optionally `base`, `notes`, `labels`, `issue`):

```json
{"name": "docs sweep", "jobs": [
  {"repo": "thomasbardhi01/vigil", "title": "...", "request": "...", "why": "...", "value": 3, "risk": 1}
]}
```

It is all or none: if any job is refused, the answer is a 422 whose errors name it
(`jobs[1]: ...`) and no job exists. On success you get `{"batch_id", "name", "jobs"}`; each
job waits for its spec and your approval in the inbox as any job does, and the WIP limit
decides how many run at once. `GET /api/batch/<id>` shows each job's state and cost and the
total (spent plus held). `POST /api/batch/<id>/kill` kills every job in it, through the job
kill below; it answers 200 with each job's result, and any job whose kill failed is listed under `failed`: run it again to finish those.

From the CLI (it needs `VIGIL_API_TOKEN` like `vigil kill`), write the same body as YAML:

```yaml
name: docs sweep
jobs:
  - repo: thomasbardhi01/vigil
    title: ...
    request: ...
    why: ...
    value: 3
    risk: 1
```

```sh
uv run python cli/vigil batch submit batch.yaml    # prints the batch id and its jobs
uv run python cli/vigil batch status b-0001        # state and cost per job, and the total
```

When the specs are in the inbox, `POST /api/batch/<id>/approve-all` (admin) approves every
open one. Each is an ordinary approval with its own event; a job whose spec is not ready
yet is skipped and listed, and approving again later takes the rest.

### Kill a job

`vigil kill <job>` stops a job cleanly, whatever it is doing. It needs `VIGIL_API_TOKEN`
(in your shell, or in the env file vigild reads) and finds vigild at `VIGIL_API_URL`
(default: the configured loopback listener, `http://127.0.0.1:8787`):

```sh
uv run python cli/vigil kill pa-0007
```

It moves the job to `abandoned`, kills its session containers and egress sidecars (and a
verifier run), removes them and their networks, revokes the job's gateway tokens, waits up to
30 s for model calls in flight, and settles what was metered and releases every hold. Open
PRs stay: close them yourself. The answer lists what it did. If it says a call is still in
flight (`pending_calls`), the reservation stays open, and counted against the day, until the
call lands. A job that is already `done` or `abandoned` (or a second kill) is a no-op that
says so, and exits 0. If a kill fails partway, vigil prints the step (a 500); the job is already
`abandoned`, and running `vigil kill <job>` again finishes the work. A kill is not a failure: it does not count toward the repo's failure
breaker. Only containers carrying the job's own `dev.vigil.session` labels are touched.

### Use vigil from a Claude Code session (MCP)

`vigil mcp` is a stdio MCP server over vigild's API. It reads `VIGIL_API_TOKEN` from its own
environment only (it never opens `~/.config/vigil/`), and finds vigild at `VIGIL_API_URL`
(default `http://127.0.0.1:8787`). Add it once; give
it no `--env`, so the token stays out of the MCP config and comes from the shell that starts
Claude Code:

```sh
claude mcp add vigil -- uv run --directory <checkout> python cli/vigil mcp
claude mcp list                      # vigil should show as connected
```

Start Claude Code from a shell where `VIGIL_API_TOKEN` is exported; the server inherits it.
In the session, `/mcp` lists the server's tools: `submit_job`, `job_status`, `list_inbox`,
`inbox_act` (approve, replan with a note, retry, abandon, reset), `kill`, and the batch tools
`submit_batch`, `batch_status` and `approve_all` (batch ids look like `b-0001`). A job submitted
there appears in the inbox as a spec; each call is an `mcp_call` event with `channel: mcp`
on the job, and a kill's transition carries the channel too.

### Check everything

```sh
uv run python cli/vigil doctor
```

It prints one `pass`, `fail` or `skip` line per check, never a value. Add
`--repo <owner/name>` to limit it to some repos: it then makes no call about
any other repo (no token, no visibility read), and lists each of them as
`decider: <repo> (not checked: not selected)`. A build session always passes
`--repo thomasbardhi01/vigil`.

### Back up and restore

This is the durability story until there is a second machine. `vigil backup`
writes a dated set to a destination you choose (an external drive, or a
mount of the tower over Tailscale): `vigil-<UTC stamp>.db` (SQLite online
backup, mode 600) and `vigil-<stamp>.vault/` (rsync). It keeps 14 days.
It is safe to run while vigild is running (SQLite's backup API gives a
consistent snapshot; it may create `vigil.db-wal` and `-shm` beside the
database), and it checks the copy with `integrity_check` before keeping it.
Any filesystem works for the destination, exFAT included (the marker is a
plain copy, not a hard link); mode 600 is not enforced on FAT or exFAT. The
destination must be outside the repo and the data dir. It never reads
`~/.config/vigil/`: back that directory up separately, encrypted, with a
key only you hold.

**Install the nightly job** (the owner does this; a build session does not):

1. Copy `deploy/launchd/dev.vigil.backup.plist` to
   `~/Library/LaunchAgents/dev.vigil.backup.plist`.
2. Replace `__REPO_CHECKOUT__` (the checkout vigild runs from),
   `__UV_PATH__` (`command -v uv`) and `__BACKUP_DESTINATION__` (an absolute
   path on the drive or mount).
3. `plutil -lint ~/Library/LaunchAgents/dev.vigil.backup.plist`
4. Load it with `launchctl bootstrap gui/501 ~/Library/LaunchAgents/dev.vigil.backup.plist`
   (501 is your uid; `id -u` prints it).
5. Test once with `launchctl kickstart gui/501/dev.vigil.backup`, then read
   `/tmp/dev.vigil.backup.log` and check a new set appeared. Docker Desktop
   is not needed. Then `vigil doctor` should show `pass  backup freshness`.
   The job runs at 03:15; the issue's done-when is two nights with old sets
   pruned.

`vigil doctor` checks freshness: it fails when the newest complete set (marker,
db and vault) is more than 36 hours old, when there is none, or when the
destination can't be listed. It reads the destination from `VIGIL_BACKUP_DIR`,
else from the installed plist's `EnvironmentVariables`; it skips only while
neither exists, and once the plist is installed an unparseable plist, a missing
key, the `__BACKUP_DESTINATION__` placeholder or an unreachable destination all
fail.

Run by hand: `VIGIL_BACKUP_DIR=/path/to/dest uv run python cli/vigil backup`
(`--keep-days N`, `--data-dir DIR`).

**Check a backup** (verify only, writes nothing):
`uv run python cli/vigil restore /path/to/dest/vigil-<stamp>.db`. It opens
the file read-only, runs `PRAGMA integrity_check` and compares
`user_version` with the code's migration count; it exits 1 and says why on a
missing, corrupt or wrong-version file.

**Restore** (manual; there is no automated restore):

1. Stop vigild.
2. Move `vigil.db`, `vigil.db-wal`, `vigil.db-shm` and `vault/` out of the
   data dir to a safe place (do not delete them).
3. Copy the backup's `vigil-<stamp>.db` to `<data>/vigil.db` (mode 600) and
   `vigil-<stamp>.vault/` to `<data>/vault`.
4. Run `uv run python cli/vigil restore <data>/vigil.db` to verify the copy.
5. Start vigild.

**The drill** (run on a scratch copy, never on `~/vigil-data`). Recorded run,
2026-10-28, scratch dirs only (`$DRILL`); the manual restore is steps 2 and 3
into a second scratch dir. The last step truncates a copy of the db.

```
$ vigil backup
backed up vigil-20261028T101303Z.db and vigil-20261028T101303Z.vault
[exit 0]
$ ls -l dest
total 336
-rw-------  1 owner group  167936 Oct 28 06:13 vigil-20261028T101303Z.db
-rw-------  1 owner group      16 Oct 28 06:13 vigil-20261028T101303Z.marker
drwxr-xr-x  4 owner group     128 Oct 28 06:13 vigil-20261028T101303Z.vault
[exit 0]
$ vigil restore <dest db>
vigil-20261028T101303Z.db passes integrity_check, user_version 14
[exit 0]
$ cp <db> restored/vigil.db; cp -R dest/vigil-*.vault restored/vault
[exit 0]
$ vigil restore restored/vigil.db
vigil.db passes integrity_check, user_version 14
[exit 0]
$ sqlite3 restored/vigil.db "PRAGMA integrity_check; PRAGMA user_version; SELECT count(*) FROM jobs"
ok
14
3
[exit 0]
$ diff -r src/vault restored/vault && echo identical
identical
[exit 0]
$ freshness(dest, now=now)
(True, 'newest set 20261028T101303Z, 0h old')
[exit 0]
$ freshness(dest, now=now + 37h)
(False, 'newest set 20261028T101303Z, 37h old')
[exit 0]
$ vigil restore truncated.db
backup failed verification: not a readable database: database disk image is malformed
[exit 1]
```

`vigil doctor` has no `--data-dir`: it checks credentials and dependencies,
not a data dir, so it can't be pointed at the restored copy.

### Keep development off the production data

Only the owner's production vigild uses `~/vigil-data`. Build sessions and
branch work (vigild, `vigil doctor`, `vigil dev-image`) set `VIGIL_DATA_DIR`
to a scratch dir, e.g. `VIGIL_DATA_DIR=$(mktemp -d) uv run python cli/vigil doctor`.
It overrides `data_dir` in `configs/vigild.yaml`. Opening the live DB from
another branch's code can migrate it past steps main has never run.

### Run vigild under launchd

Production vigild runs as a launchd agent, so a reboot or a crash doesn't
stop the factory: `RunAtLoad` starts it at login and `KeepAlive` restarts it
whenever it exits (at most every 30 s, so a refused start doesn't hammer
GitHub). It logs to `~/vigil-data/logs/vigild.log`. The template is
`deploy/launchd/dev.vigil.vigild.plist`; it doesn't install itself.

**Before you install it:**
- **Docker Desktop starts at login:** Docker Desktop → Settings → General →
  "Start Docker Desktop when you sign in to your computer". Until Docker is
  up, vigild's container work fails; launchd's restarts cover the gap.
- **The Mac logs in after a reboot.** A launchd agent runs only in a logged-in
  session. Either turn on automatic login (System Settings → Users & Groups;
  not offered while FileVault is on), or log in by hand after each reboot.
- **The trace stack** (Phoenix and the collector) comes back with Docker:
  compose sets `restart: unless-stopped`.

**Install** (the owner does this; a build session does not):

1. Stop any vigild you started by hand (only one may hold the API's 8787 and
   the gateway's 8788).
2. `mkdir -p ~/vigil-data/logs`
3. Copy `deploy/launchd/dev.vigil.vigild.plist` to
   `~/Library/LaunchAgents/dev.vigil.vigild.plist`, and replace
   `__REPO_CHECKOUT__` (the main checkout), `__UV_PATH__` (`command -v uv`),
   `__HOME__` (your home directory, absolute; launchd doesn't expand `~`) and
   `__PATH__`: the `PATH` of the shell that runs vigild successfully today
   (`echo $PATH` there). launchd's own `PATH` is only
   `/usr/bin:/bin:/usr/sbin:/sbin`. Check that the value holds
   `~/.docker/bin` (written out absolute) and uv's directory. No
   `DOCKER_HOST` is needed: the Docker SDK follows the CLI's current context
   (`desktop-linux`, `~/.docker/run/docker.sock`); Docker Desktop has no
   `/var/run/docker.sock` unless that option is on.
4. `plutil -lint ~/Library/LaunchAgents/dev.vigil.vigild.plist`
5. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.vigil.vigild.plist`
6. Check: `launchctl print gui/$(id -u)/dev.vigil.vigild | grep -E "state|pid"`,
   `tail ~/vigil-data/logs/vigild.log`, and
   `curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:8787/api/health`
   (401 without a token means it's up).

**Day to day:**
- Restart (after pulling main, or a config or secret change):
  `launchctl kickstart -k gui/$(id -u)/dev.vigil.vigild`.
- Stop until the next login: `launchctl bootout gui/$(id -u)/dev.vigil.vigild`.
  `launchctl kill` alone isn't enough: `KeepAlive` starts it again.
- The log isn't rotated; truncate it with `: > ~/vigil-data/logs/vigild.log`
  when it grows.

### Start vigild

```sh
uv run python -m vigild
```

Run by hand only when the launchd agent isn't installed or is booted out
(see above).

- **At start** vigild:
  - reads each repo's visibility live through the App (there is no YAML
    field: nothing to edit when a repo changes visibility; vigild follows
    GitHub) and checks the private-repo rule against it. A repo it can't
    read counts as private. It refuses to start when a private repo's
    entries break the rule, and later refuses a dispatch the same way, with
    a page;
  - migrates an older `vigil.db` in place: every `MIGRATIONS` step runs on
    every start (all are idempotent), so a DB another branch advanced repairs
    itself on restart;
  - reconciles anything a previous run left behind: tokens revoked, session
    containers removed, reservations settled, mid-flight jobs blocked with a
    page in the inbox (retry, re-plan or abandon), an `in_review` job whose plan has a
    task with no open PR paged (re-plan or abandon), a draft whose coord died
    paged (retry or cancel), and a draft stuck in `spec_pending` with no open
    inbox item (its coord failed and the page was resolved) paged once with
    **re-plan** and **abandon** ("pa-NNNN is waiting for a spec that was never
    drafted"); the same check runs every 10 minutes. A task whose PR has merged
    is never reviewed;
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
- **"no open PR for <task>; replan or abandon"** (phase 1 run, finding #4): a job
  reached the point of review but a task in its current plan has no open PR for that
  plan's work (the page lists the tasks). Nothing is running. **Re-plan** (with a note
  if you like) sends it back to the coord; **Abandon** ends it. The spec gate now
  refuses a re-plan that names a worker id the job already has, and vigild numbers a
  re-plan's tasks after the job's highest. After an upgrade, a job already stuck in
  `in_review` (pa-0002) gets this page at vigild's start.
- The API is at 127.0.0.1:8787, bearer tokens only (the CLI and MCP).
- The web UI's login exists only on the Mac's Tailscale address
  (`api.tailscale_host`; unset means no UI login). Behind `tailscale serve`
  (HTTPS) the session cookie is `Secure` automatically.
- The gateway is at 127.0.0.1:8788. Containers reach it as
  `host.docker.internal:8788` through their proxy; Docker Desktop forwards
  that to the Mac's loopback.
- **The fleet** (`scheduler` in `configs/vigild.yaml`): three sessions at
  once across every job; four open fleet PRs per repo (`wip_per_repo` in
  `configs/budgets.yaml`). At the limit the repo's WIP breaker pages
  once, listing the stack: merge or close one, then press **Reset**.
- **Circuit breakers** (`breakers` in `configs/vigild.yaml`). Each pages
  once, as "breaker tripped: <kind> (<scope>)", and pauses dispatch for
  its scope: the day's or month's cap (once metered spend reaches it), a
  task's cap, the WIP limit, escalations per day, fix-job depth, a
  backend's errors, a repo's failed jobs, the verifier's unknowns, the
  Cloudflare credit, a repo's decider fallback. Fix the cause, then press
  **Reset**: the jobs it held resume (`queued` if one's last run is still
  finishing; it picks up right after). Or press **Abandon** on the breaker's
  page, naming one of the jobs it holds, to give that job up; abandoning
  its last job resolves the page but leaves the breaker open, so dispatch
  waits for its own condition (or your reset). A backend breaker also closes itself
  after a successful probe five minutes on, resolving its page; meanwhile
  its work goes to the preset's fallback in `configs/routing.yaml`.
- **The experiment line (bench; owner, 2026-10-27).** A bench job's money comes from
  `experiment_monthly_usd` in `configs/budgets.yaml` ($30 a UTC month), never from the
  $25 day; it still counts toward the $200 month, and a bench task's own cap is
  `bench_task_usd` ($1.50). Both are hard stops. A bench reservation with no room is
  refused, not queued, and pages nobody: the run ends with the failure tag `budget`
  (a `bench_failure` event). `vigil doctor` prints the line's spend this month ("experiment
  budget"). At about $0.50-1.50 a task that is 2-6 Smoke runs a month and no Core run;
  raising the line is your edit of that file. Bench rows are `eval-only`, which the
  training exporter refuses.
- **Migration 13 backup (bench 1a).** The first start that finds a database without
  `eval-only` in its provenance CHECKs writes a copy beside it,
  `vigil.db.pre-m13-<UTC stamp>.bak`. The file is created private (umask 077, mode 600),
  and vigild logs the path. **Every start** then checks, on a new connection and inside
  a transaction it rolls back, that each provenance table stores `eval-only`. Only a
  failed CHECK on provenance counts as a refusal. Any other error (a test value that
  clashes) is inconclusive: the table passes if its stored CHECK lists `eval-only`,
  with a warning in the log. On a real refusal vigild **refuses to start**, names the
  table and the newest backup, and
  repairs nothing. To go back: stop vigild, move `vigil.db`, `vigil.db-wal` and
  `vigil.db-shm` aside, copy the `.bak` to `vigil.db`, start the previous build. The
  `.bak` is a whole database (every row, including secrets-adjacent text); keep it at
  mode 600 and delete it once the new build has run well.
- **Midnight.** Work still running at UTC midnight counts against the new
  day (and month) until it settles, since its whole cost books to the day it
  settles; the new day's room is $5 less that.
- **Money held after a session ended.** A reservation stays open (and
  counted) while a call made against it is still in flight, up to the
  upstream timeout (10 minutes) plus 2; then it settles with what the call
  cost. Every 10 minutes vigild settles anything nothing holds
  (`reservation_swept` events), so held money never outlives its work.
- **Dismiss** on a breaker's page is refused while it holds a job: press
  **Reset**, or **Abandon** each job it holds.
- **A full day waits.** A worker (or a coord) the day has no room for is not
  refused: it waits its turn, first in first out, and runs when a reservation
  settles or the UTC day rolls over (`queued` and `dequeued` events, no page).
  A wait ends if the job is abandoned or held meanwhile. A day whose metered
  spend has reached the cap is not waited for: the daily breaker trips.
- **Budget pages.** A job whose workers can't fit a cap that waiting won't
  free (a task's own cap, the month) gets its own page, "the budget can't fit
  this work now": retry later, re-plan with smaller `max_cost_usd`, or
  abandon.
- **Review caps.** A job entering review reserves $1.20 per task under review
  (`review_session_usd` $0.40 + `audit_session_usd` $0.80 in
  `configs/budgets.yaml`); what isn't used goes back when the pass ends,
  before any fix round builds. A pass reviews at most 4 tasks; a job with
  more is reviewed in passes, one after another. A
  review that runs past its cap still completes, charged to the day (the
  daily and monthly caps stay hard). "Review caps too low for shape <shape>:
  2 overages; consider re-sizing" means raise those two values. Re-size
  them from real ParkAgent reviews after the phase 1 run.
- **Deps PRs.** vigild opens them with no source builds (wheels and
  tarballs only) and builds the new dev image only after you merge. A review
  that fails closes the PR and makes it again once; a second failure closes
  that one and pages you. Merging one by hand at any point resumes the job
  that waits on it.
- **Label intake** is off until a repo says `intake: true` in
  `configs/repos.yaml`. Then every five minutes vigild turns each open
  issue labelled `vigil:ready` into a job, once; `value:N` and `risk:N`
  labels score it (3 each by default). Anyone who can label issues there
  can start work, so turn it on only where you trust everyone with triage
  access. A labelled issue starts a coord draft (up to `coord_session_usd`,
  $2, against the day); nothing runs past `spec_pending` until you approve
  the spec. On today: `thomasbardhi01/parkagent` (owner, 2026-10-27). An issue that
  already has a job is skipped, matched by the job's `issue` field, and so is
  a second `POST /api/jobs` for it (409). Every job not abandoned blocks,
  merged and done included: a merged job's issue is not re-taken by label;
  submit a new job for follow-up work. Label an issue only if it wasn't
  submitted through the API, or abandon the API job first (jobs from before
  the field carry none). An issue intake took itself stays claimed even
  after its job is abandoned; to redo it, submit a new job through the API.
- Running it under launchd arrives with phase 2b, together with the CLI.

## Review replays and the regression set

A replay asks "what would this reviewer prompt have said about that PR?" without
touching anything real. It needs vigild running, the job's vault and clone record
still on disk, and no open breaker.

- **One replay:** `POST /api/review/replay` with the admin credential and
  `{"job_id": "pa-0003", "task_id": "pa-0003-w1", "prompt_version": "<ref or hash>"}`.
  `prompt_version` is a git ref of this repo (a branch, tag or commit) or the sha256 of
  `prompts/reviewer.md` at some commit, but only one **shipped** on `origin/main`: a
  commit not reachable from it (a feature branch, a stash) is refused, and the repo vigild
  runs from needs a fetched `origin/main`. Add `"head": "<sha>"` to replay an earlier head of the task (one its PR was
  published at); the default is the latest. There is no visibility option: a private
  repo reaches only the backends its real review may, and is refused (403) otherwise.
  The answer has the replay's id (`rp-…`), `status`, `verdict`, `findings` and
  `cost_usd`; the same row is in the `replays` table.
- **What it costs and where it books:** one reviewer session, at most
  `review_session_usd` ($0.40, a hard cap: a replay gets no over-cap extension), on the
  experiment line (`experiment_monthly_usd`), never the $25 day, and counted in the $200
  month. A full line, or any open breaker, refuses it (409).
- **What it never does:** change a job, task, review ladder, rung, window or audit; file
  an inbox item; call GitHub; write the job's own `review.json`. Every row it writes is
  `eval-only`.
- **The regression set:** `tests/fixtures/review_cases/`, one file per case, no code. Run
  it on a prompt version with
  `uv run python cli/vigil review replay-set --prompt-version <ref>`
  (or `POST /api/review/replay-set`). It prints **recall**, expected findings found
  divided by expected, three ways (pa-0003's 4, the 5 AC-only findings, all 6), per case,
  and the total cost. A case whose head the
  vault no longer has is reported as an error, not a miss. To compare two prompts, run it
  once on each (the current one: `main`; a candidate: a commit or tag already on main). Five cases
  cost at most $2.00 a run, usually $0.75-$1.50.
- **Adding a case:** a YAML file with `id`, `job_id`, `task_id`, `head` (40 hex) and
  `expected` (each with `ac` like `AC7`, `class`, a short `description`, and `file` and
  `line` where the miss was). Never paste code or diff text into one. A finding counts as
  found only if it names the AC (or, with `ac: null`, the file) and sits on that file within
  15 lines of `line`; the class only breaks ties (its terms are `CLASS_TERMS` in
  `vigild/review/replay.py`).
