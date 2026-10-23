# Vigil web UI

The spec for phase 3. vigild serves the UI on Tailscale only. It is plain
HTML, CSS and a little vanilla JS, with no build step, no framework and no
component library. Every number on screen comes from the same REST API the
CLI and the MCP server use (`docs/architecture.md` B6, B7).

## Look: a surveillance console

Pure black, one accent, dense but calm. Picture the sonar-vision scene in
*The Dark Knight*: a dark room with everything drawn as pale blue signal
on black.
- There's no chrome, no cards with shadows and no gradients.
- **State is text and motion, never colour.** There is no red, green or
  yellow anywhere.

### Palette

These CSS variables are the only colours:

| Variable | Value | Use |
|---|---|---|
| `--bg` | `#000000` | Page and panels. No lighter "card" greys. |
| `--ink` | `#E8EDF2` | Primary text. |
| `--dim` | `#6B7580` | Secondary text, borders, idle state. |
| `--sonar` | `#9FD8F0` | Accent: live activity, links, the selected row, the logo. |
| `--sonar-2` | `#346E93` | Dark accent: fills, graph bodies, hover. |
| `--warn` | `--ink` text on a `--sonar-2` background | Anything that needs you: cap reached, guard denial, failed, escalated. |

- **Borders** are hairlines only: 1px `--dim` at 30% opacity.
- **Corners** are never rounded more than 2px.
- **Icons:** none from icon packs.

**Contrast, measured** (owner decision Q3, 2026-10-22). The final hexes:
`--bg #000000`, `--ink #E8EDF2`, `--dim #6B7580`, `--sonar #9FD8F0`,
`--sonar-2 #346E93`.
- `--sonar-2` was `#3A7CA5`, which gave the warn pair (`--ink` on
  `--sonar-2`) 3.87:1. At `#346E93` it is **4.69:1**, which passes WCAG AA
  for 12–13px text.
- `--dim` on black is 4.48:1, a hair under AA; it stays `#6B7580` by the
  owner's choice.
- `--sonar` on black is 13.55:1.

### Type

- **Data** uses one monospace stack, 12px and 13px:
  `"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace`.
- **Headings** use one grotesk stack:
  `Inter, ui-sans-serif, system-ui, sans-serif`.
- **No web fonts are downloaded.** A font is used if it's installed, and
  the system font otherwise. That keeps the page weight budget and makes
  no outside requests.
- **Headings are sparse and large**, after the "Intelligence in Motion"
  reference: one big low-contrast heading per page (`--dim`, 48–64px,
  light weight), then dense data.

### Logo

- **What:** the "Vigil" wordmark, plus a mark drawn in characters, not an
  image. The mark is a cowboy-hat vigilante silhouette (brim, crown,
  bandana) built from `# * : . ' _ -`, in `--sonar` on black, inside a
  `<pre>`.
- **Sizes:** about 12 rows × 24 columns full size, and a 5-row version for
  the nav.
- **Source:** both live in `vigild/web/assets/mark.txt`, served at
  `/assets/mark.txt`. The phase 3 worker draws the final mark by hand.

This is a first draft so the shape is agreed early:

```text
         _.--._
       .'######'.
      :##########:
   ___:##########:___
 .'##################'.
 '-..______________..-'
      :##'    '##:
      :#*  ..  *#:
      '*:*:*:*:*:'
     .:*:*:*:*:*:*.
   .'::*:*:*:*:*:*::'.
  :####################:
```

```text
   _.--._
 _:######:_
'-.______.-'
  :*:*:*:
 :#######:
```

### Character graphs

Every chart and sparkline is text in a `<pre>`, updated live from the
stream. There is no canvas, no SVG and no charting library.

| Graph | Characters | Meaning |
|---|---|---|
| Sparkline | `▁▂▃▄▅▆▇█` | 8 levels, scaled to the window's max; the newest sample on the right |
| Bar | `█▓▒░` | `█` spent or used, `▓` reserved, `▒` the other series, `░` the rest of the cap |
| Timeline | `─┬┴│` | events on a time axis: `┬` starts, `┴` ends, `│` now |
| Meter | `[████████░░░░] 63%` | a block meter for GPU, VRAM, CPU, RAM, W, and tokens against a limit |
| Histogram | `▁…█` columns, one per confidence bucket | decider calibration |

**Breathing.** Graphs should read like the dot-matrix sphere in the
reference: a shape made of characters that breathes while it's live.
- While a source is live, the newest column eases between `--sonar` and
  `--sonar-2` on a 2 s cycle.
- A stale source (no sample for 10 s) goes still in `--dim`.
- `prefers-reduced-motion` turns all motion off.
- **The one exception** is `/watch`'s CRT effects (flicker, scanlines,
  beam, glow): on that page only, CSS only, off under
  `prefers-reduced-motion`, and toggled with `f`.

## Behaviour

- **Live data.** There is one stream per page: `GET /api/stream?topics=…`,
  in server-sent events format. The browser's `EventSource` can't send an
  `Authorization` header, so the UI reads the stream with `fetch()` and a
  roughly 30-line line parser. The session cookie rides along with it.
  The UI never polls: a page paints from its GETs once, then the stream
  keeps it current.
- **Stream topics.** A page asks only for the topics it shows:
  - `job` and `job:{id}`: jobs and one job's timeline;
  - `session`: live sessions and heartbeats;
  - `summary`: the header-strip numbers (queue, WIP, $ today, rates);
  - `talk:{id}`: one coordinator's thread;
  - new for `/watch` (phase 3): `code`, the active worker's latest edit;
    `events`, every event as it is stored; `tower`, GPU, VRAM, W and slots
    (empty before phase 5).
  - Every event and every `code` payload passes through one redactor,
    `vigild.redact.redact`, the same function the event log and the
    gateway use, before it is stored or published.
- **Login, not a stored token** (P7 is no, owner 2026-10-22). The UI never
  keeps a token, in storage or in page memory:
  - The login form posts the admin (or read) token once to
    `POST /api/login`. vigild answers with a `vigil_session` cookie and the
    page forgets the token.
  - The cookie is a random id, stored hashed in vigild: `HttpOnly`,
    `SameSite=Strict`, `Path=/`, 12 hours, absolute (log in again after).
  - `Secure` is set when the UI is reached over HTTPS through
    `tailscale serve` (`api.cookie_secure: true` in `configs/vigild.yaml`).
    Over plain HTTP on the tailnet it can't be set, because the browser
    would never send the cookie back; the tailnet's WireGuard encryption is
    the transport there.
  - Every write the UI makes carries `X-Vigil-UI: 1`; vigild refuses a
    cookie-authenticated write without it, so a cross-site form can't
    write even if a browser ignored SameSite.
  - On a 401 the UI shows the login form. `POST /api/logout` ends the
    session.
- **Where it's served.** The UI binds to the Mac's Tailscale address only,
  never `0.0.0.0`. The CLI and MCP keep bearer tokens.
- **A live row pulses** in `--sonar` once per update, for 600 ms.
- **Keyboard:**
  - `/` focuses the filter.
  - `j` / `k` move the selected row.
  - `Enter` opens the row.
  - `Esc` clears the filter.
  - `x` on `/job/:id` asks to kill the running session.
  - On `/job/:id`, `Enter` on a timeline line expands it.
  - `f` on `/watch` (only there) turns the CRT effects on and off; the
    choice isn't saved.
- **Pages, not overlays.** No modals: detail is a page, and expanding a
  timeline line opens it in place.
- **Width.** No page scrolls horizontally at 1280px. Long values (branch
  names, commands) are cut with `…`, and the full text is in the `title`.
- **Page weight** is under 100 KB per page, including the HTML, the CSS,
  every JS module and the mark. A test in `tests/web/` fails over the
  limit.
- **Before phase 5**, tower fields (GPU busy, slots, W, J/token) show
  `—`. They are never hidden, so the layout doesn't move when the tower
  arrives.

## Pages

These nine pages, and nothing else. The Vigil UI canvas is the visual
reference for all nine, Talk and Watch included. Each page lists the API calls
behind it; the UI calls nothing else. A **job** is the UI's word for a task (the API says `tasks`): one
contract, and every session (attempt, review, audit) it has spawned.

### 1. `/`: Board

One table with every job:
- id, repo, branch;
- tier, backend, state;
- turns, elapsed;
- tokens in/out, $ or GPU-s;
- PR, CI, review rung, trace link.
- **Evidence** (planned, `docs/design.md` "PR evidence pack"): three marks,
  `rec` (recording), `diag` (Mermaid diagram) and `foot` (the PR footer).
  Each is `made`, `skipped` or `pending`. A skipped mark shows its reason
  on hover and on `/job/:id`: `no_ui_path`, `verify_failed`, `no_flow`,
  `no_recorder_image`, `budget`, `error`, `fewer_than_3_modules`,
  `no_local_model` (before phase 5), `diagram_invalid`. The cell reads
  artifact rows; it holds no state and never changes a CI or gate column.

Typing filters the table. `Enter` opens `/job/:id`.

Above the table is a one-line header strip:
- queue depth;
- WIP per repo (`parkagent 2/4`);
- $ today against the cap (`$1.84 / $5.00`);
- GPU busy;
- llama-server slots in use;
- escalation rate over 7 days.

**API:** `GET /api/tasks`, `GET /api/summary`, and the stream's `job` and
`summary` topics.

### 2. `/job/:id`: Detail

Three columns.

- **Left:**
  - the contract: goal, `test_cmd`, `done_when`, files in scope, out of
    scope;
  - limits;
  - `tier_reason`;
  - backend;
  - why, value and risk;
  - the decider's answers, each with its confidence and which backend
    answered.
- **Centre:** the live timeline, newest at the bottom, one line per
  event:
  - a model request, with model, tokens in/out/cache and latency;
  - a tool call, with its command or path;
  - a permission decision (allowed or denied, and by which layer);
  - a review finding;
  - a state change.

  Each line expands to full content.
- **Right:** meters for this job.
  - Tokens and cost against its cap.
  - GPU, CPU, RAM and W as per-second character sparklines.
  - J/token and GPU-s/token.
  - A link to the Langfuse trace.
- **Kill.** `x` puts an inline line under the header: `kill pa-0142? y/n`.
  `y` stops the running session (`POST /api/sessions/{id}/kill`), the same
  call as `vigil kill`.

**API:**
- `GET /api/tasks/{id}`
- `GET /api/tasks/{id}/events?after=…` (paged)
- `GET /api/events/{id}` (full content for one line)
- `GET /api/metering?task={id}`
- `POST /api/sessions/{id}/kill`
- the stream's `job:{id}` topic

### 3. `/sessions`

Every live session Vigil knows about, each with what it is doing right
now and its last tool call:
- vigil workers, reviewers and auditors;
- any outside session that registers through the opt-in heartbeat
  endpoint, such as vigil-coord, or ParkAgent's coord if you wire it.

**Heartbeats.**
- **Endpoint:** `POST /api/heartbeat` with
  `{name, repo, branch, doing, last_tool, at}`.
- **Token:** the heartbeat-scoped `VIGIL_HEARTBEAT_TOKEN`, which can
  write heartbeats and nothing else.
- **Expiry:** a heartbeat expires after 5 minutes.
- **Docs:** the phase 3 PR adds a "Heartbeats" section to the runbook.
- **Opt-in:** coord never adds it to ParkAgent; you do, if you want it.

**API:** `GET /api/sessions?live=1`, `GET /api/heartbeats`, and the
stream's `session` topic.

### 4. `/decider`

Every decider question asked today:
- the question;
- an excerpt of the state it was asked about;
- the answer and its confidence;
- the backend that answered;
- what vigild did: acted, escalated, or waited;
- agreement with your later label.

**Labels** are set inline on the row: `l` opens a select in place, and
`Enter` saves it (`POST /api/decisions/{id}/label`). The CLI does the same
with `vigil label <decision> <answer>`. The eval reads these labels.

**Calibration:** a character histogram per backend, showing accuracy per
confidence bucket against your labels.

**API:** `GET /api/decisions?since=…`, `GET /api/decider/calibration`,
`POST /api/decisions/{id}/label`.

### 5. `/review`

The trust ladder. For each task shape:
- its rung;
- a sparkline of the audit match rate;
- false findings per PR;
- the promotions and demotions, with dates and the audit numbers behind
  each.

Money, auth and consent shapes are marked `rung 0 · fixed`.

**API:** `GET /api/ladder`.

### 6. `/new`

Start a session. It is one form, not a wizard, with these fields:
- **Repo:** only those in `configs/repos.yaml`.
- **Base and branch.**
- **Backend:** a preset, or a custom endpoint as `{kind, base_url,
  model, key_ref}`. `key_ref` is a choice among variable names, never a
  value field.
- **The contract's spec:** goal, `test_cmd`, `must_fail_first`,
  `files_in_scope`, `done_when`, `out_of_scope`.
- **Why, value and risk.**
- **Limits:** max turns, minutes and cost, never above the per-task cap.

The server's spec gate, intake gate, private-repo rule and budget check
answer inline. The form never re-implements them.

**Batch.** Below the form, a textarea takes a batch YAML file, the same
format `vigil batch` reads. It submits to `POST /api/batches`, and each
task passes the same gates.

**API:** `GET /api/repos`, `GET /api/backends`, `POST /api/tasks`,
`POST /api/batches`.

### 7. `/budget`

- **Caps against spend:**
  - per day for the last 30 days, as character bars (`█` spent, `▓`
    reserved, `░` remaining);
  - today's open reservations;
  - the month against the monthly cap.
- **WIP limits** per repo, with the open fleet PRs.
- Caps are read-only here. They live in `configs/budgets.yaml`, the same
  for every interface.

**API:** `GET /api/budgets`.

### 8. `/talk`

A conversation with a coordinator: the standing fleet coord, or a job's
coord. It is the web client of the bridge (`docs/design.md`, "Talking to
coordinators from anywhere").

- **Left:** the coordinators, each with its job and current backend.
- **Centre:** the thread, newest at the bottom.
  - Your messages are marked `human · web`.
  - Messages relayed from Claude or a local chat show their channel and
    relay model.
  - A send box sits under the thread.
- **Backend switch:** a select above the thread.
  - Switching starts a fresh session on the new backend, with the thread
    as context, and the switch shows as a line in the thread.
  - A local backend is offered only for shapes at trust rung 3.
- **A conversation surface, not a decision surface.** Nothing on `/talk`
  approves, merges, or dispatches. When a coord needs a decision, it
  files an inbox item, and the thread shows a link to it.

**API:** `GET /api/coords`, `GET /api/coords/{id}/thread`,
`POST /api/coords/{id}/messages`, `POST /api/coords/{id}/backend`, and
the stream's `talk:{id}` topic.

### 9. `/watch`: Watch

A full-screen ambient display, made to be left open on a screen. The page
fills the viewport and has no nav chrome. Going fullscreen is the
browser's own control: the page never calls the Fullscreen API.

**Layout.** A 12-column pane grid. No horizontal scroll at 1280px, and it
reads well at 1920px and wider (the panes grow; the type doesn't).

| Row | Panes (column span) |
|---|---|
| 1–2 | Live code stream (8, two rows tall) · Throughput (4) · Funnel (4) |
| 3 | Cost burn (4) · Tower meters (4) · Sessions (4) |
| 4 | Quality strip (12) |
| 5 | Event log (12) |

**Panes:**
- **Live code stream:** the active worker's latest edit, from the
  session proxy (phase 2), with its repo, file path and worker id. It is
  redacted on the server by `vigild.redact.redact`, the one redactor the
  event log and the gateway use, before it enters the stream. With no
  active worker, the last edit stays, in `--dim`.
- **Throughput:** jobs started and finished, and PRs opened and merged,
  per hour, as sparklines.
- **Funnel:** the count of jobs in each state, `spec_pending` through
  `done`, as bars.
- **Cost burn:** $ per hour, and today against the daily cap as a bar.
- **Tower meters:** GPU, VRAM, W and slots; `—` before phase 5, as
  everywhere.
- **Sessions:** a compact `/sessions`: one line per live session.
- **Quality strip:** the KPIs: first-pass merge rate, findings per PR by
  severity, revert + fix-job rate, cost per merged PR, median spec→merge.
- **Event log:** a tail of the event stream.

**CRT effects** (flicker, scanlines, beam, glow), on this page only:
- CSS only: no `<canvas>`, no SVG, no library, as everywhere.
- Off under `prefers-reduced-motion`.
- `f` toggles them; the toggle isn't saved.

**API:** the same API as every page. First paint from `GET /api/summary`,
`GET /api/tasks`, `GET /api/sessions?live=1` and `GET /api/budgets`; then
one stream, `GET /api/stream?topics=code,summary,session,events,tower`.
No polling. The 100 KB page-weight limit applies.

**Depends on:** the phase 2 session proxy (the code stream) and phase 2
review data (the quality strip's findings); tower meters wait for phase 5.

## Files

```text
vigild/web/
  index.html        one shell; routes by path
  app.css           the palette, type and hairlines; nothing else
  js/api.js         fetch (session cookie, X-Vigil-UI on writes) + stream reader
  js/glyphs.js      sparkline, bar, timeline, meter, histogram as pure functions over numbers → strings
  js/keys.js        / j k Enter Esc l, and f on /watch
  js/pages/*.js     one module per page
  assets/mark.txt   the logo, both sizes
```

`glyphs.js` is pure and unit-tested: the same numbers always give the same
string.

## Done when (phase 3)

- A session started from `/new` shows live on `/`.
- Its `/job/:id` timeline shows each model call. The per-call costs sum
  to the session total within a cent.
- `/metrics` shows its counters.
- Each page is under 100 KB, with no horizontal scroll at 1280px.
- `/`, `j`, `k` and `Enter` work.
- The PR includes a screenshot of each of the nine pages.
- `/watch` (`tests/web/test_watch.py`):
  - a secret-shaped string planted in a worker's edit never reaches the
    `code` topic;
  - under `prefers-reduced-motion` the CRT effects are off;
  - after first paint the page makes no request but its one stream (no
    polling).
