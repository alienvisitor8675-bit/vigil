# Deploy note: the owner's x86 machine as a sandbox host

**Status: for the owner to review before touching the machine.** Nothing here
has been run. This note carries no addresses, hostnames or IPs, because the
repo is public: write them in your own notes, and in `configs/sandbox_hosts.yaml`
only on a branch you do not publish with real values (see step 10).

Read `docs/decisions/ADR-0004-sandbox-hosts.md` and the runbook section "Add a
sandbox host" first; this is the same procedure as a checklist for one machine.

**What this machine becomes.** An amd64 sandbox host for `bench` tasks only
(`uses: [bench]`). It stays what it already is for everything else. If it also
serves models, rules 3 to 5 below are what keep sandboxes away from the model
server: sandboxes must not reach its model ports, and the slice must keep them
from starving it.

**Before you start.** Two things you need written down, not in the repo:
the machine's address on your private network (`SANDBOX_ADDR`), and the Mac
mini's address on the same network (`VIGILD_ADDR`). Both must be RFC 1918 or
in 100.64.0.0/10.

## On the x86 machine

1. **Make a dedicated user.** Create a user named `vigil` with no sudo, no
   password login, and membership of the `docker` group only.
   - Check: `id vigil` lists `docker` and nothing privileged.
2. **Install the key, restricted.** On the Mac mini, make a new ed25519 key
   used for nothing else (`ssh-keygen -t ed25519 -f <path outside the repo>`;
   do not reuse a key you already have). On the x86 machine, put exactly this
   one line in `~vigil/.ssh/authorized_keys` (directory mode 700, file mode
   600, owned by `vigil`), with the new public key in place of the placeholder:

   ```
   command="docker system dial-stdio",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding <the public key> vigil-sandbox
   ```

   - Check: the file holds that one line and no other.
   - Why: the key equals root on this machine. With this line it can speak the
     Docker API and nothing else. Doctor later tries a plain shell with it and
     fails if the shell works.
3. **Install and pin the Docker Engine.** Install a Docker Engine release at
   least two weeks old. Write down its exact version string (the output of
   `docker version --format '{{.Server.Version}}'`) as `ENGINE_PIN`, then hold
   the package so it does not upgrade by itself.
   - Check: `docker version` reports `ENGINE_PIN`.
4. **Turn on userns-remap.** Set `"userns-remap": "default"` in
   `/etc/docker/daemon.json`; also set the systemd cgroup driver
   (`"exec-opts": ["native.cgroupdriver=systemd"]`) and leave the default
   runtime as `runc`. Restart Docker. Do not use rootless Docker: it is not
   supported yet.
   - Check: `docker info` lists `name=userns` under Security Options, and the
     default runtime is `runc`, not a GPU runtime.
   - Note: images and containers that existed before the change are not
     visible after it (Docker keeps a separate store per remap). That is
     expected.
5. **Keep sandboxes off the GPU.** Do not set the `nvidia` runtime as the
   default. vigild never passes a device request.
   - Check: `docker info` shows `Default Runtime: runc`.
6. **Make the slice.** Create `vigil.slice` and cap it, with a drop-in at
   `/etc/systemd/system/vigil.slice.d/limits.conf`:

   ```
   [Slice]
   MemoryMax=<memory cap>
   CPUQuota=<cpus x 100>%
   ```

   Pick the caps so the machine's other work (a model server, say) keeps
   enough memory and cores. Run `systemctl daemon-reload`. The config's
   `memory_gb` and `cpus` must be these same numbers; doctor fails if the
   slice is missing or larger.
   - Check: `systemctl show vigil.slice -p MemoryMax -p CPUQuotaPerSecUSec`.
7. **Close the machine's other ports to the sandbox network.** The sandboxes
   run behind Docker's bridge networks and reach only vigild's gateway, but
   also add a firewall rule so that traffic from Docker's bridge ranges
   cannot reach this machine's own listening ports (SSH, any model server,
   any database). This is defence in depth; doctor's probe tests the result.
   - Check: from a throwaway container on the machine, a connection to the
     machine's own address on its model port fails.
8. **Free disk.** Decide the most space vigil's images and containers may
   use (`disk_budget_gb`), and make sure that much is free on Docker's data
   directory. vigild prunes only what carries vigil's own labels.

## On the Mac mini

9. **Create the Docker context.** The endpoint is `ssh://`, never `tcp://`:

   ```sh
   docker context create sandbox-x86 --docker "host=ssh://vigil@<SANDBOX_ADDR>"
   ```

   Use whatever context name you like. Make sure the new key is the one ssh
   offers for that target (an entry in your own ssh config, outside the repo).
   - Check: `docker --context sandbox-x86 info` works, and
     `ssh vigil@<SANDBOX_ADDR> echo hi` prints nothing and does not give a
     shell.
10. **Add the host to the config.** Add an entry to
    `configs/sandbox_hosts.yaml` (the commented template in that file lists
    every field), with `arch: amd64`, `uses: [bench]`, your caps, your
    `ENGINE_PIN`, `private_address: <SANDBOX_ADDR>`, and
    `gateway_url: http://<VIGILD_ADDR>:8788`, and set the file's
    `gateway_bind: <VIGILD_ADDR>`. **These values are addresses: keep them out
    of any commit or PR.** Keep that edit local (do not commit it), or keep it
    in a copy outside the repo that you point vigild at.
11. **Build the images on the x86 machine.** Sessions and graders never
    build. From the repo on the Mac mini:

    ```sh
    DOCKER_CONTEXT=sandbox-x86 uv run python scripts/build-images.py --claude-mods-repo <a local claude-mods checkout>
    ```

    - Check: `docker --context sandbox-x86 image ls` shows the executor and
      egress-proxy images.
12. **Run doctor.**

    ```sh
    uv run python cli/vigil doctor --repo thomasbardhi01/vigil
    ```

    Expect six passing lines for the host, `rule 1` to `rule 6`. A failing
    line says which rule and why. Fix the cause on the machine; do not edit
    the check.
13. **Start vigild.** It refuses to start while the host fails a rule, and
    with the host listed it also listens on `VIGILD_ADDR:8788`, accepting
    only `SANDBOX_ADDR`. Keep your private network's own access rules on too.
14. **After it works.** Once the host passes doctor and a bench task has run
    on it natively, turn off emulation by hand: set
    `bench.allow_emulation: false`.

## If something is wrong
- Doctor `rule 1` fails on "a plain shell ran": the `authorized_keys` line is
  not restricted, or another line for the same key exists. Fix the file.
- Doctor `rule 3` fails on "reached ...": a sandbox can reach something it
  should not; fix the firewall on the machine before anything else.
- To remove the host: delete its entry from the config, then
  `docker context rm sandbox-x86`, then delete the key from `authorized_keys`.

## Not built yet
Copying a worker's clone and packet to a remote host over the Docker API is
not written, so the host cannot run a session until it is. This checklist gets
the machine ready and proven safe by doctor.
