"""The launchd templates in deploy/launchd/: they parse, run from the checkout
with uv, and hold placeholders only (no host path, no address)."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIGILD = ROOT / "deploy" / "launchd" / "dev.vigil.vigild.plist"


def _load(p: Path) -> dict[str, object]:
    data = plistlib.loads(p.read_bytes())
    assert isinstance(data, dict)
    return data


def test_vigild_plist_keeps_vigild_alive_from_login() -> None:
    d = _load(VIGILD)
    assert d["Label"] == "dev.vigil.vigild"
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is True
    assert d["WorkingDirectory"] == "__REPO_CHECKOUT__"
    assert d["ProgramArguments"] == ["__UV_PATH__", "run", "--locked", "python", "-m", "vigild"]


def test_vigild_plist_logs_under_the_data_dir() -> None:
    d = _load(VIGILD)
    assert d["StandardOutPath"] == "__HOME__/vigil-data/logs/vigild.log"
    assert d["StandardErrorPath"] == "__HOME__/vigil-data/logs/vigild.log"


def test_vigild_plist_is_throttled() -> None:
    # A refused start (the private-repo rule) exits; launchd must not hammer GitHub.
    d = _load(VIGILD)
    assert isinstance(d["ThrottleInterval"], int) and d["ThrottleInterval"] >= 30


def test_vigild_plist_sets_path_and_nothing_else() -> None:
    # launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin, without ~/.docker/bin or
    # uv's directory; the runbook fills __PATH__ from a shell that runs vigild.
    # Secrets come from ~/.config/vigil/env, read by vigild, never from here.
    d = _load(VIGILD)
    assert d["EnvironmentVariables"] == {"PATH": "__PATH__"}


def test_launchd_templates_hold_no_host_paths() -> None:
    for p in sorted((ROOT / "deploy" / "launchd").glob("*.plist")):
        text = p.read_text()
        assert "/Users/" not in text, p.name
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), p.name
