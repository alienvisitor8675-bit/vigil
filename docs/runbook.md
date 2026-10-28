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
`coord_session_usd` with it and keep a job's reservations under the $5 day. With the coord at $2, an Opus coord is refused only when its request reaches about 600k characters with ~$0.30 spent (at $1 it was 230-270k). A session whose reservation does not fit what is left of the day waits (events `queued`, `dequeued`) rather than failing; a wave of three workers at $2 runs two and the third waits for one to settle. A call refused mid-run (output below the floor, or out of reservation) ends that session with a page naming the cap and the shortfall (a crashed or killed worker, by contrast, resumes from its last checkpoint).
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

### Review dry run

Rerun the reviewer on a past task to see what a changed prompt would say, with no
effects on `~/vigil-data`: the database is opened read-only, the vault and mirror are
read through gitsafe, and the run happens in a scratch data dir (default: a new temp
dir) with its own database and gateway. It changes no job or task, makes no inbox item
or PR, calls no GitHub. It uses the task's existing session image, so run vigild once
first if the dev image is stale. It needs port 8788 for its gateway: **stop vigild
first**.

**The run's real spend is NOT recorded in the production ledger.** Its calls are
metered in the scratch database only, so add the printed cost to your own records. The
tool prints `COST $X.XXXX: NOT recorded in the production ledger; add it to your
records` and puts `cost_usd` in the JSON. To keep the caps true, it first seeds the
scratch ledger from the source database (read-only): this month's spend, the task's
spend and every open reservation. The $5 day, $2 task and $100 month caps then bite as
in production, and the tool refuses before any call if the run doesn't fit. The run
itself is capped at `review_session_usd` ($0.40; about $0.10 for a review).

```sh
uv run python scripts/review-dry-run.py --data-dir ~/vigil-data --job pa-0003 \
  --task pa-0003-w1 --head 5e57b155fdef46d266fd768492fd6094f3c1da89 \
  --out ~/review-dry/pa-0003-w1.json --repo-visibility public
```

vigild's own loader reads the env file for the Anthropic key; you pass no secret.
GitHub isn't asked for the repo's visibility, so you state it (`--repo-visibility`,
default `private`, which refuses a hosted backend the repo's policy doesn't list;
`public` is refused for a repo whose entry lists only private-safe backends).
`--head` defaults to the latest `pr_open` event's head. The verdict and findings are
printed and written to `--out`, which must not be under the data dir.

### Check everything

```sh
uv run python cli/vigil doctor
```

It prints one `pass`, `fail` or `skip` line per check, never a value. Add
`--repo <owner/name>` to limit it to some repos: it then makes no call about
any other repo (no token, no visibility read), and lists each of them as
`decider: <repo> (not checked: not selected)`. A build session always passes
`--repo thomasbardhi01/vigil`.

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
