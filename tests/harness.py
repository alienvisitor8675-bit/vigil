"""Stand-ins for the container pieces, for tests that run without Docker.

- `finish_session` does what the executor harness does at the end of a
  session: commit leftovers with git pinned against the clone's own .git,
  write the result bundle into the session's out dir, and return what the
  harness puts on its result line (the head commit; review round 2, N1).
- `LocalTestRunner` stands in for the verifier's container."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vigild.verifier import REPORT_DIR, RunResult

PINS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "user.name=vigil-worker[bot]", "-c", "user.email=w@vigil.invalid")  # fmt: skip


def _pinned(clone: Path, *args: str) -> str:
    gd = str(clone / ".git")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_DIR": gd, "GIT_COMMON_DIR": gd, "GIT_WORK_TREE": str(clone),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})  # fmt: skip
    p = subprocess.run(
        ["git", *PINS, *args], cwd=clone, env=env, capture_output=True, text=True, check=True
    )
    return p.stdout.strip()


def finish_session(
    clone: Path,
    out_dir: Path,
    *,
    branch: str,
    base_sha: str,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """With `scope`, commit as the harness does (round 4, item 7): tracked
    files, and new files in scope or under .vigil/. Without, everything (for
    tests that build a history by hand)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    left_out: list[str] = []
    if scope is None:
        _pinned(clone, "add", "-A")
    else:
        from vigild.intake.paths import in_scope

        _pinned(clone, "add", "-u")
        untracked = _pinned(clone, "ls-files", "--others", "--exclude-standard").split("\n")
        keep = [
            p
            for p in untracked
            if p
            and (p.startswith(".vigil/") or in_scope(p, scope))
            and "__pycache__" not in p.split("/")
            and not p.endswith((".pyc", ".pyo"))
        ]
        left_out = sorted(p for p in untracked if p and p not in keep)
        if keep:
            _pinned(clone, "add", "--", *keep)
    if _pinned(clone, "diff", "--cached", "--name-only"):
        _pinned(clone, "commit", "-q", "--no-verify", "-m", "harness: leftovers")
    bundle = out_dir / "result.bundle"
    if bundle.is_symlink() or bundle.exists():
        bundle.unlink()
    _pinned(clone, "bundle", "create", "-q", str(bundle), f"refs/heads/{branch}", f"^{base_sha}")
    return {
        "status": "done",
        "plugins": ["guard"],
        "head": _pinned(clone, "rev-parse", f"refs/heads/{branch}"),
        "not_committed": left_out,
    }


@dataclass
class LocalTestRunner:
    """Stands in for the verifier's container: runs the command in the checkout
    vigild made, and records what that checkout looked like."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(
        self,
        image: str,
        source: Path,
        command: str,
        *,
        timeout_s: int,
        max_output: int,
        env: Mapping[str, str] | None = None,
        subject: Path | None = None,
    ) -> RunResult:
        hooks = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"], cwd=source, capture_output=True, text=True
        ).stdout.strip()
        planted = [p for p in (".git/commondir", ".vigil/task.json") if (source / p).exists()]
        self.calls.append(
            {
                "source": str(source),
                "command": command,
                "hooksPath": hooks,
                "worker_files": planted,
                "image": image,
                "subject": str(subject) if subject else "",
            }
        )
        # as in the container: bash with pipefail and errexit, pytest asked for
        # a JUnit report in a directory outside the checkout
        reports = Path(tempfile.mkdtemp(prefix="vigil-report-"))
        env = {
            **os.environ,
            "PYTEST_ADDOPTS": f"--junitxml={reports}/pytest.xml -p no:cacheprovider",
            **({"VIGIL_SUBJECT": str(subject)} if subject else {}),
            **(env or {}),
        }
        try:
            p = subprocess.run(
                [
                    "bash",
                    "-o",
                    "pipefail",
                    "-o",
                    "errexit",
                    "-c",
                    command.replace(REPORT_DIR, str(reports)),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return RunResult(None, "", timed_out=True)
        from vigild.executors.runtime import REPORT_NAME

        found = {f.name: f.read_bytes() for f in reports.glob("*.xml") if REPORT_NAME.match(f.name)}
        shutil.rmtree(reports, ignore_errors=True)
        return RunResult(p.returncode, (p.stdout + p.stderr)[-max_output:], reports=found)
