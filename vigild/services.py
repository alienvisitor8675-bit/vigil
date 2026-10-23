"""What the API and the runner share: config, store, tokens, the job service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vigild.config import Config
    from vigild.db import Store
    from vigild.jobs.service import JobService


@dataclass
class Tokens:
    """B7's three bearer scopes. None means the scope is off; nothing matches it."""

    admin: str | None
    read: str | None
    heartbeat: str | None


@dataclass
class Services:
    cfg: Config
    store: Store
    tokens: Tokens
    jobs: JobService
    # Features switched off because a credential is missing, e.g. "github-app".
    degraded: set[str] = field(default_factory=set)
