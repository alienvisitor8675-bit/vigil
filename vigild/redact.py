"""The one redactor (review round 1, item 13; round 2). Everything vigild stores
or publishes that a session or a model produced passes through `redact`: the
event log, the gateway's refusal records, and from phase 3 the stream's
`code` topic (docs/ui.md). The harness inside the container has its own copy
of the same pattern, since it can't import vigild."""

from __future__ import annotations

import re
from typing import Any

SECRETISH = re.compile(r"(vgw_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})")
# A PEM private key, through its END line; or, if the text was cut off, to the end
# of the string or the next `"` or escaped `\"` (a PEM body has neither), so a JSON string
# holding it keeps parsing. Matches real newlines and the escaped backslash-n of JSON.
PEM_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[^\"\\]|\\(?!\"))*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|(?=\\?\")|\Z)"
)


def redact(value: Any) -> Any:
    """Drop anything token-shaped, and any PEM private key, at any depth."""
    if isinstance(value, str):
        return PEM_KEY.sub("[redacted]", SECRETISH.sub("[redacted]", value))
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
