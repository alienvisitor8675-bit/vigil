"""The private-repo backend rule.

Private repos never go to a hosted API, with one exception: Jev over
Cloudflare zero-retention, and only where configs/repos.yaml lists it. The
owner granted one more on 2026-10-22 (Q2): `anthropic` for
thomasbardhi01/vigil only. It lives here, in code, so a YAML edit can't
extend it to another repo.

The rule is checked three times: at config load (vigild refuses to start),
at submit and container start (`check_backend`), and on the wire by the
gateway (`check_backend` again, per call).
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


log = logging.getLogger("vigild.repos")


class RepoPolicyError(ValueError):
    pass


def _hosted_allowed(repo: str, provider: str) -> bool:
    return provider in PRIVATE_HOSTED_PROVIDER_EXCEPTIONS.get(repo, frozenset())


def validate_repo_policies(cfg: Config) -> None:
    presets = cfg.backends.presets
    routes = cfg.backends.decider_routes
    for repo, pol in cfg.repos.repos.items():
        for name in pol.backends + pol.worker_overflow:
            if name not in presets:
                raise RepoPolicyError(f"{repo}: unknown backend {name}")
        for name in pol.decider:
            if name not in routes:
                raise RepoPolicyError(f"{repo}: unknown decider route {name}")
        if pol.visibility != "private":
            continue
        for name in pol.backends + pol.worker_overflow:
            p = presets[name]
            if p.locality == "hosted" and not _hosted_allowed(repo, p.provider):
                raise RepoPolicyError(
                    f"{repo} is private; hosted backend {name} ({p.provider}) is not allowed"
                )
        for name in pol.decider:
            r = routes[name]
            if r.locality != "hosted" or name in PRIVATE_HOSTED_ROUTES:
                continue
            if not _hosted_allowed(repo, r.provider):
                raise RepoPolicyError(
                    f"{repo} is private; hosted decider route {name} ({r.provider}) is not allowed"
                )


def check_backend(cfg: Config, repo: str, preset_name: str) -> None:
    """Refuse a backend the repo's policy doesn't list (any visibility)."""
    pol = cfg.repo(repo)
    if preset_name not in pol.backends + pol.worker_overflow:
        raise RepoPolicyError(f"{repo} does not list backend {preset_name} in configs/repos.yaml")


def check_hidden_author(cfg: Config, repo: str, preset_name: str) -> None:
    """Hosted hidden-test authoring sends the spec and the repo's code to a hosted
    model. For a private repo that is ruling 15; the owner waived it for
    thomasbardhi01/vigil only (2026-10-23), here in code with the other hard-coded
    exception, so a YAML edit can't extend it. Every other private repo is
    local-only or off; with no local model yet, that is off."""
    pol = cfg.repo(repo)
    if pol.visibility != "private":
        return
    p = cfg.backends.presets[preset_name]
    if p.locality == "hosted" and not _hosted_allowed(repo, p.provider):
        raise RepoPolicyError(
            f"{repo} is private: hidden-test authoring on a hosted model ({p.provider}) is off"
        )


def check_decider_route(cfg: Config, repo: str, route: str) -> None:
    if route not in cfg.repo(repo).decider:
        raise RepoPolicyError(f"{repo} does not list decider route {route}")


def verify_visibility(cfg: Config, is_private: Callable[[str], bool]) -> None:
    """Review round 1, item 10: the YAML's `visibility` is checked against the
    repo itself (through the App) before vigild starts. One direction only:
    - YAML public, GitHub private: refuse to start (the YAML is less strict);
    - YAML private, GitHub public: warn and keep the YAML (it is stricter, and
      a repo may be public for a while and private again later);
    - the check can't be made: refuse to start."""
    for repo, pol in cfg.repos.repos.items():
        try:
            private = is_private(repo)
        except Exception as e:
            raise RepoPolicyError(f"{repo}: can't check its visibility ({type(e).__name__})") from e
        if private and pol.visibility != "private":
            raise RepoPolicyError(f"{repo} is private on GitHub but configs/repos.yaml says public")
        if not private and pol.visibility == "private":
            log.warning(
                "%s is public on GitHub; configs/repos.yaml keeps it private (stricter)", repo
            )
