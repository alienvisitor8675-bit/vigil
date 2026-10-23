"""Build vigil's linux/arm64 images.

claude-mods reaches the executor build as a named build context: a
`git archive` of the pinned SHA from a local repository (vigild's mirror by
default), so the build needs no token and clones nothing.

Usage:
  uv run python scripts/build-images.py [--claude-mods-repo PATH] [--only executor|proxy]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vigild import config  # noqa: E402
from vigild.devimage import docker_cli  # noqa: E402

DOCKER, DOCKER_ENV = docker_cli()


def run(*cmd: str) -> None:
    """`docker …` through the CLI vigild uses, with its credential helper on PATH."""
    if cmd and cmd[0] == "docker":
        cmd = (DOCKER, *cmd[1:])
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=DOCKER_ENV)


def claude_mods_context(repo: Path, sha: str, dest: Path) -> Path:
    tar = dest / "claude-mods.tar"
    with tar.open("wb") as f:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", sha], stdout=f, check=True
        )
    out = dest / "claude-mods"
    out.mkdir()
    with tarfile.open(tar) as t:
        t.extractall(out, filter="data")
    return out


def main() -> int:
    cfg = config.load()
    default_mirror = cfg.vigild.data_path / "mirrors" / f"{cfg.vigild.github.claude_mods}.git"
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-mods-repo", type=Path, default=default_mirror)
    ap.add_argument("--only", choices=["executor", "proxy"])
    args = ap.parse_args()

    if args.only in (None, "proxy"):
        run(
            "docker",
            "build",
            "--platform",
            "linux/arm64",
            "-t",
            cfg.vigild.images.egress_proxy,
            str(ROOT / "images" / "egress-proxy"),
        )
    if args.only in (None, "executor"):
        sha = cfg.vigild.github.claude_mods_sha
        with tempfile.TemporaryDirectory() as tmp:
            ctx = claude_mods_context(args.claude_mods_repo, sha, Path(tmp))
            run(
                "docker",
                "buildx",
                "build",
                "--load",
                "--platform",
                "linux/arm64",
                "--build-context",
                f"claude_mods={ctx}",
                "--build-context",
                f"vigil_configs={ROOT / 'configs'}",
                "--build-context",
                f"vigil_schemas={ROOT / 'schemas'}",
                "-t",
                cfg.vigild.images.claude_executor,
                str(ROOT / "images" / "claude-executor"),
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
