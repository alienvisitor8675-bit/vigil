"""The Claude executor's harness: PID 1 in the container, as root.

Protocol with vigild:
- stdin, one JSON line: {"gateway_token": "..."}. Never in the environment
  docker sees; the token goes only into the CLI child's environment.
- /vigil/packet.md (read-only mount): the task packet, the whole prompt.
- /work/.vigil/task.json: the contract (worker) or the job request (coord).
- env: VIGIL_ROLE, VIGIL_MODEL, VIGIL_GATEWAY_URL, VIGIL_MAX_TURNS,
  VIGIL_MAX_BUDGET_USD, VIGIL_REQUIRE_PLUGIN, VIGIL_AGENT_UID, plus OTEL_*.
- stdout: event lines `{"vigil_event": {...}}` while running, and one final
  `{"vigil_result": {...}}` line naming the head commit.

Two users (review round 2, N1 and N11). The harness is root, with only the
capabilities to change uid and to kill. Everything that runs code from the
repo or the model runs as the agent uid (10001) with no capabilities: the CLI
(through /opt/vigil/agent-cli) and git. The agent uid can't signal PID 1, read
its memory or write its stdout (/proc/1/fd/1), so every line vigild reads from
the container is the harness's own.

The harness runs no tests: test results come only from vigild's verifier,
which runs test_cmd in fresh containers on commits taken from the vault (owner
direction, round 2). After the agent, the harness kills every other process,
then commits what the agent left and writes the branch as
/vigil/out/result.bundle (from the base commit vigild recorded), with git
pinned against the repo's own config and a .git that points elsewhere refused.
vigild imports the bundle into its vault and refuses one whose branch tip
isn't the head named on the result line (the out dir is a bind mount, which on
Docker Desktop ignores file modes). The harness never pushes. Tool inputs and
results are redacted of anything token-shaped before they are emitted (item 13).
Exit codes: 0 done, 2 bad input, 3 guard not loaded, 4 agent error,
5 the work tree can't be committed safely, 6 the session-prepare hook failed.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import fnmatch
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

WORK = Path("/work")
HOOK_DIR = Path(os.environ.get("VIGIL_HOOK_DIR", "/run/vigil"))  # root-owned tmpfs (A6)
PACKET = Path("/vigil/packet.md")
OUT = Path("/vigil/out")
AGENT_CLI = "/opt/vigil/agent-cli"  # setpriv down to the agent uid, then the CLI
SESSION_PREPARE = "/usr/local/bin/vigil-session-prepare"  # a dev image's hook, if it has one
PLAIN_CLI = "/usr/local/bin/claude"
BOT_NAME = "vigil-worker[bot]"
BOT_EMAIL = "vigil-worker[bot]@users.noreply.github.com"
SECRETISH = re.compile(r"(vgw_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})")
# Paths in .git that send git somewhere else for its config, hooks or objects.
ELSEWHERE = ("commondir", "gitdir", "objects/info/alternates", "objects/info/http-alternates")
NO_SYMLINK = ("HEAD", "config", "objects", "refs", "hooks", "info", "packed-refs")
# Reviewers and auditors read and answer; they never write (phase 2a, item 1).
READ_ONLY_ROLES = ("reviewer", "auditor")
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "Task", "WebFetch", "WebSearch"]
MAX_STRUCTURED = 256 << 10


class HarnessRefused(Exception):
    """The work tree can't be committed safely; the session fails closed."""


def redact(value: Any, token: str = "") -> Any:
    if isinstance(value, str):
        if token:
            value = value.replace(token, "[redacted]")
        return SECRETISH.sub("[redacted]", value)
    if isinstance(value, dict):
        return {k: redact(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, token) for v in value]
    return value


def agent_uid() -> int:
    return int(os.environ.get("VIGIL_AGENT_UID", "10001"))


def as_agent() -> dict[str, Any]:
    """subprocess kwargs that drop to the agent uid (outside a container,
    where the harness isn't root, there is nothing to drop)."""
    if os.getuid() != 0:
        return {}
    uid = agent_uid()
    return {"user": uid, "group": uid, "extra_groups": []}


def kill_strays() -> None:
    """As PID 1, signal every other process in the container."""
    if os.getpid() == 1:
        with contextlib.suppress(ProcessLookupError):
            os.kill(-1, signal.SIGKILL)
        time.sleep(0.2)


def emit(kind: str, payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({kind: payload}) + "\n")
    sys.stdout.flush()


def emit_event(token: str, event: dict[str, Any]) -> None:
    emit("vigil_event", redact(event, token))


# --- git, pinned against the repo's own config --------------------------------------------


def git_pins() -> list[str]:
    """Command-line config beats the repo's: no hooks, no fsmonitor, no
    external programs, no network, a fixed identity."""
    pins = {
        "core.hooksPath": "/dev/null",
        "core.fsmonitor": "false",
        "core.untrackedCache": "false",
        "core.sshCommand": "false",
        "credential.helper": "",
        "diff.external": "",
        "commit.gpgSign": "false",
        "protocol.allow": "never",
        "gc.auto": "0",
        "maintenance.auto": "false",
        "safe.directory": str(WORK),
        "user.name": BOT_NAME,
        "user.email": BOT_EMAIL,
    }
    out: list[str] = []
    for k, v in pins.items():
        out += ["-c", f"{k}={v}"]
    return out


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    gd = str(WORK / ".git")
    env.update(
        {
            "GIT_DIR": gd,
            "GIT_COMMON_DIR": gd,  # a planted .git/commondir is ignored as well as refused
            "GIT_WORK_TREE": str(WORK),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",
        }
    )
    return env


def git(*args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    p = subprocess.run(
        ["git", *git_pins(), *args],
        cwd=WORK,
        env={**git_env(), **(env or {})},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        **as_agent(),
    )
    if check and p.returncode != 0:
        raise HarnessRefused(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def check_git_dir() -> None:
    """Refuse a .git that sends git somewhere the agent chose."""
    g = WORK / ".git"
    try:
        if not stat.S_ISDIR(os.lstat(g).st_mode):
            raise HarnessRefused(".git is not a directory")
    except FileNotFoundError as e:
        raise HarnessRefused(".git is missing") from e
    for rel in ELSEWHERE:
        if os.path.lexists(g / rel):
            raise HarnessRefused(f".git/{rel} is present")
    for rel in NO_SYMLINK:
        if (g / rel).is_symlink():
            raise HarnessRefused(f".git/{rel} is a symlink")


def _match_segs(p: list[str], g: list[str]) -> bool:
    if not g:
        return not p
    if g[0] == "**":
        return any(_match_segs(p[i:], g[1:]) for i in range(len(p) + 1))
    return bool(p) and fnmatch.fnmatchcase(p[0], g[0]) and _match_segs(p[1:], g[1:])


def in_scope(path: str, scope: list[str]) -> bool:
    """vigild's strict scope globs (vigild/intake/paths.py), which the
    harness can't import."""
    return any(path == g or _match_segs(path.split("/"), g.split("/")) for g in scope)


def stage(task: dict[str, Any], env: dict[str, str] | None = None) -> list[str]:
    """Stage the work (in `env`'s index, or the clone's): tracked changes, and
    new files in the task's scope or under `.vigil/`. Returns what's left out."""
    scope = [str(x) for x in (task.get("spec") or {}).get("files_in_scope") or []]
    git("add", "-u", env=env)  # tracked files: changes and deletions
    untracked = git("ls-files", "-z", "--others", "--exclude-standard", env=env).split("\0")
    keep = [
        p
        for p in untracked
        if p
        and (p.startswith(".vigil/") or in_scope(p, scope))
        and "__pycache__" not in p.split("/")  # bytecode is never the work
        and not p.endswith((".pyc", ".pyo"))
    ]
    if keep:
        git("add", "--", *keep, env=env)
    return sorted(p for p in untracked if p and p not in keep)


CHECKPOINT_REF = "refs/vigil/checkpoint"
CHECKPOINT_SECONDS = float(os.environ.get("VIGIL_CHECKPOINT_SECONDS", "30"))


def checkpoint(task: dict[str, Any], turns: int) -> str:
    """The work so far, as a commit off the branch (phase 2a, item 3), bundled
    for vigild as /vigil/out/checkpoint.bundle. A throwaway index: the
    branch, HEAD and the agent's own index are untouched. vigild imports it at
    once and binds it to the commit named on the checkpoint line, so a
    crashed or killed worker resumes from its last commit."""
    branch, base_sha = task.get("branch"), task.get("base_sha")
    if not branch or not base_sha:
        raise HarnessRefused("task.json names no branch or base_sha")
    check_git_dir()
    head = git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    # the agent uid's git makes the index in the sticky /tmp, and removes it
    index = Path(tempfile.gettempdir()) / f"vigil-ckpt-{os.urandom(8).hex()}.index"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git("read-tree", head, env=env)
        stage(task, env)
        tree = git("write-tree", env=env)
    finally:
        subprocess.run(["rm", "-f", str(index)], check=False, timeout=30, **as_agent())
    what = task.get("task_id") or task.get("job_id")
    commit = git("commit-tree", tree, "-p", head, "-m", f"vigil: checkpoint {what} turn {turns}")
    git("update-ref", CHECKPOINT_REF, commit)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(OUT / "checkpoint.bundle")
    git("bundle", "create", "-q", str(OUT / "checkpoint.bundle"), CHECKPOINT_REF, f"^{base_sha}")
    return commit


def snapshot(task: dict[str, Any], role: str) -> tuple[str, list[str]]:
    """Commit what the agent left and bundle the branch from the base. Returns
    the head commit, which vigild binds the imported bundle to, and the
    untracked files left out of the commit.

    Only tracked files, and new files inside the task's scope or under
    `.vigil/`, are committed (review round 4, item 7): build output, caches
    and logs a session leaves behind are not the work, and committing them
    would fail the scope check."""
    branch, base_sha = task.get("branch"), task.get("base_sha")
    if not branch or not base_sha:
        raise HarnessRefused("task.json names no branch or base_sha")
    check_git_dir()
    ref = f"refs/heads/{branch}"
    if git("symbolic-ref", "-q", "HEAD", check=False) != ref:
        raise HarnessRefused(f"HEAD is not on {branch}")
    left_out = stage(task)
    if git("diff", "--cached", "--name-only"):
        what = task.get("task_id") or task.get("job_id")
        git("commit", "-q", "--no-verify", "-m", f"vigil: {role} {what}")
    head = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(OUT / "result.bundle")
    if head != base_sha:
        git("bundle", "create", "-q", str(OUT / "result.bundle"), ref, f"^{base_sha}")
    return head, left_out


def prepare_session() -> None:
    """In a repo's session image, the dev image's hook puts its baked
    dependencies next to the clone (and starts what its suites need), as the
    agent uid, so the worker's toolchain works offline (round 3)."""
    if not os.access(SESSION_PREPARE, os.X_OK):
        return
    try:
        p = subprocess.run(
            [SESSION_PREPARE],
            cwd=WORK,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            **as_agent(),
        )
    except subprocess.TimeoutExpired as e:
        raise HarnessRefused("the session-prepare hook took over 15 minutes") from e
    if p.returncode != 0:
        raise HarnessRefused(f"the session-prepare hook failed: {(p.stdout + p.stderr)[-500:]}")


RESUME = Path("/vigil/resume.jsonl")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TranscriptStore:
    """The SDK's session store (phase 2a, items 2-3), for mirroring only:
    every transcript line the CLI writes goes out on PID 1's stdout,
    redacted, so vigild keeps each session's transcript as it happens, even
    when the container is killed. It never loads: a store-backed resume would
    write the transcript into a root-only temp dir the CLI (the agent uid)
    can't read, so `place_resume` puts it where the CLI looks instead."""

    def __init__(self, token: str) -> None:
        self.token = token

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        emit(
            "vigil_transcript",
            redact(
                {
                    "session_id": key["session_id"],
                    "subpath": key.get("subpath"),
                    "entries": entries,
                },
                self.token,
            ),
        )

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        return None


def project_dir() -> Path:
    """Where the CLI keeps /work's transcripts: its config dir's projects/,
    named the way the CLI and the SDK name a directory (non-alphanumerics to
    hyphens)."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(WORK))


def place_resume() -> str | None:
    """The transcript vigild mounted, copied (as the agent uid) to where the
    CLI resumes from; the session id to resume, or None."""
    sid = os.environ.get("VIGIL_RESUME_SESSION") or ""
    if not sid:
        return None
    if not UUID.match(sid):
        raise HarnessRefused("the session to resume isn't a session id")
    dest = project_dir()
    p = subprocess.run(
        ["sh", "-c", 'mkdir -p "$1" && cp "$2" "$1/$3.jsonl"', "sh", str(dest), str(RESUME), sid],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        **as_agent(),
    )
    if p.returncode != 0:
        raise HarnessRefused(f"the transcript to resume can't be placed: {p.stderr[-300:]}")
    return sid


def _write_read_only(path: Path, doc: dict[str, Any]) -> None:
    """Replace a file the agent uid can read and nobody but root can write."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.chmod(0o444)
    os.replace(tmp, path)


def write_hook_inputs(
    hook_dir: Path, task: dict[str, Any], *, max_turns: int, hold_first_stop: bool = False
) -> None:
    """The Stop hook's inputs, from the task vigild wrote before the agent
    ran, into the root-owned hook dir (review A6): the hook never reads what
    the agent can rewrite in /work. `hold_first_stop` comes from the
    container's environment, set only by a test (2a round 9)."""
    doc: dict[str, Any] = {
        "job_id": task.get("job_id"), "task_id": task.get("task_id"),
        "test_cmd": str((task.get("spec") or {}).get("test_cmd") or ""),
        "max_turns": max_turns,
    }  # fmt: skip
    if hold_first_stop:
        doc["hold_first_stop"] = True
    _write_read_only(hook_dir / "task.json", doc)


def write_stop_blocks(hook_dir: Path, blocks: int) -> None:
    """The harness keeps the count of blocked stops (review A6, B12)."""
    _write_read_only(hook_dir / "stop.json", {"blocks": blocks})


def stop_hook_event(name: str, subtype: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """A Stop hook's answer as a vigil event (item 6): whether it sent the
    model back, its reason, and whether it let the stop through exhausted."""
    if name != "Stop" or subtype != "hook_response":
        return None
    out = str(data.get("output") or data.get("stdout") or "")
    blocked = '"decision": "block"' in out or '"decision":"block"' in out
    return {
        "type": "stop_hook",
        "data": {
            "blocked": blocked,
            "exhausted": "stop_hook_exhausted" in out,
            "outcome": data.get("outcome"),
            "output": out[-3000:],
        },
    }


def agent_options(role: str, token: str, stderr: Any) -> dict[str, Any]:
    """The SDK's options for this session, as keyword arguments (pure, so
    tested without the SDK)."""
    child_env = {
        "ANTHROPIC_BASE_URL": os.environ["VIGIL_GATEWAY_URL"],
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-5-5",
    }
    if os.environ.get("VIGIL_MAX_OUTPUT_TOKENS", "").isdigit():
        # each call's output cap: the gateway holds a call's worst case against the reservation
        child_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.environ["VIGIL_MAX_OUTPUT_TOKENS"]
    opts: dict[str, Any] = {
        "cwd": str(WORK),
        # as root, the CLI goes through setpriv to the agent uid with no capabilities
        "cli_path": AGENT_CLI if os.getuid() == 0 else PLAIN_CLI,
        "permission_mode": "bypassPermissions",
        "model": os.environ.get("VIGIL_MODEL"),
        "max_turns": int(os.environ.get("VIGIL_MAX_TURNS", "60")),
        "max_budget_usd": float(os.environ.get("VIGIL_MAX_BUDGET_USD", "1.0")),
        "setting_sources": [],  # isolation: managed settings only; the packet carries CLAUDE.md
        "env": child_env,
        "stderr": stderr,  # piped to the harness, never the container's streams
    }
    opts["include_hook_events"] = True  # the Stop hook's answers, in the stream (item 6)
    resume_id = os.environ.get("VIGIL_RESUME_SESSION") or None
    opts["session_store"] = TranscriptStore(token)
    opts["session_store_flush"] = "eager"  # a killed session keeps what it said
    if resume_id:
        opts["resume"] = resume_id  # placed by place_resume() before the agent starts
        opts["fork_session"] = os.environ.get("VIGIL_RESUME_FORK", "1") == "1"
    if role in READ_ONLY_ROLES:
        opts["tools"] = READ_ONLY_TOOLS
        opts["disallowed_tools"] = WRITE_TOOLS
    schema_path = os.environ.get("VIGIL_OUTPUT_SCHEMA")
    if schema_path:
        opts["output_format"] = {
            "type": "json_schema",
            "schema": json.loads(Path(schema_path).read_text()),
        }
    return opts


async def run_agent(packet: str, role: str, token: str) -> dict[str, Any]:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    required = os.environ.get("VIGIL_REQUIRE_PLUGIN", "guard")
    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
    options = ClaudeAgentOptions(**agent_options(role, token, stderr_tail.append))
    result: dict[str, Any] = {"plugins": [], "denials": [], "tool_calls": 0}
    turns = 0
    last_checkpoint = time.monotonic()
    async for msg in query(prompt=packet, options=options):
        hook_name = getattr(msg, "hook_event_name", "")
        if isinstance(msg, SystemMessage) and hook_name:
            ev = stop_hook_event(hook_name, msg.subtype, msg.data or {})
            if ev is not None:
                emit_event(token, ev)
                result["stop_hook_blocks"] = result.get("stop_hook_blocks", 0) + int(
                    ev["data"]["blocked"]
                )
                if ev["data"]["blocked"] and role == "worker" and HOOK_DIR.is_dir():
                    write_stop_blocks(HOOK_DIR, result["stop_hook_blocks"])
                if ev["data"]["exhausted"]:
                    result["stop_hook_exhausted"] = True
            continue
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            plugins = [
                p.get("name") if isinstance(p, dict) else str(p)
                for p in msg.data.get("plugins", [])
            ]
            result["plugins"] = plugins
            result["session_id"] = msg.data.get("session_id")
            emit(
                "vigil_event",
                {
                    "type": "state",
                    "data": {"init": True, "plugins": plugins, "model": msg.data.get("model")},
                },
            )
            if required and not any(required in (p or "") for p in plugins):
                emit(
                    "vigil_result",
                    {"status": "failed", "error": f"{required} not loaded", "plugins": plugins},
                )
                sys.exit(3)
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    result["tool_calls"] += 1
                    emit_event(
                        token,
                        {
                            "type": "tool_call",
                            "data": {"id": b.id, "name": b.name, "input": b.input},
                        },
                    )
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            ran_tools = False
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    ran_tools = True
                    text = b.content if isinstance(b.content, str) else json.dumps(b.content)
                    emit_event(
                        token,
                        {
                            "type": "tool_result",
                            "data": {
                                "id": b.tool_use_id,
                                "is_error": b.is_error,
                                "content": (text or "")[:2000],
                            },
                        },
                    )
            if ran_tools:
                turns += 1
                if role == "worker" and time.monotonic() - last_checkpoint >= CHECKPOINT_SECONDS:
                    last_checkpoint = time.monotonic()
                    try:
                        task = json.loads((WORK / ".vigil" / "task.json").read_text())
                        commit = checkpoint(task, turns)
                        emit("vigil_checkpoint", {"head": commit, "turns": turns})
                    except (HarnessRefused, OSError, ValueError) as e:
                        emit_event(token, {"type": "state", "data": {"checkpoint": str(e)[:300]}})
        elif isinstance(msg, ResultMessage):
            result.update(
                {
                    "subtype": msg.subtype,
                    "is_error": msg.is_error,
                    "num_turns": msg.num_turns,
                    "session_id": msg.session_id,
                    "sdk_cost_estimate_usd": msg.total_cost_usd,
                    "permission_denials": msg.permission_denials or [],
                    "result_text": (msg.result or "")[:4000],
                }
            )
            if msg.structured_output is not None:
                text = json.dumps(msg.structured_output)
                if len(text) <= MAX_STRUCTURED:
                    result["structured_output"] = msg.structured_output
    result["cli_stderr_tail"] = "".join(stderr_tail)[-2000:]
    return result


def main() -> int:
    line = sys.stdin.readline()
    try:
        secrets = json.loads(line)
        token = secrets["gateway_token"]
    except json.JSONDecodeError, KeyError:
        emit("vigil_result", {"status": "failed", "error": "no gateway token on stdin"})
        return 2
    role = os.environ.get("VIGIL_ROLE", "worker")
    packet = PACKET.read_text()
    task = json.loads((WORK / ".vigil" / "task.json").read_text())

    try:
        prepare_session()
        place_resume()
        if role == "worker" and HOOK_DIR.is_dir():
            write_hook_inputs(
                HOOK_DIR, task, max_turns=int(os.environ.get("VIGIL_MAX_TURNS", "60")),
                hold_first_stop=os.environ.get("VIGIL_HOLD_FIRST_STOP") == "1",
            )  # fmt: skip
            write_stop_blocks(HOOK_DIR, 0)
    except HarnessRefused as e:
        emit("vigil_result", {"status": "failed", "error": f"harness: {e}"})
        return 6
    started = time.monotonic()
    try:
        agent = asyncio.run(run_agent(packet, role, token))
    except SystemExit:
        raise
    except Exception as e:  # report, don't hide
        emit("vigil_result", {"status": "failed", "error": f"{type(e).__name__}: {e}"[:500]})
        return 4

    kill_strays()  # nothing the agent started survives to touch what follows
    if role in READ_ONLY_ROLES:
        # a review's answer is its structured output; nothing to commit or bundle
        emit(
            "vigil_result",
            redact(
                {
                    **agent,
                    "status": "done",
                    "role": role,
                    "seconds": round(time.monotonic() - started, 1),
                },
                token,
            ),
        )
        return 0
    try:
        head, left_out = snapshot(task, role)
    except HarnessRefused as e:
        kill_strays()
        emit("vigil_result", redact({"status": "failed", "error": f"harness: {e}"}, token))
        return 5
    kill_strays()
    emit(
        "vigil_result",
        redact(
            {
                **agent,
                "status": "done",
                "role": role,
                "head": head,
                "not_committed": left_out[:200],
                "seconds": round(time.monotonic() - started, 1),
            },
            token,
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
