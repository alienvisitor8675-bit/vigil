"""The decider as vigild builds it (review round 2, N9): Haiku through the
gateway's in-process `call`, and each Jev route through `Gateway.metered`, so
every decider call is route-checked, reserved and booked. A Jev route without
its credentials is left out, and Haiku answers alone.

Nothing in phase 1 asks the decider a question: its first caller is the
phase 2 scheduler (intake scoring and routing). Until then vigild doesn't
build one at startup; this is the one place it will.
"""

from __future__ import annotations

import httpx2

from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.decider.backends.haiku import HaikuBackend
from vigild.decider.backends.jev import JevCloudflare, JevOpenRouter
from vigild.decider.policy import Backend, Decider
from vigild.gateway.service import Gateway


def build_decider(
    cfg: Config, store: Store, gateway: Gateway, secrets: Secrets, client: httpx2.Client
) -> Decider:
    backends: dict[str, Backend] = {"haiku": HaikuBackend(gateway)}
    account, cf_token = secrets.get("CLOUDFLARE_ACCOUNT_ID"), secrets.get("CLOUDFLARE_API_TOKEN")
    if account and cf_token:
        backends["jev-cloudflare"] = JevCloudflare(
            client, account_id=account, token=cf_token, meter=gateway.metered
        )
    if or_key := secrets.get("OPENROUTER_API_KEY"):
        backends["jev-openrouter"] = JevOpenRouter(client, token=or_key, meter=gateway.metered)
    return Decider(cfg, store, backends)
