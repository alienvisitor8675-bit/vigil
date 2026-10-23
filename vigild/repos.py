"""The private-repo rule, on each repo's live GitHub visibility.

Private repos never go to a hosted API, with one exception: Jev over
Cloudflare zero-retention. The owner granted one more on 2026-10-22 (Q2):
`anthropic` for thomasbardhi01/vigil only. It lives here, in code, so a YAML
edit can't extend it to another repo.

**Visibility is GitHub's live answer, never a hand-set field** (owner,
2026-10-23). `live_private` asks the App (a metadata-only token) every time
it's called; a lookup that fails, or answers anything but a bool, counts as
private: Cloudflare or no Jev, never OpenRouter. There is no cache.

- A repo's `decider:` list names the logical `jev`; `jev_route` resolves it
  per call: public -> jev-openrouter, private -> jev-cloudflare.
- `repo_rule_errors` checks a repo's backends and decider entries against
  the rule for a given visibility. vigild checks it at start (and refuses to
  start), and again at every dispatch (the runner refuses the dispatch and
  pages the owner).
- `check_backend` and `check_decider_route` run on submit, at a session's
  token, on every gateway call (with the visibility read when the session's
  token was issued) and right before every Jev call (read again then).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vigild.config import Config

PRIVATE_HOSTED_ROUTES = frozenset({"jev-cloudflare"})
PRIVATE_HOSTED_PROVIDER_EXCEPTIONS: dict[str, frozenset[str]] = {
    "thomasbardhi01/vigil": frozenset({"anthropic"}),
}
JEV = "jev"  # the logical decider entry
JEV_PUBLIC, JEV_PRIVATE = "jev-openrouter", "jev-cloudflare"

log = logging.getLogger("vigild.repos")
IsPrivate = Callable[[str], bool]
_source: IsPrivate | None = None


class RepoPolicyError(ValueError):
    pass


def set_visibility_source(source: IsPrivate | None) -> None:
    """Where `live_private` asks: the GitHub App's `is_private` in vigild."""
    global _source
    _source = source


def live_private(repo: str, source: IsPrivate | None = None) -> bool:
    """The repo's visibility, asked now. Fails closed: no source, an error, or
    an answer that isn't a bool all mean private."""
    ask = source or _source
    if ask is None:
        return True
    try:
        answer = ask(repo)
    except Exception as e:
        log.warning("visibility of %s unreadable (%s): treated as private", repo, type(e).__name__)
        return True
    return answer if isinstance(answer, bool) else True


def jev_route(private: bool) -> str:
    return JEV_PRIVATE if private else JEV_PUBLIC


def resolve_decider(cfg: Config, repo: str, private: bool) -> list[str]:
    """The repo's decider chain as routes, `jev` resolved by visibility."""
    return [jev_route(private) if e == JEV else e for e in cfg.repo(repo).decider]


def _hosted_allowed(repo: str, provider: str) -> bool:
    return provider in PRIVATE_HOSTED_PROVIDER_EXCEPTIONS.get(repo, frozenset())


def validate_repo_policies(cfg: Config) -> None:
    """At load: every entry names something real; decider lists use `jev`."""
    presets = cfg.backends.presets
    routes = cfg.backends.decider_routes
    for repo, pol in cfg.repos.repos.items():
        for name in pol.backends + pol.worker_overflow:
            if name not in presets:
                raise RepoPolicyError(f"{repo}: unknown backend {name}")
        for name in pol.decider:
            if name in (JEV_PUBLIC, JEV_PRIVATE):
                raise RepoPolicyError(
                    f"{repo}: list `jev`, not {name}: vigild picks the route by live visibility"
                )
            if name != JEV and name not in routes:
                raise RepoPolicyError(f"{repo}: unknown decider route {name}")


def repo_rule_errors(cfg: Config, repo: str, private: bool) -> list[str]:
    """What breaks the private-repo rule for this repo at this visibility."""
    if not private:
        return []
    errors: list[str] = []
    pol = cfg.repo(repo)
    presets = cfg.backends.presets
    for name in pol.backends + pol.worker_overflow:
        p = presets[name]
        if p.locality == "hosted" and not _hosted_allowed(repo, p.provider):
            errors.append(f"{repo} is private; hosted backend {name} ({p.provider}) is not allowed")
    for name in resolve_decider(cfg, repo, private):
        r = cfg.backends.decider_routes[name]
        if r.locality != "hosted" or name in PRIVATE_HOSTED_ROUTES:
            continue
        if not _hosted_allowed(repo, r.provider):
            errors.append(
                f"{repo} is private; hosted decider route {name} ({r.provider}) is not allowed"
            )
    return errors


def check_backend(cfg: Config, repo: str, preset_name: str, *, private: bool) -> None:
    """Refuse a backend the repo doesn't list, or one the rule forbids now."""
    pol = cfg.repo(repo)
    if preset_name not in pol.backends + pol.worker_overflow:
        raise RepoPolicyError(f"{repo} does not list backend {preset_name} in configs/repos.yaml")
    p = cfg.preset(preset_name)
    if private and p.locality == "hosted" and not _hosted_allowed(repo, p.provider):
        raise RepoPolicyError(f"{repo} is private; hosted backend {preset_name} is not allowed")


def check_decider_route(cfg: Config, repo: str, route: str, *, private: bool) -> None:
    """Refuse a route the repo's chain doesn't resolve to at this visibility:
    a private repo can never reach jev-openrouter."""
    if route not in resolve_decider(cfg, repo, private):
        state = "private" if private else "public"
        raise RepoPolicyError(f"{repo} ({state}) does not use decider route {route}")
    r = cfg.backends.decider_routes[route]
    hosted = r.locality == "hosted" and route not in PRIVATE_HOSTED_ROUTES
    if private and hosted and not _hosted_allowed(repo, r.provider):
        raise RepoPolicyError(f"{repo} is private; decider route {route} is not allowed")


def verify_visibility(cfg: Config, is_private: IsPrivate) -> dict[str, bool]:
    """At start: each repo's live visibility, and the rule checked against it.
    A repo that can't be read counts as private; a rule broken refuses the
    start. Returns what it read."""
    seen: dict[str, bool] = {}
    for repo in cfg.repos.repos:
        private = live_private(repo, is_private)
        errors = repo_rule_errors(cfg, repo, private)
        if errors:
            raise RepoPolicyError("; ".join(errors))
        seen[repo] = private
    return seen
