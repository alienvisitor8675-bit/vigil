# ParkAgent

Personal prototype: detect that a car has parked in a metered zone, quote
the cost, pay via the city's app (ParkNYC / ParkBoston) within a budget,
and auto-extend using a cost-based rule. Anyone can sign up with Sign in
with Apple (email codes and Google are built but switched off) and connect
their own city's parking account in one guided step. Two cities (NYC and Boston —
zones and sessions rows carry a `city`); Boston zone numbers aren't in the
open data, so they come from the Passport Find Parking feed importer
(`data/import_parkboston_zones.py` → `pnpm -C server load:zone-numbers`)
plus driver reports of the posted number
(`POST /zones/:zoneId/provider-number`, verified when two users agree —
a verified report beats an import, an import beats a single unverified
report) and the executor types the stored number in. The Passport flow was
walked with real money (start through receipt in #112, extend in #113;
Boston has no early stop). Its Vehicles chooser also yields
provider-observed zone terms, and `zone_terms_observed` beats the dataset
when quoting. ParkNYC's paid run is still to do (#24). iOS only.

Latest tag: `v1.0.0-rc3` (79413c4). Prod runs `c0f64cb` (rc3 plus 16
merges; `v1.0.0-rc4` is proposed there) in dry run. The README has the
status and the release tags. What's left for V1 is three workstreams
(`docs/workstreams.md`, scoped by `docs/decisions/2026-09-29-v1-scope.md`):
one issue per PR under the **V1** milestone, labeled `ws-1` / `ws-2` /
`ws-3`, on project board 3 (the milestone also holds the flaky-test issue
#163). By-hand and partner work stays in the Field
test, TestFlight 1.0, and App Store 1.0 milestones; deferred work is V2.

## Locations
The checkout lives at `~/Documents/parkagent`. There is no repo at
`~/parkagent`, and no `~/Documents/parkagent-data` worktree any more. A tool
that claims either is pointed at a stale path. Helper-agent worktrees go
under `.claude/worktrees/`, which is gitignored. Remove them, and any
`~/Documents/pa-*` worktree, once their branch has merged.

## Layout
Read docs/feature-map.md before exploring; update the row when you change a feature.

- data/      Python scripts that fetch NYC/Boston open data and build zone GeoJSON,
             and garage and lot footprints from OSM (the `garages` table, FR-49)
- server/    Fastify + TypeScript API on Fly.io; Prisma + Postgres/PostGIS
- executor/  Playwright scripts that drive the ParkNYC and ParkBoston web apps (isolated, replaceable)
- ios/       SwiftUI app: park detection, location reporting, session UI;
             the Xcode project is generated, see "iOS project" below
- policy.json  Spending and extension rules; server reads it at boot
- docs/decisions/  dated decision records (V1 scope); docs/research/  the
             three research reports the V1 issues come from;
             docs/workstreams.md  who owns which files in V1;
             docs/feature-map.md  every feature's routes, code, guardrails,
             tests and status (diagram: docs/feature-map.mmd)

## Prerequisites
    brew install git gh nvm pnpm xcodegen flyctl stripe/stripe-cli/stripe
    brew install python@3.12 uv

`xcodegen` is required for any iOS work — `ios/ParkAgent.xcodeproj` does not
exist in a fresh checkout until you generate it. Xcode is needed too, from the
Mac App Store.

## Non-negotiables
- Any code path that moves money checks DRY_RUN and policy.json first.
- Never store card numbers or Playwright auth state in the repo. Stripe IDs only.
- Every automated decision writes a row to the `decisions` table with its inputs.
- The executor is the only module allowed to touch a provider's site (ParkNYC,
  ParkBoston). Nothing else imports it.
- Never create a provider account without the user present, never store a
  provider password, never automate a terms checkbox, a verification code,
  or a captcha. Link-or-create prefills empty TEXT inputs on the
  provider's own page and nothing else (see "Accounts" below).

## Accounts (identity + sessions)
Users sign in with Apple (identity token verified against Apple's JWKS,
audience `APPLE_AUDIENCE`) — the only method on by default. Email codes
(`EMAIL_SIGNIN_ENABLED` + `RESEND_API_KEY`) and Google
(`GOOGLE_SIGNIN_ENABLED` + `GOOGLE_CLIENT_ID`) are built but switched off:
their routes answer `403 <method>_signin_disabled`, the server boots
without any of their settings, and the Welcome screen shows only what
`GET /auth/methods` reports on. A sign-in
returns a 15-minute HS256 access JWT (`AUTH_JWT_SECRET`) plus an opaque
refresh token: stored hashed, bound to a device id, 60-day sliding
expiry, **rotated on every use**, and replay of a rotated token revokes
the whole family. Verified-email matches merge into one account.

`Authorization: Bearer` is how the app authenticates; `x-api-key` remains
for admin and scripts only, and the iOS app no longer carries a key at
all (tokens live in the Keychain, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`
so background detection can refresh while locked and a restored backup
never carries a refresh family to a second phone). Carry an existing
script-made user across with
`pnpm -C server attach-identity -- --user <id> --email <e> [--apple-sub <s>]`.

`DELETE /me` tombstones the users row rather than deleting it — the
`decisions` ledger needs a valid user id, so the person goes and the id
stays. It also revokes the person's Sign in with Apple token at Apple
(App Store 5.1.1(v)): the sign-in's authorization code is exchanged for a
refresh token stored sealed, which needs the `APPLE_SIGNIN_KEY` /
`_KEY_ID` / `_TEAM_ID` group; a failed revoke is retried with backoff
(1 h doubling, at most daily) and dead-lettered after 8 attempts, shown
in `/admin/summary`. See server/API.md "Identity & sessions" for the full contract.

A daily job (`jobs/providerHealthTick.ts`) verifies each linked provider
session headlessly and pushes "Reconnect …" when one is expiring or
expired, so it is fixed before the next park. The `expiring` status still
pays — use `providerStatusUsable()` from the registry, never
`status === "linked"`.

## Paying: three sources and their gates
`users.payment_source` picks one. Caps bind all three: each user's own
per-stop and per-day caps (`GET/PUT /me/limits`, the `user_limits` table),
never above `policy.json`'s `session_cap_usd` / `daily_cap_usd`, which are
the ceilings. Every cap check reads `policyFor(user)`
(`services/limits.ts`); `test/limitsScan.test.ts` fails a new read of the
global caps. "Today's spend" counts everything that paid, including
garages approved in Link.
- `provider_card` (default, **live**): the card already saved on the
  user's ParkNYC/ParkBoston account. The executor pays with it. We never
  see the card number.
- `link_wallet`: Stripe Link. It pays **garages only**. Each paid garage is
  a spend request the user approves in Link, and the one-time card is
  revealed once for the garage's own checkout. Providers keep ONE saved
  card, so Link never pays a street meter (#128). `/link/*` answers 503,
  and the app shows "Coming soon", until `LINK_CLIENT_ID` /
  `_CLIENT_SECRET` / `_PUBLISHABLE_KEY` / `_REDIRECT_URI` are set.
  `LINK_TEST_MODE` lets a spend request through under dry run.
- `parkagent_card`: our Issuing card on each linked parking account,
  funded per session by a manual-capture **hold** on the user's own card
  (`services/wallet/holds.ts`). There is no stored balance anywhere. It's
  selectable only with `ISSUING_LIVE=true`, or in a Debug build with the
  Diagnostics sandbox toggle against a test-mode key. Before
  `ISSUING_LIVE`, setup-card never runs against a real provider.

Dry run is effective when env `DRY_RUN` is true OR `policy.json`'s
`dry_run` is. Any `DRY_RUN` other than exactly `false` runs dry. It is
server-wide, for every account. `PUT /policy` is admin-only (`GET /policy`
reports `editable`) and rewrites `policy.json` inside the container
(`/app/policy.json` links into the node-owned `/app/var`, #154), so any
restart or deploy, including `fly secrets set`, reloads the image's copy
(dry run on, caps 45/60). To go real: flip
the `DRY_RUN` secret first, then the caps, then the policy.

## Street session lifecycle (FR-55)
A street session is active from the moment the phone leaves the car until
it comes back. `/parked` holds a street park for an app that lists
`walk_away` in `outcomes` (a `pending_parks` row, status `at_car`): the app
shows nothing at the car and reports its fixes to `POST /location`. The
server decides when the phone has left (two fixes clear of the car, or the
app's `left_car`) and answers with the prompt, which the phone shows as
written ("Pay $4.10 for zone 456?", Pay · Not now · Wrong spot); the
prompt is a local notification, not an APNs push. The tap is
`POST /parked/:id/confirm`: it runs `POST /session/start`'s own path, for
no more than the amount shown, once per park, and a tap at the car waits
for the walk-away. Coming back (30 m of the car for 60 s, or
`returned_to_car` at the car) ends the session: stopped where the provider
can stop early, `ended_at_return` where it can't. The rules are in
`server/src/services/pendingSession.ts`; the app half is
`ios/ParkAgent/Detection/ParkLifecycle.swift`. Keep new code on these:
- nothing asks, pays, or extends on a fix that has the phone at the car;
- a park is paid only through its tap, and a closed park never again;
- only the newest `/parked` says where the car is: it closes a park still
  waiting elsewhere, and stops extensions for a session whose car moved;
- the tap pays what was shown (amount, zone number, dry run) or nothing;
- the extension worker buys time only for a phone known to be away.

The YOLO beta (no-tap start within every cap) and dropping the per-stop
cap are #210, not built yet.

## Working style
- Small PRs on feat/* branches, squash-merged into main.
- **V1 workstreams** (`docs/workstreams.md`): one PR per issue; a session
  works in a worktree per PR and never edits files outside its stream
  (shared files only additively, per the table there). The author merges
  their own PR after CI is green and a self-review, except a PR touching
  money or provider access, which the other founder reads first. One
  merge in flight at a time across both people, announced in chat; after
  every merge `waitdeploy <n> && nightly` (or `shipit <n>`); rebase after
  the other person merges.
- All changes land through a PR — no direct pushes to main, no exceptions.
  Before any commit, check `git branch --show-current`; if it says main,
  branch first (the checkout can land on main after a PR merge). Branch
  protection (admins included) requires the `server`, `ios`, and `executor`
  checks. Only squash merges are allowed, and merged branches are deleted
  on GitHub automatically. A human merges; a Claude session never does
  (auto mode can't).
- CI (`.github/workflows/ci.yml`):
  - `city-neutral` runs `scripts/check-city-neutral.sh`. No city or provider
    names in app/server sources outside the registries. Take names from
    `CityCatalog` / the provider registry. The same job runs
    `scripts/check-feature-map.mjs`: a path in `docs/feature-map.md` that
    doesn't exist, or a route or job in no row, fails it.
  - `server`, `executor`, `ios` (unit tests, the `ParkAgentRelease` scheme,
    and the Release-binary `strings` check). On a PR that changes nothing
    under `ios/` nor `ci.yml`, `ios` reports "skipped", which passes.
  - `boot` runs `scripts/boot-check.sh off`, `on`, and `broken` against a
    migrated PostGIS. `broken` misconfigures every optional feature, and the
    server must still boot.
  - `ui-tests` (`continue-on-error`). A failed UI test runs once more; one
    that passes only on that retry is listed in the job summary and gets a
    "Flaky UI test: <id>" issue (label `flaky-test`), opened or commented
    on every time it needs the retry again. It runs on main, on a manual
    run, and on a PR only when the PR carries the `ui-tests` label.
    Label a PR `ui-tests` when it changes iOS UI flows.
  - `supply-chain.yml` (PRs, main, and daily): `audit` fails on a high or
    critical advisory in a production dependency (`scripts/audit.mjs`)
    unless `scripts/audit-allowlist.json` accepts it with a reason and an
    expiry, and reports dev-only ones; `action-pins` fails on a
    third-party action not pinned to a commit SHA with a version comment.
  - `deploy` needs `server`, `boot`, and `ios`. A new optional env var needs
    three things:
    - its check in `server/src/env.ts`, as part of a FEATURES entry if it
      turns something on;
    - a line in **both** of `boot-check.sh`'s lists;
    - a broken value in its `broken` mode.

    Anything that logs at boot goes through the `app` from
    `createFastify()` (`docs/incidents.md`).
- pnpm 12 passes a literal `--` through to scripts, and a script using
  strict `parseArgs` rejects it. So call `pnpm -C server decisions:recent
  --city bos`, not `… -- --city bos`. Only `attach-identity`,
  `create:fr-user`, `create:fr-throwaway`, and `purge:fr-throwaways`
  strip the `--`.
- Third-party GitHub Actions are pinned by commit SHA with a version
  comment (`uses: owner/repo@<sha> # v1.2.3`); Dependabot's weekly
  github-actions PR moves the SHA and the comment together.
- Dependabot never proposes an npm major. Take one on deliberately, in its
  own PR, against its breaking changes. Its npm PRs arrive with a stale
  root `pnpm-lock.yaml`; `.github/workflows/dependabot-lockfile.yml` pushes
  the regenerated one, and the PR's CI then waits for "Approve workflows
  to run" in the merge box. After that commit Dependabot won't rebase the
  PR; `@dependabot recreate` starts it over.
- Run `pnpm -r lint && pnpm -r test` before proposing a change.
- `pnpm install` registers a lefthook pre-commit hook (see lefthook.yml):
  lint-staged runs prettier and eslint --fix on staged files, then the server
  tests run. `git commit --no-verify` bypasses it in a pinch; `pnpm format`
  reformats the whole repo.
- Ask before adding a dependency over ~50 KB or any native module.
- When touching Swift, note that background location and motion permissions
  are already configured; do not add new entitlements without asking.

## iOS project
`ios/ParkAgent.xcodeproj` is generated by XcodeGen and is gitignored. After
pulling, or any time `ios/project.yml` changes, run:

    cd ios && xcodegen generate

Never hand-edit the `.xcodeproj` — `.pbxproj` merge conflicts are miserable, and
your edits are erased on the next generate. Change `ios/project.yml` instead.
`ios/ParkAgent/Info.plist` and `ios/ParkAgent/ParkAgent.entitlements` are also
generated from `project.yml`, so add plist keys and entitlements there too.

First checkout also needs `cp ios/Config.example.xcconfig ios/Config.xcconfig`
and your `DEVELOPMENT_TEAM` filled in; the file is gitignored and holds
`API_BASE_URL`, which reaches the app through Info.plist via `AppConfig`.
There is no `API_KEY` any more — the app authenticates as the signed-in
user.

Sign in with Apple needs the capability on the App ID; the entitlement is
declared in `project.yml`. On an UNSIGNED simulator build
(`CODE_SIGNING_ALLOWED=NO`, how the tests run) every Keychain call fails
`errSecMissingEntitlement` (-34018), so `Keychain.swift` keeps a
in-memory fallback — without it nobody could stay signed in on the
simulator. It is compiled only into DEBUG *simulator* builds: a Debug
build on a phone never has it, so a locked-phone Keychain error can't
quietly sign the app out onto an empty store.

The app talks to the **live API on every build**, Debug included. `MockAPI`
activates only for a launch carrying `-useMockAPI YES` (the UI tests) or
inside a SwiftUI preview, exists only in Debug builds, and the choice is
never persisted — a missing
`API_BASE_URL` puts the app on `UnconfiguredAPI` (a visible error state:
the welcome screen's sign-in failure, or Home's banner once signed in),
never a silent swap to fixtures. Launch order is Welcome (no valid
session) → the onboarding truth gate (`State/OnboardingGate.swift`, the
one place that decides which setup step is missing) → Home. Developer
tools live in `Settings/DiagnosticsView.swift`, reached by tapping the
version number in the Account sheet's About section five times, and
compiled out of Release. It holds exactly the field-test kit — detector
status with every capability's live state, the detector self-test,
signal-log export, the effective dry run, Reset onboarding, and the
ParkAgent-card sandbox toggle — and nothing else.

Park detection lives outside the SwiftUI scene (`State/AppServices.swift`):
iOS relaunches the app in the background for significant-change and
visit events with no window, so the app delegate re-arms the detector
there. `PermissionsManager.capabilities` is the one live answer to what
detection can use (`Detection/DetectionCapabilities.swift` holds the
rules and the words); Home's banner, onboarding, Account → Privacy, and
Diagnostics all read it. The engine (`ParkFusionEngine`) is pure and
replays signal logs (`SignalTrace`); `ios/Fixtures/drive-park-walk.gpx`
(from `ios/Tools/make-route.py`) is the shared test route. Each park is
then classified on the phone (`Detection/PlaceClassifier.swift`, FR-53:
the driver's saved places in `State/PlaceMemory.swift`, garage outlines
fetched a 2 km cell at a time from `GET /garages/near`, the entry fix, GPS
loss, and the barometer, which runs only in the stop window) and sent as
`/parked`'s `placeHint`. The server weighs it with the zones and its own
garage outlines (`services/placeClassification.ts`, FR-54) and answers
`garage` or `nopay` only to an app that lists them in `outcomes`. A hint
never makes a park `pay`, and a meter that would charge is never
silenced. The driver's answer about a place goes to
`POST /parked/:id/place` first, and to place memory only once that
succeeds (`Detection/PlaceAnswers.swift`).

**Release builds carry no debug code (FR-34).** Everything mock, scenario,
launch-argument, UI-test-hook, preview, and Diagnostics is inside `#if
DEBUG`; a Release build honors no launch argument. Anything new of that
kind goes inside `#if DEBUG` too, and its type name or a 16+-byte marker
(an accessibility identifier) goes into `ios/Tools/release-denylist.txt`.
Proof runs in CI: `xcodebuild -scheme ParkAgentRelease test` (tests that
run inside the Release build) and `ios/Tools/check-release-binary.sh
<ParkAgent.app>` (`strings`). TestFlight uploads come from the manual
`testflight` workflow; see `docs/testflight.md`.

Maps use MapKit for now; Mapbox is a possible later swap and nothing outside
the map views should depend on MapKit types.

The app depends on the Stripe iOS SDK (SPM, declared in `project.yml`) to
save the card the ParkAgent card's per-session holds are placed on — a
SetupIntent confirmed with Apple Pay or PaymentSheet (nothing is charged;
there is no stored balance). `Support/StripeWallet.swift` is the only file
that imports it, and the mock never reaches it. Live confirmation needs
`STRIPE_PUBLISHABLE_KEY` in `Config.xcconfig` plus the one-time Apple Pay
merchant setup (`merchant.com.thomasbardhi.parkagent`; see server/API.md
"Apple Pay setup").

The tabs are Park · Activity · Wallet. The Wallet answers "how am I
paying, and what have I spent" for three ways to pay (`provider_card`
default, `link_wallet`, `parkagent_card`); one `WalletModel` on AppModel
feeds the Wallet, the Account sheet's "How you pay" row, and onboarding's
pay step, and `Views/Wallet/WalletCopy.swift` holds every sentence about
paying — so the three never disagree. Link never pays a street meter (the
provider keeps one saved card); see server/API.md "Wallet".

The Wallet's "Add to Apple Wallet" is behind `FeatureFlags.applePayProvisioning`
(a constant `false`, showing "coming soon"). Turning it on for real requires the
`com.apple.developer.payment-pass-provisioning` entitlement, which Apple
grants only after an application through Stripe (support-issuing@stripe.com)
— add it to `project.yml` when approved, flip the constant, and add `STPPushProvisioningContext`
(see AddToWalletButton.swift).

## Reliability
docs/reliability.md is the one place that says how the app stays correct
when the phone, the network, a provider, or the server fails:

- Every unsafe request carries an `Idempotency-Key` and is answered once
  (server `services/idempotency.ts`, app `LiveAPI.send`).
- Retries only happen under a key; sign-in and secret-bearing calls are
  never retried.
- Every outbound call has a deadline (a bare `fetch` fails
  `outboundScan.test.ts`).
- Offline parks wait in `ParkOutbox`.
- Shutdown drains in-flight work before closing the browser.
- Fly routes by `/health/ready`.

Keep new code on those rules.

## Claude Code tooling in this repo
- `.claude/agents/`: `ci-watch` (Haiku) polls checks and reads failed logs;
  `test-run` (Sonnet) runs suites and reports counts; `code-map` (Haiku)
  sweeps callers, env vars and external calls. A session delegates those
  steps to them instead of reading the raw output itself. Their `tools:`
  lines use plain `Bash` (a scoped `Bash(...)` pattern there does not
  actually restrict which commands run — #221 comment 6305900070); the
  reader agents keep `disallowedTools: Write, Edit`, and the `guard` plugin
  below is the real backstop.
- There are **two guard layers**, and together they keep a slip from running:
  - The `guard` plugin (`guard@claude-mods`, enabled in `.claude/settings.json`,
    configured under `claudeMods.guard` there; README in
    thomasbardhi01/claude-mods) is the **strong layer**, in every session that
    has installed it. It parses every Bash command (in this session and its
    subagents) and the file tools' target paths, and denies the ones a Claude
    session must never run — `gh pr merge`, `gh pr review --approve`,
    `gh secret set`, `gh workflow run` on main, `gh api` writes,
    `gh repo/release/ruleset/variable` mutations, `git push` to main (the
    default branch, read from `origin/HEAD`), any non-read-only `fly`, and
    reads of `.env`/`.env.*`/`Config.xcconfig`. It matches gh/git/fly by
    basename, fails closed on a command it can't parse or whose name is
    hidden behind a variable, and its `tool.check` catch never answers
    "allow". `/guard off` lifts it for 10 minutes in one session after a
    confirmation (never for subagents). Edit `.claude/settings.json` by
    hand: the `guard` plugin blocks Claude sessions from changing it.
  - `.claude/settings.json` `permissions.deny` is the **always-on weak layer**:
    native Claude Code rules that need no plugin and apply in every session,
    covering the worst cases (reading `.env`/`Config.xcconfig`, `gh pr merge`,
    `gh secret set`, `fly deploy`/`secrets set`/`ssh`, `git push origin main`).
    It's a coarse backstop for sessions that haven't installed the guard
    plugin; the plugin is what actually understands compound commands,
    wrappers and substitutions.
  Neither changes the working agreement: a human still merges every PR, a
  Claude session never does (see "Working style") — the guards just make the
  slip impossible to run by hand.
- The `fleet`, `route` and `lean` plugins (same marketplace, config under
  `claudeMods` in `.claude/settings.json`): a band with context %, rate limits
  and cost above the prompt; a `/fleet` pane of every session on this machine
  plus CI, open PRs, the last nightly and prod health; `/report` to the `coord`
  session; simple read-only turns routed to haiku (`/route`); a lean prompt
  mode (`/lean`).
- Sessions talk to each other with the built-in `ListAgents`/`SendMessage`;
  one coordinator session hands out one PR per worker worktree.
- `docs/audit/2026-10-audit-brief.md` is the audit prompt: one session per
  phase in an audit worktree, outputs in `docs/audit/2026-10/`; only Phase
  3.5 writes code (tests, on `audit/tests-<area>` in its own worktree).

## Pinned versions
Two server deps are deliberately held below `latest`. Don't bump them casually.
- `prisma` / `@prisma/client` pinned to `^7`. The `latest` npm tag currently
  points at an `8.0.0-rc`, and installing it gives a v8-rc CLI against a v7
  client, which is a broken pair. Bump both together once v8 is stable.
- `typescript` pinned to `6.x` in server/ and executor/. TS 7 builds fine,
  but no typescript-eslint release supports it yet, so lint hard-errors.
  Revisit when typescript-eslint ships TS 7 support (their issue #10940).

Prisma's own transitive lodash, deepmerge-ts, and mysql2 are held at
patched versions by `overrides` in `pnpm-workspace.yaml`, each scoped to
the vulnerable range so it goes quiet once Prisma ships the fix.

Note that Prisma 7 reads `DATABASE_URL` from `prisma7.config.ts`, not from
`schema.prisma`. That config and `server/src/index.ts` both load the repo-root
`.env` by explicit path, because `pnpm -C server dev` runs with cwd `server/`
and bare `dotenv/config` would miss it.

## Executor (Phase 5 + provider accounts)
The real executors (ParkNYC/Flowbird and ParkBoston/Passport) are the
Playwright package in `executor/`;
`server/src/services/parknycExecutor.ts` is its only importer. Auth is per
user: each user links their own ParkNYC account (cookies from the app's
login web view via `POST /providers/:provider/link`), sealed with
AES-256-GCM under the `PROVIDER_STATE_KEY` secret in `provider_accounts`.
Real calls run only with env `DRY_RUN=false`, the state key set, and a
linked account for the zone's provider; each call gets a fresh browser
context on one warm shared Chromium process. The old single-secret
`PARKNYC_STATE_PATH`/`PARKNYC_STATE_JSON` plumbing is gone
(`pnpm -C executor run login` remains as a local way to capture cookies).
Linking is a durable job: `POST /providers/:provider/link` answers `202`
with a job id at once and `jobs/linkWorker.ts` verifies in the background
under a 45 s budget, retrying transient failures (1 then 5 min) before
dead-lettering; the app polls `link-status` for the real step and offers
"Continue — we'll let you know" (a push) after 20 s. All provider calls go
through one `ExecutorGate` (`EXECUTOR_CONCURRENCY`, default 2 — sized for
the 1 GB machine) and a per-provider `CircuitBreaker`; the browser warms
at boot (`EXECUTOR_WARM_AT_BOOT`). Nothing is ever retried after the pay
click (`afterPayClick`). See server/API.md "Executor capacity and
resilience".
Its tests run over recorded fixture HTML and never touch a provider's live
site. A few DOM tests launch a local headless Chromium, and they skip
themselves where it is absent. CI's required `executor` job runs lint and
the tests with Chromium installed (#93). `pnpm -C server build` needs
`executor/dist` types, so run `pnpm -C executor run build` first. It is a personal-use prototype
against ParkNYC's own web app. The repo is public. #37 tracks making it
private, or moving `executor/` out, before anything beyond friends. Details: `executor/README.md`.

## Deploy, verify, roll back
A merge to main deploys through CI's `deploy` job, about 10–25 minutes
after the merge because it waits on `ios`. The release command runs
`prisma migrate deploy` first. Three shell helpers (in `~/.zshrc`, not the
repo) wrap the checks:

    # block until prod's /health runs PR <n>'s merge commit or a later one
    waitdeploy(){ SHA=$(gh pr view "$1" --repo thomasbardhi01/parkagent --json mergeCommit -q '.mergeCommit.oid // empty'); [ -z "$SHA" ] && { echo "PR #$1 isn't merged yet"; return 1; }; while :; do D=$(curl -s https://parkagent-api.fly.dev/health | sed -n 's/.*"commit":"\([0-9a-f]*\)".*/\1/p'); if [ -n "$D" ]; then case "$(gh api "repos/thomasbardhi01/parkagent/compare/$SHA...$D" -q .status 2>/dev/null)" in identical|ahead) echo "deployed $D (includes #$1)"; return 0;; esac; fi; sleep 20; done; }
    # dispatch the nightly FR suite against prod and watch it
    nightly(){ gh workflow run nightly-fr.yml --repo thomasbardhi01/parkagent && sleep 8 && gh run watch "$(gh run list --repo thomasbardhi01/parkagent --workflow=nightly-fr.yml --limit 1 --json databaseId -q '.[0].databaseId')" --repo thomasbardhi01/parkagent; }
    # squash-merge PR <n>, then wait for its deploy and run the nightly
    shipit(){ gh pr merge "$1" --repo thomasbardhi01/parkagent --squash --delete-branch && waitdeploy "$1" && nightly; }

So after merging PR N: `waitdeploy N && nightly`, or `shipit N` to merge
too. `waitdeploy` also returns when a later merge has already deployed
over N (the compare says prod is ahead). Don't dispatch the nightly before
the deploy lands, or it tests the old build (its report prints the commit
it tested).

**Secrets: check before every `fly secrets set`.** Run

    pnpm -C server check-secrets NAME=value …   # NAME=@file for a .p8

first, and set only what it calls OK. It runs the server's own checks
(`server/src/env.ts`) on your values, on top of the names already on
`parkagent-api`. It also prints the `fly secrets set` command.
- **Core settings stop the server:** `DATABASE_URL`, `AUTH_JWT_SECRET`,
  `API_KEY_PEPPER`, and a malformed `PROVIDER_STATE_KEY`. With one of these
  wrong, the release crash-loops and prod is down until it's fixed. Fly
  doesn't roll back a failed secrets release.
- **Any other setting that's missing, partial, or malformed** switches only
  its feature off. The server logs `config: <feature> is off — <the
  variable and why>` and lists the feature in `/health`'s `degraded`.
- **Afterwards:** `curl -s https://parkagent-api.fly.dev/health` should show
  `"degraded":[]`. `assistant` there can also mean an assistant turn failed
  on Anthropic billing or auth since the last one that succeeded (it clears
  on the next good turn; server/API.md "GET /health").

The 2026-09-26 outage was `APPLE_MAPS_PRIVATE_KEY` set where the server
reads `APPLE_MAPS_KEY` (`docs/incidents.md`).

**Apple key: one key serves push, sign-in, and Maps.** A single `.p8`
from Keys, with Apple Push Notifications, Sign in with Apple, and MapKit
JS enabled, is set in all three groups under the same key id and team:
`APNS_KEY` / `APNS_KEY_ID` / `APNS_TEAM_ID` (+ `APNS_BUNDLE_ID`),
`APPLE_SIGNIN_KEY` / `_KEY_ID` / `_TEAM_ID`, and `APPLE_MAPS_KEY` /
`_KEY_ID` / `_TEAM_ID` (`fly secrets list` shows equal digests). The
server logs "… is the same key as …" at boot. That's expected with one
key, and wrong only if the key ids differ. To rotate it, enable all three
services on the new key, check all three groups with `check-secrets --live`
(it asks Apple about Maps and Sign in with Apple; push can't be checked
live), set them together, confirm push with `POST /admin/push-test`, then
revoke the old key. The key must be
allowed to send to APNs **Production** for TestFlight (#67).

**Rollback.** For a crash-looping or broken release:

    fly releases -a parkagent-api --image     # the last good release's image
    fly deploy -a parkagent-api --image registry.fly.io/parkagent-api:deployment-<id>
    curl -s https://parkagent-api.fly.dev/health

Then fix forward in a PR. A rollback doesn't revert migrations, so keep
them additive.

**Machine size lives in `fly.toml`.** The `[[vm]]` block is
`shared-cpu-1x` with 1 GB, which the executor's Chromium needs. Every `fly
deploy`, rollbacks included, re-applies it and undoes any `fly scale
memory` or `fly scale vm`. Change the size in `fly.toml` through a PR, and
check it with `fly scale show -a parkagent-api`.

## Commands
- `pnpm -C server dev`         start the API locally
- `pnpm -C server prisma migrate dev`   apply migrations
- `pnpm -C server migrate:policy-fee`   move parknyc_fee_usd into city_overrides
- `./scripts/check-city-neutral.sh`     fail on hardcoded city/provider names
- `pnpm -C server check-secrets NAME=value … [--live]`  check secrets before `fly secrets set`
  (see "Deploy, verify, roll back"; `--unset NAME`, `--no-app`, `--env-file`, a bare `AuthKey_….p8`)
- `scripts/boot-check.sh off|on|broken`   boot the built server with every optional feature off / on /
  misconfigured and assert /health, including its `degraded` list (CI gates deploy on all three;
  needs a migrated DATABASE_URL and no repo-root .env — run it from a scratch worktree). A new
  optional env var gets a line in both of its lists and a broken value in `broken`.
- `pnpm -C server attach-identity -- --user <id> --email <e>`  give an existing user a sign-in identity
  (or `--api-key-prefix <8 chars>` in place of `--user`; on prod:
  `fly ssh console -a parkagent-api -C "node dist/scripts/attach-identity.js …"`)
- `pnpm -C server create:fr-throwaway --pool "$(node server/fr/pool.mjs)"`  mint the FR suite's throwaway users, one per
  `server/fr/` file, as one JSON line: save it to a file named by `FR_THROWAWAY_POOL_FILE` (what the nightly does — never
  an env var's value, which Actions prints in every later step's log) or export it as `FR_THROWAWAY_POOL` locally (needs
  the target's DB + AUTH_JWT_SECRET). The suite runs shuffled; `FR_SEED=<n>` replays the order a report names
- `pnpm -C server purge:fr-throwaways [-- --apply]`  tear down throwaways a run left behind (dry run by default; the nightly applies it on prod)
- `pnpm -C executor run login`     headed browser; sign in to ParkNYC once, save auth state
- `pnpm -C executor run record`    record a real ParkNYC flow (HAR/trace/screens) to fixtures/
- `pnpm -C executor run build`     compile (server build needs its d.ts first)
- `uv run data/fetch_nyc.py`   refresh raw NYC data
- `uv run data/build_zones.py` rebuild zones.geojson
- `uv run data/fetch_boston.py`         refresh raw Boston meter data
- `uv run data/build_boston_zones.py`   rebuild boston_zones.geojson
  (load either file with `pnpm -C server load:zones [--file …]`, once per city)
- `uv run data/fetch_parking_footprints.py [--city bos|nyc]`  garage and lot outlines from OSM
  -> data/out/<city>_garages.geojson (Overpass, cached per tile; New York takes about an hour)
- `pnpm -C server load:garages [--file …]`   load them into `garages`, once per city (data/README.md)
