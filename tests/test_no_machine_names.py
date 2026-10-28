"""ADR-0004: no code in vigild/ names a machine. A sandbox host is whatever
configs/sandbox_hosts.yaml says; only the default `local` may appear."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from vigild.config import CONFIG_DIR

ROOT = CONFIG_DIR.parent
ALLOWED_NAMES = {"local"}
# addresses code may name: loopback, any-address, Docker Desktop's host address, and a public resolver the egress probe tries
ALLOWED_ADDRESSES = {"127.0.0.1", "0.0.0.0", "192.168.65.254", "255.255.255.255", "1.1.1.1"}  # noqa: S104
IPV4 = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])(?!/\d)"
)  # a CIDR range names a network, not a machine
SSH_TARGET = re.compile(r"ssh://[\w.@-]+")


def machine_names(config_dir: Path) -> set[str]:
    data = yaml.safe_load((config_dir / "sandbox_hosts.yaml").read_text())
    names: set[str] = set()
    for h in data["hosts"]:
        names |= {h["name"], h["docker_context"]}
        for k in ("private_address",):
            if h.get(k):
                names.add(h[k])
    return {n.lower() for n in names} - ALLOWED_NAMES - {"default"}


def violations(root: Path, names: set[str]) -> list[str]:
    out: list[str] = []
    for f in sorted(root.rglob("*.py")):
        text = f.read_text()
        low = text.lower()
        for n in names:
            if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", low):
                out.append(f"{f.name}: names host {n!r}")
        for m in IPV4.finditer(text):
            if m.group(1) not in ALLOWED_ADDRESSES:
                out.append(f"{f.name}: literal address {m.group(1)}")
        for m in SSH_TARGET.finditer(text):
            out.append(f"{f.name}: ssh target {m.group(0)}")
    return out


def test_vigild_names_no_machine() -> None:
    assert violations(ROOT / "vigild", machine_names(CONFIG_DIR)) == []


def test_the_scan_finds_a_named_machine(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text('HOST = "the-x86-box"\nADDR = "10.9.8.7"\nU = "ssh://bob@h"\n')
    found = violations(tmp_path, {"the-x86-box"})
    assert len(found) == 3
    (tmp_path / "bad.py").write_text('A = "127.0.0.1"\nB = "local"\n')
    assert violations(tmp_path, {"the-x86-box"}) == []


def test_the_scan_reads_names_from_the_config(tmp_path: Path) -> None:
    (tmp_path / "sandbox_hosts.yaml").write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "local", "docker_context": "default"},
                    {"name": "Rack-7", "docker_context": "ctx-r7", "private_address": "10.0.0.9"},
                ]
            }
        )
    )
    assert machine_names(tmp_path) == {"rack-7", "ctx-r7", "10.0.0.9"}
