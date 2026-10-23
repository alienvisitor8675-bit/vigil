# Sources

What Vigil's versions and facts rest on. Checked **2026-10-22**.

**The pin rule.** A third-party version may be pinned only if it was
released at least two weeks earlier, which today means on or before
2026-10-08.
- A pin dated 2026-10-08 meets the rule on the boundary day.
- Our own code is pinned by commit SHA and isn't subject to the rule.
  That covers claude-mods, read at `c0fd9b19c4742dba84543e351a41e0faa60c84c8`
  (2026-10-21).
- **One owner-granted exception:** Claude Code CLI **2.1.306** (released
  2026-10-21) in the executor image, because Claude Code is the harness and
  guard is verified on it (Q1, owner 2026-10-22).
- **PyPI pins are enforced, not just listed:** `pyproject.toml` sets
  `[tool.uv] exclude-newer = "2026-10-08T23:59:59Z"`, so transitive
  dependencies obey the rule too.
- **Docker base images are pinned by digest.** Official images are rebuilt
  in place for OS patches. A digest resolved on 2026-10-22 is the current
  build of a release that itself meets the rule (CPython 3.14.8, Node
  24.21.0), so the OS layer may be newer than the cut-off.

When a phase adds or bumps a pin, it updates this file in the same PR.

## Pins

| Component | Pin | Released | Newest seen | Where it's used | Source |
|---|---|---|---|---|---|
| CPython | **3.14.8** (P3, owner 2026-10-22; `uv python install 3.14.8`) | 2026-09-30 | 3.15.0 (2026-10-09) | vigild, CLI, executor harness | https://www.python.org/downloads/ · https://peps.python.org/api/release-cycle.json |
| uv | 0.12.24 | 2026-10-08 | 0.13.5 | builds, lockfiles, CI | https://pypi.org/project/uv/ |
| fastapi | 0.143.0 | 2026-10-08 | 0.159.1 | vigild API, native SSE | https://pypi.org/project/fastapi/ · https://fastapi.tiangolo.com/release-notes/ |
| uvicorn | 0.54.0 | 2026-09-25 | same | vigild server | https://pypi.org/project/uvicorn/ |
| pydantic | 2.14.0 | 2026-10-08 | 2.14.1 | contract models | https://pypi.org/project/pydantic/ |
| httpx2 | 2.13.1 | 2026-09-23 | 2.16.0 | the one HTTP client (B9) | https://pypi.org/project/httpx2/ · https://github.com/pydantic/httpx2 |
| mcp | 2.3.0 | 2026-10-02 | same | `vigil mcp` (`MCPServer`) | https://pypi.org/project/mcp/ · https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md |
| typer | 0.27.3 | 2026-10-06 | same | CLI | https://pypi.org/project/typer/ |
| PyYAML | 6.0.3 | 2025-09-25 | same | configs | https://pypi.org/project/PyYAML/ |
| opentelemetry-api / -sdk / -exporter-otlp-proto-http | 1.45.1 | 2026-10-06 | same | vigild spans | https://pypi.org/project/opentelemetry-sdk/ |
| prometheus-client | 0.26.0 | 2026-07-24 | same | `/metrics`, tower exporter | https://pypi.org/project/prometheus-client/ |
| PyJWT | 2.15.1 | 2026-09-28 | same | GitHub App JWT (RS256) | https://pypi.org/project/PyJWT/ |
| cryptography | 50.0.2 | 2026-09-30 | same | RS256 signing | https://pypi.org/project/cryptography/ |
| docker (docker-py) | 7.2.0 | 2026-07-09 | same | container lifecycle | https://pypi.org/project/docker/ |
| pytest | 9.1.1 | 2026-06-19 | same | tests | https://pypi.org/project/pytest/ |
| pytest-asyncio | 1.4.0 | 2026-05-26 | same | tests | https://pypi.org/project/pytest-asyncio/ |
| ruff | 0.16.10 | 2026-10-01 | 0.17.1 | lint + format | https://pypi.org/project/ruff/ |
| mypy | 2.4.0 | 2026-10-01 | same | types | https://pypi.org/project/mypy/ |
| claude-agent-sdk (Python) | 0.2.165 (bundles CLI 2.1.294; vigil runs its own CLI through `cli_path`) | 2026-10-08 | 0.2.172 | Claude executor image | https://pypi.org/project/claude-agent-sdk/#history · https://github.com/anthropics/claude-agent-sdk-python |
| Claude Code CLI | **2.1.306**, the owner's exception (Q1) | 2026-10-21 | 2.1.308 | Claude executor (`cli_path=/usr/local/bin/claude`) | https://www.npmjs.com/package/@anthropic-ai/claude-code |
| Node.js | 24.21.0 (LTS "Krypton"), only to run the CLI; `node:24.21.0-trixie-slim@sha256:173f1258…fb66` | 2026-09-07 | 24.22.1 | Claude executor image | https://nodejs.org/dist/index.json |
| Python base image | `python:3.14.8-slim-trixie@sha256:fbecd461…ed4a` | 2026-09-30 (CPython) | — | executor and proxy images | https://hub.docker.com/_/python |
| OpenCode | v2.0.26 (`@opencode/cli`, linux-arm64 package) | 2026-10-08 | v2.1.3 | OpenCode executor (phase 4) | https://opencode.ai/changelog · https://github.com/anomalyco/opencode |
| llama.cpp | b11513 (ubuntu-cuda-12.8 / 13.4 x64 binaries, sm_120 included) | 2026-10-08 | b11819 | tower (phase 5) | https://github.com/ggml-org/llama.cpp/releases/tag/b11513 |
| nvidia-ml-py | 13.615.71 | 2026-09-25 | same | tower exporter | https://pypi.org/project/nvidia-ml-py/ |
| psutil | 7.2.2 | 2026-01-28 | same | tower exporter | https://pypi.org/project/psutil/ |
| Arize Phoenix (self-hosted) | 20.19.0, `arizephoenix/phoenix:version-20.19.0-nonroot@sha256:f2109e09…11c9` (amd64/arm64) | 2026-10-01 | 20.24.0 | trace store (owner decision 2026-10-22) | https://github.com/Arize-ai/phoenix/releases/tag/arize-phoenix-v20.19.0 · https://hub.docker.com/r/arizephoenix/phoenix |
| OTel Collector Contrib | 0.162.0, `otel/opentelemetry-collector-contrib@sha256:39923a8e…74be6` (arm64) | 2026-09-29 | 0.163.2 | collector | https://github.com/open-telemetry/opentelemetry-collector-releases/releases |
| smokescreen | v0.1.0 `linux_arm64` tarball, SHA-256 `f15ddd0d…b7c9` (checked by `ADD --checksum`) | 2026-09-21 | same | egress proxy | https://github.com/stripe/smokescreen/releases/tag/v0.1.0 |
| Dockerfile frontend | `docker/dockerfile:1.28.0@sha256:bb22d981…7e5b` (the image index), the `# syntax=` line of every Dockerfile (phase 2a; `tests/test_pins.py` fails an unpinned one) | 2026-10-07 | same | every image build | https://hub.docker.com/r/docker/dockerfile/tags |
| Docker Desktop | 4.94.0; the engine here reports 29.9.2 | 2026-10-05 | 4.96.2 | Mac runtime | https://docs.docker.com/desktop/release-notes/ |
| Langfuse (option, not used) | v4.55.0, for a machine with ~16 GiB for the Docker VM | 2026-10-08 | v4.68.0 | — | https://github.com/langfuse/langfuse/releases |
| actions/checkout | v7.0.1 `3d3c42e5aac5ba805825da76410c181273ba90b1` | 2026-07-20 | same | CI | https://github.com/actions/checkout/releases |
| astral-sh/setup-uv | v10.2.0 `c18668ad3cf93ea998bef934396af7bb5c839dc7` | 2026-09-21 | v10.3.2 | CI | https://github.com/astral-sh/setup-uv/releases |
| Jev on OpenRouter | `typesafe/jev-1.13` (endpoint `jev-1.13-20260917`) | 2026-09-18 | jev-1.14 (2026-10-14) | decider | https://openrouter.ai/docs/guides/community/jev |
| Jev on Cloudflare | `typesafe/jev` (unversioned; answers report `jev-1.13.0`) | — | — | decider | https://developers.cloudflare.com/ai/models/typesafe/jev/ |

**Models** (Hugging Face; hashes go in `tower/models.lock` in phase 5):
- Qwen3.6-35B-A3B, unsloth UD-Q4_K_M, 22.13 GB: https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF
- gpt-oss-20b, ggml-org MXFP4, 12.11 GB: https://huggingface.co/ggml-org/gpt-oss-20b-GGUF
- Qwen3.8-27B (eval only), UD-Q3_K_XL, 13.15 GB: https://huggingface.co/unsloth/Qwen3.8-27B-GGUF
- LFM2.5-1.2B-Instruct, Q4_K_M, 0.73 GB (local decider): https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF

**Claude API models and prices** (per million tokens; `configs/prices.yaml`
is seeded from this). Columns are input / 5-minute cache write / 1-hour
cache write / cache read / output.
- Opus 5.5 (`claude-opus-5-5`): $4 / $5 / $8 / $0.20 / $20
- Sonnet 5.5 (`claude-sonnet-5-5`): $2 / $2.50 / $4 / $0.10 / $10
- Haiku 5.5 (`claude-haiku-5-5`), prompts up to 100k: $0.10 / $0.125 / $0.20 / $0.01 / $0.50
- Haiku 5.5, prompts over 100k: $0.50 / $0.625 / $1 / $0.05 / $2.50

Sources: https://platform.claude.com/docs/en/about-claude/models/overview · https://platform.claude.com/docs/en/about-claude/pricing

## Facts the architecture relies on

**Claude Agent SDK and Claude Code**
- **`ClaudeAgentOptions`:**
  - `permission_mode` (`bypassPermissions` is still valid; deny rules
    still apply in it; it refuses to run as root)
  - `hooks`
  - `plugins=[{"type":"local","path":…}]` (local paths only)
  - `setting_sources`, `settings`
  - `max_turns`, `max_budget_usd` (a client-side estimate)
  - `cli_path`
- **`ResultMessage`:** `total_cost_usd`, `usage` (main loop only),
  `model_usage` (per model, all calls, estimate), and the subtypes
  `error_max_turns` and `error_max_budget_usd`.
- Sources: https://code.claude.com/docs/en/agent-sdk/python ·
  https://code.claude.com/docs/en/permission-modes ·
  https://code.claude.com/docs/en/permissions
- **Telemetry:**
  - Switches: `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
    `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` (needed for traces),
    `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp`.
  - Spans: `claude_code.interaction`, `claude_code.llm_request`,
    `claude_code.tool`.
  - Event: `claude_code.api_request` (tokens + `cost_usd`).
  - `TRACEPARENT` is honored in SDK and `-p` runs.
  - `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` and
    `OTEL_LOG_TOOL_CONTENT` (60 KB cap) exist.
  - Hook spans need `ENABLE_BETA_TRACING_DETAILED`.
  - Sources: https://code.claude.com/docs/en/agent-sdk/observability ·
    https://code.claude.com/docs/en/monitoring-usage
- **Plugins in containers:**
  - Build with `CLAUDE_CODE_PLUGIN_CACHE_DIR` and run with
    `CLAUDE_CODE_PLUGIN_SEED_DIR`.
  - `CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1` for `-p` runs.
  - Git credentials come from the machine's credential helper.
  - Sources: https://code.claude.com/docs/en/plugins/org ·
    https://code.claude.com/docs/en/plugins/host-marketplace ·
    https://code.claude.com/docs/en/plugins/cli-reference
- **Non-Anthropic backends:**
  - OpenRouter's Claude Code setup
    (`ANTHROPIC_BASE_URL=https://openrouter.ai/api`) is guaranteed only
    with the Anthropic provider:
    https://openrouter.ai/docs/guides/guides/claude-code-integration
  - Anthropic doesn't support routing Claude Code to non-Claude models:
    https://code.claude.com/docs/en/llm-gateway
  - llama-server serves `/v1/messages`:
    https://huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp
- **Structured outputs:** GA through `output_config.format` (JSON
  schema), supported on Haiku 5.5:
  https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- **Key health check:** `GET /v1/models`:
  https://platform.claude.com/docs/en/api/models/list

**OpenCode v2**
- **Permissions:** an ordered `permissions` array of
  `{action, resource, effect}`.
  - The last match wins.
  - Shell resources are raw text and best effort.
  - `read` paths are relative inside the project.
  - Source: https://opencode.ai/v2/docs/permissions/ (the design's
    `v2.opencode.ai/permissions` link is dead).
- **Server API:**
  - Routes: `GET /api/info`, `GET /api/event` (volatile SSE),
    `POST /api/session`, `POST /api/session/{id}/prompt`,
    `GET /api/session/{id}/diff`, `POST /api/session/{id}/interrupt`,
    `POST /api/session/{id}/permission/{requestID}/reply`.
  - The password goes in `OPENCODE_PASSWORD`.
  - `serve` has no `--auto` (v2.0.26 `packages/cli/src/commands/commands.ts`).
  - Source: https://dev.opencode.ai/v2/docs/api
- **Providers:** the v2 shape is `providers.<id>.package`, `settings.baseURL`
  and `settings.apiKey`, and OpenRouter is built in:
  https://dev.opencode.ai/v2/docs/providers/
- **The design's v1 endpoints**, kept for comparison:
  https://opencode.ai/docs/server/

**llama.cpp (b11513)**
- **Flags:**
  - `--cpu-moe`, `--n-cpu-moe`, `--parallel`, `--metrics`,
    `--api-key[-file]`.
  - `--jinja` and `--slots` are defaults.
  - Router mode: `--models-dir`, `--models-max`, `--sleep-idle-seconds`.
- **Endpoints:**
  - `/metrics`: aggregate counters.
  - `/slots`: `id`, `id_task` (server counter), `is_processing`.
  - Per-response `timings`.
- **Request features:** `json_schema` / `response_format`, and `n_probs`
  (OpenAI `logprobs` maps to it).
- Source: https://github.com/ggml-org/llama.cpp/blob/b11513/tools/server/README.md
- **Context:** the design's manual page,
  https://manpages.debian.org/unstable/llama.cpp-tools/llama-server.1.en.html

**NVML**
- Power usage is in mW, averaged over 1 s (Ampere and newer).
- The total-energy counter is in mJ.
- Per-process memory comes from `nvmlDeviceGetComputeRunningProcesses`.
- Not checked on an RTX 5070 Ti specifically. Phase 5 runs
  `nvidia-smi -q -d POWER` on the tower first.
- Source: https://docs.nvidia.com/deploy/nvml-api/

**Arize Phoenix 20.19.0 (self-hosted; the trace store)**
- **One container** serving the UI and OTLP over HTTP on port 6006
  (`/v1/traces`); gRPC on 4317 is unused. Storage is SQLite under
  `PHOENIX_WORKING_DIR` (or Postgres via `PHOENIX_SQL_DATABASE_URL`). The
  `-nonroot` image runs as uid 65532, so its volume is mounted at
  `/home/nonroot`; a volume at `/mnt/data` fails with EACCES (checked).
- **Auth:** `PHOENIX_ENABLE_AUTH=true` with `PHOENIX_SECRET` (≥ 32
  characters). `PHOENIX_ADMIN_SECRET` works as a bearer token for the first
  system user, which is how the collector authenticates.
  `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` sets the first login.
- **Grouping:** the `openinference.project.name` resource attribute picks
  the project ("vigil"); the OpenInference `session.id` span attribute
  groups a session.
- **REST:** `GET /v1/projects/{name}/spans` (used by the compose test).
- Checked end to end on 2026-10-22: a vigild span sent to the collector
  appears in Phoenix (`tests/telemetry/test_spans.py`, docker).
- Sources: `src/phoenix/config.py` and
  `src/phoenix/server/api/routers/v1/spans.py` at tag
  `arize-phoenix-v20.19.0`.
- Licence: Elastic License 2.0. Self-hosted use for your own traces is
  fine per the owner.

**Langfuse v4 (self-hosted; an option, not used)**
- **Containers:** six (web, worker, postgres:17, clickhouse ≥ 25.12,
  redis 7, minio), all arm64. The docs recommend 4 cores, 16 GiB and
  100 GiB of disk, which is more than this Mac's 12 GiB Docker VM.
- **OTLP ingest:** HTTP only, at `/api/public/otel`, with Basic auth and
  the `x-langfuse-ingestion-version: 4` header.
- **Trace attributes:** `langfuse.trace.name`, `langfuse.session.id`,
  `langfuse.trace.tags` and `langfuse.trace.metadata.*`, set on every
  span.
- **Headless init:** `LANGFUSE_INIT_*`.
- **Licence:** MIT outside `ee/`; copyright ClickHouse, Inc.
- Sources:
  - https://langfuse.com/self-hosting/deployment/docker-compose
  - https://github.com/langfuse/langfuse/blob/main/docker-compose.yml
  - https://langfuse.com/integrations/native/opentelemetry
  - https://langfuse.com/self-hosting/administration/headless-initialization
  - https://langfuse.com/docs/opentelemetry/example-opentelemetry-collector
  - https://langfuse.com/compare/arize-phoenix (the design's Phoenix
    comparison)

**GitHub App installation tokens**
- **Minting:** `POST /app/installations/{id}/access_tokens` with
  `repositories` or `repository_ids` and a down-scoped `permissions`
  object. Tokens last one hour.
- **Revoking:** `DELETE /installation/token`, authenticated with the
  token itself.
- **The JWT:** RS256, `iat` set 60 s in the past, `exp` at most 10
  minutes ahead, and `iss` set to the client ID.
- **Workflows:** a push that touches `.github/workflows` needs the
  `workflows` permission.
- Sources:
  - https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
  - https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
  - https://docs.github.com/en/rest/apps/installations#revoke-an-installation-access-token
  - https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps

**smokescreen v0.1.0 (checked on this Mac, 2026-10-22)**
- With no TLS client certificates it can't tell roles apart, so
  `allow_missing_role: true` in its `--config-file` makes every request use
  the ACL's `default` policy (otherwise every request is a 407).
- `allow_addresses: ["192.168.65.254:8788"]` opens exactly the gateway's
  address and port in the private ranges smokescreen blocks by default;
  port 8787 on the same host stays refused.
- Flags and config keys: `smokescreen --help` in the built image.

**Docker Desktop networking**
- **Internal networks** have no default route:
  https://docs.docker.com/reference/cli/docker/network/create/
- **DNS on internal networks.** Moby 26+ stops external DNS on them
  (CVE-2024-29018), but that advisory says Docker Desktop was not
  affected. So DNS isn't treated as a boundary:
  https://security-tracker.debian.org/tracker/CVE-2024-29018
- **How Desktop routes container traffic:**
  https://www.docker.com/blog/how-docker-desktop-networking-works-under-the-hood/
- **Checked 2026-10-22:** from a container on an `internal` network,
  through the sidecar, `host.docker.internal` (192.168.65.254) reaches a
  server bound to the Mac's `127.0.0.1`. That is how workers reach the
  gateway. Direct connections out fail ("Network is unreachable").
- **Checked 2026-10-22:** without Files and Folders access to `~/Documents`,
  a bind mount from there hangs `docker start` until Docker Desktop restarts.
  The grant has since been given on this Mac. Mounts from the home directory
  and from `/private/var/folders` work either way.
- **Not verified:** that a Desktop container reaches a tailnet `100.x`
  address while the Tailscale app runs. Phase 5 tests it first.

**TypeSafe Jev**
- **What it is:** typed `noul`, `choice` and `score` questions with
  probabilities.
  - Up to 255 choices.
  - $0.042 per million input tokens; output free.
  - 70–500 ms.
  - Source: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- **Cloudflare route:**
  - `POST /client/v4/accounts/{account_id}/ai/run` with
    `{"model":"typesafe/jev","input":{…}}`.
  - Zero data retention.
  - 32k context.
  - The token needs Workers AI Read and Edit.
  - Source: https://developers.cloudflare.com/ai/models/typesafe/jev/
- **OpenRouter route:**
  - The Decisions API, `POST /api/alpha/decisions`.
  - `provider.zdr` is supported.
  - Every response carries `usage.cost`.
  - Source: https://openrouter.ai/docs/guides/community/jev

**OpenRouter**
- **Usage:** usage, including cost, is always returned, and
  `usage.include` is deprecated.
- **Data retention:** `provider.zdr` and `provider.data_collection` are
  set per request.
- **Keys:** management keys can create keys with credit limits.
- Source: https://openrouter.ai/docs

**Claude Code 2.1.306 in the container (checked 2026-10-22)**
- **Managed settings:** `/etc/claude-code/managed-settings.json` is read:
  - its `enabledPlugins` turns guard on;
  - its `claudeMods.guard.blockedMessage` is the message the guard test
    sees.
- **Plugin seeding:** at build time, `CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed`
  with the CLI's marketplace-add and install commands (see
  `images/claude-executor/Dockerfile`); at run time,
  `CLAUDE_CODE_PLUGIN_SEED_DIR`. The CLI's plugin list shows guard enabled.
- **The init message's `plugins`** lists guard, plus the CLI's built-ins
  `cc-plugin-sec-default`, `cc-plugin-agents-md` and
  `cc-plugin-plugin-authoring`.
- **`ANTHROPIC_BASE_URL` plus `ANTHROPIC_AUTH_TOKEN`** send every model
  call to vigild's gateway. The guard test and the real-container
  coord-loop test ran through it.

**The vigil-worker App (checked 2026-10-22 with `vigil doctor`)**
- `GET /app`, one installation, and a mint and revoke work on vigil and
  claude-mods.
- GitHub adds `metadata: read` to every installation token, even one asked
  for `{"contents": "read"}` alone.

## Not yet verified (checked in the phase that needs it)

- **Phase 1:** draft PRs on a private repo through the App. The phase 1
  PR itself is a draft on the private vigil repo, but it was opened with
  the owner's own `gh` login, not the App; the App's first draft PR is the
  done-when run.
- **Phase 5:** Docker Desktop container → smokescreen → tower over
  Tailscale. Phase 5 tests this first.
- **Phase 5:** NVML power on the RTX 5070 Ti.
- **Phase 5:** smokescreen accepting a bare tailnet IP in its allowlist,
  as opposed to the MagicDNS name.
