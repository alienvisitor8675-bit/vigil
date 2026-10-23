"""The one redactor (review round 1, item 13; round 2). Everything vigild stores
or publishes that a session or a model produced passes through `redact`: the
event log, the gateway's refusal records, and from phase 3 the stream's
`code` topic (docs/ui.md). The harness inside the container has its own copy
of the same pattern, since it can't import vigild."""

from __future__ import annotations

import re
from typing import Any

SECRETISH = re.compile(r"(vgw_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})")


def redact(value: Any) -> Any:
    """Drop anything token-shaped, at any depth."""
    if isinstance(value, str):
        return SECRETISH.sub("[redacted]", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
