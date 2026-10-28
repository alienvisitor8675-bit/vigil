"""CI `image-key` job: does this PR change an input of any dev-image key or of the
executor layer digest? The inputs come from `vigild.devimage.key_input_paths`
(the code vigild hashes with), never a second list.

  image-key-check.py --base origin/main   print changed inputs; set $GITHUB_OUTPUT
  image-key-check.py --check-body         fail unless $PR_BODY has `Rebuilds images: <what>`
  image-key-check.py --validate           build vigil's dev image, run its dev_suites offline

The executor (session) layer needs claude-mods as a build context, which CI
doesn't have, so --validate covers the dev image only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vigild import config  # noqa: E402
from vigild.config import Config  # noqa: E402
from vigild.devimage import config_input_changes, key_input_paths  # noqa: E402

THIS_REPO = "thomasbardhi01/vigil"
BODY_LINE = re.compile(r"^Rebuilds images:[ \t]*\S", re.MULTILINE)


def _in_layer(path: str) -> bool:
    return path.startswith("images/claude-executor/") and "__pycache__" not in path.split("/")


def changed_inputs(
    cfg: Config,
    repo: str,
    changed: list[str],
    base_repos: dict[str, Any],
    head_repos: dict[str, Any],
    base_vigild: dict[str, Any] | None = None,
    head_vigild: dict[str, Any] | None = None,
) -> list[str]:
    """Each changed input: a file in the key list, any path under the executor layer
    (so a deleted file counts), or a key field of repos.yaml or vigild.yaml."""
    inputs = set(key_input_paths(cfg, repo))
    out = {f for f in changed if f in inputs or _in_layer(f)}
    if "configs/vigild.yaml" in changed:
        old = (base_vigild or {}).get("github", {}).get("claude_mods_sha")
        if old != (head_vigild or {}).get("github", {}).get("claude_mods_sha"):
            out.add("configs/vigild.yaml: github.claude_mods_sha")  # SessionImages.key
    if "configs/repos.yaml" in changed:
        out |= {f"configs/repos.yaml: {c}" for c in config_input_changes(base_repos, head_repos)}
    return sorted(out)


def body_names_rebuild(body: str | None) -> bool:
    """A line starting `Rebuilds images:` that names a repo or layer."""
    return bool(body and BODY_LINE.search(body))


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def _repos_yaml(text: str) -> dict[str, Any]:
    return dict((yaml.safe_load(text) or {}).get("repos") or {})


def _vigild_yaml(text: str) -> dict[str, Any]:
    return dict(yaml.safe_load(text) or {})


AGENT = "setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all --"


def suite_run_args(tag: str, src: Path, cmd: str) -> list[str]:
    """The container command for one suite, hardened like the verifier's: no network,
    read-only root, no-new-privileges, pids and memory limits, the test as uid 10001."""
    script = (
        f"{AGENT} /usr/local/bin/vigil-prepare && cd /work && "
        f'{AGENT} bash -o pipefail -o errexit -c "$0"'
    )
    tmp = "rw,exec,uid=10001,gid=10001"
    run = ["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--read-only"]
    run += ["--security-opt", "no-new-privileges", "--pids-limit", "2048", "--memory", "6g"]
    run += ["--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID"]
    run += ["--tmpfs", f"/work:{tmp},size=4g", "--tmpfs", f"/tmp:{tmp},size=2g,mode=1777"]  # noqa: S108
    run += ["-e", "HOME=/tmp/home", "-e", "XDG_CACHE_HOME=/tmp/cache", "-e", "NO_COLOR=1"]
    run += ["-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPYCACHEPREFIX=/tmp/pycache"]
    run += ["-v", f"{src}:/src:ro", "-w", "/work", "--entrypoint", "sh", tag, "-c", script, cmd]
    return run


def validate(cfg: Config, repo: str) -> int:
    """The dev image from the lockfiles alone, then each dev_suites command in it
    with no network, as the agent uid, on a clean export of HEAD (no .git, no .venv)."""
    pol = cfg.repo(repo)
    tag = "vigil-dev/ci:check"
    with tempfile.TemporaryDirectory() as ctx:
        for lock in pol.dev_lockfiles:
            dest = Path(ctx) / lock
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / lock).read_bytes())
        build = ["docker", "build", "-f", str(ROOT / pol.dev_dockerfile), "-t", tag, ctx]
        if subprocess.run(build, check=False).returncode:
            print("dev image build failed", file=sys.stderr)
            return 1
    failed = []
    with tempfile.TemporaryDirectory() as src:
        archive = subprocess.run(
            ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", src], input=archive, check=True)
        for name, cmd in pol.dev_suites.items():
            print(f"::group::suite {name}: {cmd}", flush=True)
            rc = subprocess.run(suite_run_args(tag, Path(src), cmd), check=False).returncode
            print("::endgroup::", flush=True)
            if rc:
                failed.append(name)
    print(f"suites failed: {failed}" if failed else "all suites passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--check-body", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--repo", default=THIS_REPO)
    a = ap.parse_args(argv)
    cfg = config.load()
    if a.validate:
        return validate(cfg, a.repo)
    if a.check_body:
        if body_names_rebuild(os.environ.get("PR_BODY")):
            return 0
        print(
            "this PR rebuilds images: add a line `Rebuilds images: <repo or layer>` to its body",
            file=sys.stderr,
        )
        return 1
    if not a.base:
        ap.error("--base is required")
    changed = [f for f in _git("diff", "--name-only", f"{a.base}...HEAD").splitlines() if f]
    base_yaml = _git("show", f"{a.base}:configs/repos.yaml")
    head_yaml = (ROOT / "configs/repos.yaml").read_text()
    base_v = _vigild_yaml(_git("show", f"{a.base}:configs/vigild.yaml"))
    head_v = _vigild_yaml((ROOT / "configs/vigild.yaml").read_text())
    hits = changed_inputs(
        cfg, a.repo, changed, _repos_yaml(base_yaml), _repos_yaml(head_yaml), base_v, head_v
    )
    for h in hits:
        print(f"image-key input changed: {h}")
    print("rebuilds images" if hits else "no image-key input changed")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"changed={'true' if hits else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
