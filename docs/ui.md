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
| `--sonar-2` | `#3A7CA5` | Dark accent: fills, graph bodies, hover. |
| `--warn` | `--ink` text on a `--sonar-2` background | Anything that needs you: cap reached, guard denial, failed, escalated. |

- **Borders** are hairlines only: 1px `--dim` at 30% opacity.
- **Corners** are never rounded more than 2px.
- **Icons:** none from icon packs.

**Contrast, measured.** The palette stands as written unless you change
it.
- `--dim` on black is 4.48:1, a hair under WCAG AA's 4.5:1 for 12px text.
  `#707A85` would give about 4.8:1.
- The warn pair (`--ink` on `--sonar-2`) is about 3.9:1. WCAG AA allows
  that only for large text (18.7px bold or 24px regular), so 12–13px warn
  text fails. This is question Q3 in `docs/architecture.md`; until you
  answer, phase 3 uses the palette as written.

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

## Behaviour

- **Live data.** There is one stream per page: `GET /api/stream?topics=…`,
  in server-sent events format. The browser's `EventSource` can't send an
  `Authorization` header, so the UI reads the stream with `fetch()` and a
  roughly 30-line line parser. That keeps one auth mechanism, the bearer
  token, for everything.
- **Token.** The UI asks for the token and keeps it in page memory only,
  so it lasts until the tab is closed or reloaded. It is cleared on a 401.
  P7 in `docs/architecture.md` would keep it in `localStorage`.
- **A live row pulses** in `--sonar` once per update, for 600 ms.
- **Keyboard:**
  - `/` focuses the filter.
  - `j` / `k` move the selected row.
  - `Enter` opens the row.
  - `Esc` clears the filter.
  - `x` on `/job/:id` asks to kill the running session.
  - On `/job/:id`, `Enter` on a timeline line expands it.
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

These eight pages, and nothing else. The Vigil UI canvas is the visual
reference for all eight, Talk included. Each page lists the API calls
behind it; the UI calls nothing else. A **job** is the UI's word for a task (the API says `tasks`): one
contract, and every session (attempt, review, audit) it has spawned.

### 1. `/`: Board

One table with every job:
- id, repo, branch;
- tier, backend, state;
- turns, elapsed;
- tokens in/out, $ or GPU-s;
- PR, CI, review rung, trace link.

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

## Files

```text
vigild/web/
  index.html        one shell; routes by path
  app.css           the palette, type and hairlines; nothing else
  js/api.js         fetch + bearer token + stream reader
  js/glyphs.js      sparkline, bar, timeline, meter, histogram as pure functions over numbers → strings
  js/keys.js        / j k Enter Esc l
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
- The PR includes a screenshot of each of the eight pages.
