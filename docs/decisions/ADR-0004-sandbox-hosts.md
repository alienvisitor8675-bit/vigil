# ADR-0004: Sandbox hosts

- **Status:** accepted, 2026-10-27. The owner approved it with four changes (gateway source check, SSH key restrictions, userns-remap only, a separate build phase), now folded in, and answered both open questions.
- **Deciders:** the owner. Recorded by vigil-coord.
- **Related:** issue #8 (vigil bench). The design note and the owner's decisions are its 2026-10-27 comments.

## Context
vigil runs every session and every grader in the local Docker Desktop. On the Mac mini that is linux/arm64.

Every benchmark source vigil bench uses ships linux/amd64 images, and no arm64 ones were found: SWE-PolyBench, SWE-bench Pro V2 and SWE-bench Live. Under Rosetta, the measured cost is a 2.8–5.3× CPU slowdown. Two Pro V2 tasks still graded in 12–22 s (#8). That's fine for a pilot, but routine Core runs want native x86.

The owner's ruling, 2026-10-27:
- Hosts that run models and hosts that run sandboxes are **separate concepts**.
- A sandbox host is **generic**, and no code names a specific machine.

## Decision

### 1. Config: `sandbox_hosts`
`configs/sandbox_hosts.yaml` holds a list. Each entry has these fields:

| Field | Meaning |
|---|---|
| `name` | Free text, used in logs and doctor output. |
| `docker_context` | A Docker context name. The local Docker is the built-in default entry, `local`. |
| `arch` | `arm64` or `amd64`. Checked against `docker info`. |
| `memory_gb`, `cpus` | The caps of the host's dedicated slice. |
| `disk_budget_gb` | The most space vigil's images and containers may use. |
| `uses` | Any of `jobs` and `bench`. `local` defaults to both. |
| `gateway_url` | How this host's containers reach vigild's gateway. For `local` this is `http://host.docker.internal:8788`. |

Model hosts stay in `configs/backends.yaml` as they are. **No code in `vigild/` names a machine.** A test fails on any host name in `vigild/` that isn't read from config.

### 2. Scheduling
- **Architecture match.** A task runs on a host whose `arch` matches its image's architecture, read from the image manifest.
- **Normal jobs** run on `local`. Their images are built locally, so they're arm64. A host takes normal jobs only if `uses` lists `jobs`, and today only `local` does.
- **Bench tasks** go to a host whose `arch` is `amd64` and whose `uses` includes `bench`.
- **When no x86 bench host is configured:**
  - `bench.allow_emulation: true` runs the task on `local` under Rosetta. The run is labelled `emulated`, its timeouts are scaled, and a timeout is tagged `environment`. These are the pilot rules in #8.
  - Otherwise the run is refused, with a clear reason.
- **The verifier grades on the same host as the session**, in the task's derived image, with `--network none`.

### 3. Rules every remote sandbox host must meet
Doctor checks every rule, and vigild refuses to start if a configured remote host fails one.

1. **SSH only, private network only, with a restricted key.**
   - The Docker context endpoint is `ssh://…`. A `tcp://` endpoint is refused.
   - The SSH target must resolve to a private address (RFC 1918, or the 100.64.0.0/10 range a private overlay network uses).
   - The SSH key lives on the vigild host. The sandbox host holds no secrets.
   - **The SSH key equals root on the sandbox host.** Anyone holding it can drive that host's Docker daemon, and through it the whole machine. So the host's `authorized_keys` entry for that key must carry all of these:
     - `command="docker system dial-stdio"`
     - `no-pty`
     - `no-port-forwarding`
     - `no-agent-forwarding`
     - `no-X11-forwarding`

     The key can then do nothing but speak the Docker API.
   - **Doctor checks the restriction:** it opens a plain shell over that key, and the check fails unless the shell is refused.
2. **A pinned Docker Engine running with userns-remap.**
   - The engine version must equal **that host's** pin in `configs/sandbox_hosts.yaml`. Each host has its own pin.
   - `docker info` must show `userns` in its security options.
   - **Rootless mode is not supported** until the threat tests (`tests/security/test_threats.py`) pass under it.
3. **Egress only to vigild's gateway.**
   - Each session gets its own `internal: true` network and a smokescreen sidecar on that host. The sidecar's allowlist is exactly the host's `gateway_url`.
   - Doctor runs a probe container on the host. It must reach the gateway's health route. It must fail to reach the internet, the host's own other ports, any other address on the private network, and every model endpoint in `configs/backends.yaml`.
4. **No GPU.**
   - vigild never passes device requests or `--gpus`.
   - Doctor fails if the host's default runtime is a GPU runtime, or if the probe container sees a GPU device node.
5. **Caps in a dedicated slice.**
   - Every container vigild creates on the host gets `cgroup_parent` set to the host's vigil slice.
   - Doctor's probe reads that slice's memory and CPU limits, and fails if they're missing or larger than `memory_gb` or `cpus`.
6. **A disk budget, enforced by pruning.**
   - vigild prunes **only its own labelled** images and containers, least recently used first, when usage passes `disk_budget_gb`. That matches #26's rule: a cleanup only ever touches what carries vigil's own label.
   - Doctor reports usage against the budget.

These carry over **unchanged on every host**, local or remote:
- **Container flags:** `--read-only`, `--cap-drop ALL`, `no-new-privileges`, and pids and memory limits.
- **The agent user:** uid 10001.
- **No bind mounts.** Files go in only through `docker cp` or a tar stream over the Docker API.
- **No GitHub token** in any container.

### 4. The gateway on a private interface
- A remote sandbox host's containers reach the gateway at that host's `gateway_url`. So vigild also listens on its private-network address when any remote host is configured, and only then. The bind address is set in config.
- **vigild enforces the source:** the private listener accepts a connection only from a configured sandbox host's address, and drops any other source before any HTTP handling.
  - A test shows a connection from an unlisted address is refused.
  - The private network's access rules are **defence in depth**, not the boundary.
- The per-session gateway token is unchanged.

### 5. Image builds are a separate phase
Building an image needs network. Sessions and grading never get network. The two are separate phases, on separate code paths:
- **Builds** cover derived bench images (dependencies pre-installed), dev images and session layers. They run with network, as an explicit build step before any session or grader starts, and produce an image pinned by digest.
- **Sessions** run with egress through the proxy to the gateway only.
- **Grading** runs with `--network none`.

No session or grader ever triggers a build. A missing image refuses the run, naming the build to run first.

### 6. Docs
- **`docs/runbook.md`:**
  - **"One machine with Docker"** stays the default path, with no sandbox host config needed.
  - **"Add a sandbox host"** is a new optional section: the six rules, how to create the SSH context and slice, and doctor's checks.
- **The owner's own setup** goes in a separate deployment note under `docs/deploy/`: a specific x86 machine on the owner's private network, configured as a sandbox host for bench. It carries no addresses, because the repo is public.

## Consequences
- The bench pilot can run natively on an x86 sandbox host, and on the Mac mini under emulation where no such host is configured.
- vigild gains a host-selection step before every session and grader run. For normal jobs it always picks `local`, so their behaviour is unchanged.
- Doctor grows a probe container per remote host. Each probe takes seconds and runs only when a remote host is configured.
- A machine that also serves models can be a sandbox host only if it passes rules 3–5. Its model ports must be unreachable from sandboxes, and the slice keeps sandboxes from starving the model server.

## Alternatives considered
- **Emulation only.** Simplest, and kept as the fallback. But it's 2.8–5.3× slower on CPU-bound suites, and an emulation timeout tells us nothing about the model.
- **A Docker TCP socket, with or without TLS.** Rejected: a network-exposed daemon socket is root on the host. SSH over a private network keeps the daemon off the network.
- **Naming the x86 machine in code**, for example as a special case of the model host. Rejected by the owner: model hosts and sandbox hosts are separate, and the product must work on one machine.
- **Normal jobs on remote hosts too.** Deferred. `uses: [jobs]` allows it later, but today normal jobs stay local.

## Owner's answers (2026-10-27)
1. **The Docker Engine pin is per host.**
2. **`bench.allow_emulation` is on for the pilot.** The owner turns it off by hand once the x86 sandbox host passes doctor.

**Before anyone touches the owner's x86 machine,** the owner reviews two documents: the deployment note in `docs/deploy/` and the "Add a sandbox host" runbook section.
