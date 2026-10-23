"""The decider as vigild builds it (review round 2, N9): Haiku through the
gateway's in-process `call`, and each Jev route through `Gateway.metered`, so
every decider call is route-checked, reserved and booked. A Jev route without
its credentials is left out, and Haiku answers alone.

Its first caller is the phase 2a fleet: label intake asks it for an issue's
value and risk when no label gives them (vigild/intake/github.py). vigild
builds it here and nowhere else.
"""

from __future__ import annotations

import httpx2

from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.decider.backends.haiku import HaikuBackend
from vigild.decider.backends.jev import JevCloudflare, JevOpenRouter
from vigild.decider.policy import Backend, Decider
from vigild.gateway.service import Gateway
from vigild.telemetry.spans import Telemetry


def available_routes(secrets: Secrets) -> set[str]:
    """The decider routes vigild builds with these credentials."""
    routes = {"haiku"}
    if secrets.get("CLOUDFLARE_ACCOUNT_ID") and secrets.get("CLOUDFLARE_API_TOKEN"):
        routes.add("jev-cloudflare")
    if secrets.get("OPENROUTER_API_KEY"):
        routes.add("jev-openrouter")
    return routes


def build_decider(
    cfg: Config,
    store: Store,
    gateway: Gateway,
    secrets: Secrets,
    client: httpx2.Client,
    telemetry: Telemetry | None = None,
) -> Decider:
    backends: dict[str, Backend] = {"haiku": HaikuBackend(gateway)}
    routes = available_routes(secrets)
    account, cf_token = secrets.get("CLOUDFLARE_ACCOUNT_ID"), secrets.get("CLOUDFLARE_API_TOKEN")
    if "jev-cloudflare" in routes and account and cf_token:
        backends["jev-cloudflare"] = JevCloudflare(
            client, account_id=account, token=cf_token, meter=gateway.metered,
            version_pin=cfg.backends.decider_routes["jev-cloudflare"].version_pin,
        )  # fmt: skip
    if "jev-openrouter" in routes and (or_key := secrets.get("OPENROUTER_API_KEY")):
        backends["jev-openrouter"] = JevOpenRouter(
            client, token=or_key, meter=gateway.metered,
            version_pin=cfg.backends.decider_routes["jev-openrouter"].version_pin,
        )  # fmt: skip
    return Decider(cfg, store, backends, breakers=gateway.breakers, telemetry=telemetry)
