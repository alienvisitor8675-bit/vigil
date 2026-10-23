"""Stage artifacts (documentation layer 6): files under `.vigil/<job>/`.

Job-level artifacts sit in `.vigil/<job>/`; per-worker ones (report, review,
audit) in `.vigil/<job>/<task_id>/`, so two workers' branches never
conflict on the same path. Every artifact carries provenance: JSON ones as
a `provenance` key, Markdown ones as a first line
`<!-- provenance: claude -->`. A missing or invalid artifact means the
stage is not done.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vigild.contract.models import CriterionVerify, WorkerContract, WorkerReport
from vigild.provenance import Provenance, ProvenanceError, require

JOB_ARTIFACTS = {
    "intake.json",
    "spec.md",
    "plan.md",
    "spec_gate.json",
    "approval.json",
    "verify.json",
    "close.md",
}
TASK_ARTIFACTS = {
    "worker_report.json",
    "review.json",
    "audit.json",
    "tests.json",
    "ask_coord.json",
}
NAMES = JOB_ARTIFACTS | TASK_ARTIFACTS
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MD_PROV = re.compile(r"^<!-- provenance: (\w+) -->\n?")


class ArtifactError(ValueError):
    pass


def artifact_path(root: Path, job_id: str, name: str, task_id: str | None = None) -> Path:
    if name not in NAMES:
        raise ArtifactError(f"unknown artifact {name!r}")
    for ident in (job_id, task_id):
        if ident is not None and not ID.match(ident):
            raise ArtifactError(f"bad id {ident!r}")
    base = root / ".vigil" / job_id
    return base / task_id / name if task_id else base / name


def render(name: str, content: str | dict[str, Any], provenance: Provenance) -> str:
    if name.endswith(".json"):
        data = json.loads(content) if isinstance(content, str) else dict(content)
        data["provenance"] = provenance.value
        return json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert isinstance(content, str)
    return f"<!-- provenance: {provenance.value} -->\n" + MD_PROV.sub("", content)


def write_artifact(
    root: Path,
    job_id: str,
    name: str,
    content: str | dict[str, Any],
    provenance: Provenance,
    task_id: str | None = None,
) -> Path:
    path = artifact_path(root, job_id, name, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(name, content, provenance))
    return path


def split_provenance(name: str, raw: str) -> tuple[str, Provenance]:
    try:
        if name.endswith(".json"):
            return raw, require(json.loads(raw).get("provenance"))
        m = MD_PROV.match(raw)
        return raw[m.end() :] if m else raw, require(m.group(1) if m else None)
    except (ProvenanceError, json.JSONDecodeError, AttributeError) as e:
        raise ArtifactError(f"{name}: missing or invalid provenance ({e})") from e


def read_artifact(
    root: Path, job_id: str, name: str, task_id: str | None = None
) -> tuple[str, Provenance]:
    path = artifact_path(root, job_id, name, task_id)
    if not path.is_file():
        raise ArtifactError(f"{name} is missing at {path}")
    return split_provenance(name, path.read_text())


def read_worker_report(root: Path, job_id: str, task_id: str) -> WorkerReport:
    text, _ = read_artifact(root, job_id, "worker_report.json", task_id)
    try:
        return WorkerReport.model_validate_json(text)
    except ValidationError as e:
        raise ArtifactError(f"worker_report.json for {task_id} is invalid: {e}") from e


# --- spec.md and plan.md ------------------------------------------------------

SPEC_SECTIONS = (
    "Goal",
    "Non-goals",
    "Acceptance criteria",
    "Files in scope",
    "Files out of scope",
    "Money paths",
    "Risks",
    "Rollback",
)
TEST = re.compile(r"test:\s*`([^`]+)`")
TICK = re.compile(r"`([^`]+)`")


@dataclass
class Criterion:
    ident: str
    statement: str
    test_cmd: str | None


@dataclass
class SpecDoc:
    sections: dict[str, str]
    criteria: list[Criterion] = field(default_factory=list)
    files_in_scope: list[str] = field(default_factory=list)
    files_out_of_scope: list[str] = field(default_factory=list)
    money_paths: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)


@dataclass
class PlanDoc:
    text: str
    workers: list[WorkerContract] = field(default_factory=list)
    verify: list[CriterionVerify] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current: str | None = None
    for line in MD_PROV.sub("", text).splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return out


def _items(body: str) -> list[str]:
    return [ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")]


def parse_spec(text: str) -> SpecDoc:
    secs = _sections(text)
    doc = SpecDoc(sections=secs)
    for i, item in enumerate(_items(secs.get("Acceptance criteria", "")), start=1):
        m = re.match(r"^(AC\d+)\s*:\s*", item)
        ident = m.group(1) if m else f"AC{i}"
        t = TEST.search(item)
        statement = TEST.sub("", item[m.end() :] if m else item).strip()
        doc.criteria.append(Criterion(ident, statement, t.group(1).strip() if t else None))
    for key, attr in (
        ("Files in scope", "files_in_scope"),
        ("Files out of scope", "files_out_of_scope"),
        ("Money paths", "money_paths"),
    ):
        setattr(doc, attr, [t for item in _items(secs.get(key, "")) for t in TICK.findall(item)])
    doc.interfaces = _items(secs.get("Interfaces", ""))
    return doc


def parse_plan(text: str) -> PlanDoc:
    doc = PlanDoc(text=text)
    blocks = re.findall(r"```json\s*\n(.*?)```", text, flags=re.S)
    block = next((b for b in blocks if '"workers"' in b), None)
    if block is None:
        doc.errors.append("plan.md has no ```json block with a workers list")
        return doc
    try:
        raw = json.loads(block)
    except json.JSONDecodeError as e:
        doc.errors.append(f"plan.md workers block is not JSON: {e}")
        return doc
    workers = raw.get("workers") if isinstance(raw, dict) else None
    if not isinstance(workers, list):
        doc.errors.append("plan.md workers must be a list")
        return doc
    for i, w in enumerate(workers):
        tid = w.get("task_id", f"#{i + 1}") if isinstance(w, dict) else f"#{i + 1}"
        try:
            doc.workers.append(WorkerContract.model_validate(w))
        except ValidationError as e:
            msgs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            doc.errors.append(f"worker {tid} does not validate: {msgs}")
    raw_verify = raw.get("verify")
    if not isinstance(raw_verify, list) or not raw_verify:
        doc.errors.append("plan.md must list `verify`: each criterion's level and, at 0, why")
    else:
        for i, v in enumerate(raw_verify):
            try:
                doc.verify.append(CriterionVerify.model_validate(v))
            except ValidationError as e:
                msgs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
                doc.errors.append(f"verify entry {i + 1} does not validate: {msgs}")
    return doc


def canonicalize_plan(job_id: str, text: str) -> str:
    """Give plan.md's workers vigild's own ids, `{job}-w1`, `{job}-w2`, … in
    order, remapping depends_on. A coord can't name a task id that collides
    with another job's task, clone or branch (review round 1, item 16)."""
    blocks = list(re.finditer(r"```json\s*\n(.*?)```", text, flags=re.S))
    for m in blocks:
        if '"workers"' not in m.group(1):
            continue
        raw = json.loads(m.group(1))
        mapping = {w["task_id"]: f"{job_id}-w{i}" for i, w in enumerate(raw["workers"], start=1)}
        for w in raw["workers"]:
            w["task_id"] = mapping[w["task_id"]]
            w["depends_on"] = [mapping.get(d, d) for d in w.get("depends_on", [])]
        block = "```json\n" + json.dumps(raw, indent=1) + "\n```"
        return text[: m.start()] + block + text[m.end() :]
    return text
