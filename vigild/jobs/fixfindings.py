"""The findings an owner can send back with "fix with findings": a task's review
and audit findings, numbered. Every id is `review:N` or `audit:N` (N is the
finding's index in review.json or audit.json), and each also has a plain index in
the combined list. The text came from model sessions, so it is only ever handed on
fenced."""

from __future__ import annotations

import json
from typing import Any

from vigild.db import Store

MAX_PICKED = 200


def selectable(store: Store, job_id: str, task_id: str) -> list[dict[str, Any]]:
    """The task's review findings, then its audit findings, as shown in the ready item."""
    out: list[dict[str, Any]] = []
    for name, source in (("review.json", "review"), ("audit.json", "audit")):
        art = store.get_artifact(job_id, name, task_id)
        if art is None:
            continue
        try:
            doc = json.loads(art.content)
        except ValueError:
            continue
        for i, f in enumerate(doc.get("findings") or []):
            out.append(
                {
                    "index": len(out),
                    "id": f"{source}:{i}",
                    "source": source,
                    "severity": f.get("severity"),
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "claim": f.get("claim"),
                    "fix": f.get("fix"),
                    # an audit finding the reviewer also found is `matched`
                    "missed_by_review": source == "audit" and f.get("matched") is None,
                }
            )
    return out


def pick(available: list[dict[str, Any]], chosen: list[Any]) -> list[dict[str, Any]]:
    """The chosen findings, in the order the task lists them. A choice is an id or an
    index into `available`; anything that is not one of them raises ValueError."""
    by_id = {f["id"]: f for f in available}
    picked: dict[str, dict[str, Any]] = {}
    for c in chosen:
        if isinstance(c, bool) or not isinstance(c, int | str):
            raise ValueError(f"not a finding id or index: {c!r}")
        f = by_id.get(c) if isinstance(c, str) else None
        if isinstance(c, int) and 0 <= c < len(available):
            f = available[c]
        if f is None:
            raise ValueError(f"not one of this task's findings: {c!r}")
        picked[f["id"]] = f
    return [f for f in available if f["id"] in picked]
