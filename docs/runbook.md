# Runbook

Operating Vigil by hand. This first version holds only **Credentials**;
start, stop, rotate and recover sections arrive with the phases that build
those things.

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

- **vigild loads that file once at startup** and never writes it. The
  CLI and `vigil mcp` read `VIGIL_API_TOKEN` from it too, so there is no
  second token file.
- **The Docker Compose stack (Langfuse and the OTel collector) reads its
  own secrets** from the same file through `docker compose --env-file
  ~/.config/vigil/env`. Those are the only containers that hold secrets
  in their environment, and none of them runs agent code.
- **No worker container gets a secret in its environment.**
  - The executor gets its model key as a read-only file mount at
    `/run/secrets/model_key`. The `key_ref` of the session's backend names
    which variable vigild writes into that file.
  - The agent's own uid can read that file. Only the guard and
    managed-settings deny rules stop it (`docs/architecture.md` B15).
  - A worker that needs GitHub gets a one-hour installation token, never
    the App's private key.
  - Langfuse keys stay in the collector.
- **The tower holds exactly one secret**, `LLAMA_API_KEY`, in
  `/etc/vigil/llama.env` (root, mode `600`). Its llama-server unit reads
  that file. The Mac keeps the same value in its own env file so vigild can
  call the server.
- **Nothing here is ever committed.** `.env.example` at the repo root lists
  the names with no values.
- **A Claude session (coord or worker) never reads, prints or writes any
  of these.**
  - It never opens `~/.config/vigil/` or any `.env*` file other than
    `.env.example`.
  - vigild checks a key only through `vigil doctor` (phase 1). That command
    makes one health call per credential and prints `pass` or `fail` and
    the variable name, never the value.

### Variables

The **Phase** column says when vigild first needs the variable. Until then
it can be missing. A missing optional variable switches only its feature
off, and `vigil doctor` reports which ones are off.

| Variable | Purpose | Where to get it | Scope / limits | Phase | Health check (`vigil doctor`) |
|---|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude executor, reviewer, auditor, and the Haiku decider. | Console → org **vigil** → API keys. It already exists. | Org "vigil" only. Set a Console spend limit at or under the monthly credit ($100–200); vigild's own caps sit below it (daily $5, per task $2, monthly see `configs/budgets.yaml`). | 1 | `GET https://api.anthropic.com/v1/models` → 200 |
| `VIGIL_GITHUB_APP_CLIENT_ID` | Identifies the vigil-worker App when vigild signs its JWT. Not secret, but kept with the key it pairs with. | GitHub → Settings → Developer settings → GitHub Apps → vigil-worker → **Client ID**. | — | 1 | `GET /app` with the JWT → 200 |
| `VIGIL_GITHUB_APP_KEY_PATH` | Path to the App's private key, normally `~/.config/vigil/vigil-worker.pem`. | Same page → **Generate a private key**. Save the `.pem` beside the env file and `chmod 600` it. | Never mounted into any container. vigild mints per-task installation tokens from it. | 1 | same as above, plus `GET /app/installations` finds the one installation on your account, and `GET /installation/repositories` (with an installation token) lists vigil, parkagent and claude-mods |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The OTel collector sends traces to Langfuse with these (HTTP Basic auth). | Generate both yourself, e.g. `printf 'pk-lf-%s' "$(openssl rand -hex 16)"` and `printf 'sk-lf-%s' "$(openssl rand -hex 24)"`. Langfuse's headless init (`LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY`) creates the project with them on first start. | One Langfuse project, "vigil". | 1 | `GET <langfuse>/api/public/health`, then an authenticated `GET /api/public/projects` → 200 |
| `LANGFUSE_INIT_USER_EMAIL`, `LANGFUSE_INIT_USER_PASSWORD` | Your Langfuse login, created on first start. | Choose them. | Local only (Tailscale). | 1 | login page loads |
| `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_POSTGRES_PASSWORD`, `LANGFUSE_CLICKHOUSE_PASSWORD`, `LANGFUSE_REDIS_PASSWORD`, `LANGFUSE_MINIO_PASSWORD` | Langfuse's own internals, read only by the Compose stack. | `openssl rand -hex 32` for each. It prints 64 hex characters, which is exactly the length `LANGFUSE_ENCRYPTION_KEY` requires. | Compose services only. | 1 | Compose services healthy |
| `CLOUDFLARE_ACCOUNT_ID` | Account for Workers AI, where the Jev decider runs (zero data retention). | Cloudflare dashboard → right sidebar of any zone or Workers page. | — | 1 (Jev; Haiku covers until then) | `POST …/ai/run` with a one-question Jev request → 200 |
| `CLOUDFLARE_API_TOKEN` | Calls Jev on Workers AI. | Cloudflare → My Profile → API Tokens → Create Custom Token: **Account · Workers AI · Read** and **Account · Workers AI · Edit**, limited to that one account, with a TTL if you want one. | Workers AI only. | 1 (Jev) | same |
| `OPENROUTER_API_KEY` | The OpenRouter backend preset, and Jev's alternative route. | openrouter.ai → Keys → Create key, with a **credit limit** (for example $10, monthly reset). | That key's limit only. Never used for a private repo: vigild refuses to start if `configs/repos.yaml` lists it for one. | 4 (OpenCode's second backend), or earlier if you want the preset sooner | `GET https://openrouter.ai/api/v1/key` → 200 |
| `LLAMA_API_KEY` | llama-server's `--api-key`; vigild, the exporter scrape and OpenCode workers send it. | `openssl rand -hex 32`. Put it in **both** `~/.config/vigil/env` and the tower's `/etc/vigil/llama.env`. | Tower only, reached over Tailscale. | 5 | `GET http://<tower>:8080/health` with the key → 200 |
| `VIGIL_API_TOKEN` | Admin bearer token for the REST API, the CLI, the MCP server and the web UI. | `openssl rand -hex 32`. The CLI and `vigil mcp` read it from this same env file. The web UI asks for it and keeps it in page memory only. | Full control of vigild. | 1 (API), 2 (CLI/MCP) | `GET /api/health` with it → 200 |
| `VIGIL_METRICS_TOKEN` | Read-only bearer token for `/metrics` and `/api/metering`, for Hearth and Prometheus. | `openssl rand -hex 32`. Hand it to Hearth yourself. | Read-only metering. | 3 | `GET /metrics` with it → 200 |
| `VIGIL_HEARTBEAT_TOKEN` | Lets an outside session (vigil-coord, or ParkAgent's coord if you wire it) post heartbeats for the UI's `/sessions` page. | `openssl rand -hex 32`. Give it only to sessions you want listed. | Write heartbeats; nothing else. | 3 | `POST /api/heartbeat` with it → 204 |

Non-secret settings (hosts, ports, the tower's Tailscale name, the Langfuse
URL) live in `configs/*.yaml`, not here.

### The GitHub App "vigil-worker"

Make it once. vigild uses it for every GitHub call a worker or reviewer
needs, and nothing else.

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.
   - Name `vigil-worker`. Homepage: the vigil repo URL.
   - Webhook: **off** for now (intake polls; a webhook comes later, if ever).
   - **Repository permissions:**
     - Contents: Read and write
     - Pull requests: Read and write
     - Issues: Read-only (the `vigil:ready` label intake reads issues)
     - Metadata: Read-only (always on)
   - Everything else: No access. In particular **no Administration, no
     Workflows, no Secrets, no Actions write**.
   - Where can it be installed: **Only on this account**.
2. Create it. On its page, copy the **Client ID** into `VIGIL_GITHUB_APP_CLIENT_ID`.
   Then **Generate a private key** and move the `.pem` to
   `~/.config/vigil/vigil-worker.pem`, then `chmod 600` it.
3. **Install** it (Install App → your account → Only select repositories):
   - `thomasbardhi01/vigil`: needed from phase 1 for CI-path tests, and
     from phase 2 for self-maintenance.
   - `thomasbardhi01/claude-mods`: needed so the Claude executor image can
     fetch the guard plugin. **This install is read-only in practice, not
     by GitHub's settings.** A GitHub App has one permission set on every
     installation, so GitHub would let this install write too. vigild
     always mints claude-mods tokens with
     `{"permissions": {"contents": "read"}}` and the single repository,
     and a test asserts that no code path asks for more. If you want
     GitHub itself to enforce read-only, make a second App
     (`vigil-reader`, Contents: Read only) for claude-mods instead, and
     tell coord.
   - `thomasbardhi01/parkagent`: needed for phase 1's done-when check.
     Coord asks you before any worker touches ParkAgent.
4. Run `vigil doctor` (phase 1). It should find the one installation, list
   its three repositories, and mint and then revoke one test token per repo.

How tokens are used:
- **One token per task**, for that task's repository only, valid for one
  hour and revoked (`DELETE /installation/token`) when the container exits.
- **A token never enters an environment variable.** It sits in a tmpfs
  file that a `git credential` helper reads.
- **claude-mods tokens exist only during an image build.** They are passed
  as a BuildKit secret, so they never land in an image layer.

### Ruleset on `main` (every target repo)

The App can't merge, approve or push to `main`, and GitHub enforces each of
those. Every repo the fleet works in needs a `main` ruleset that does
these things:
- requires a pull request;
- blocks force pushes and deletion;
- requires the CI checks (for vigil, the command is in the phase 1 PR);
- has **no bypass actor** that includes the App.

### What a Claude session never does here

Coord and the workers never run any of these:
- create, read, print, store or commit a secret;
- open `~/.config/vigil/`;
- run `gh secret set`;
- install the App;
- edit a ruleset.

When a phase needs a credential, coord stops, posts these steps in the
status issue (#1), and waits for you.
