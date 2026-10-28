"""Policy as code: configs/*.yaml, validated at load. Plus the env file.

vigild refuses to start on an invalid config, including a private repo that
lists a hosted backend or decider route it may not use (vigild/repos.py).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vigild.contract.models import Backend

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
DEFAULT_ENV_FILE = Path("~/.config/vigil/env").expanduser()


class ConfigError(ValueError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preset(Backend):
    locality: Literal["hosted", "local"]
    provider: str
    live: bool = True


class DeciderRoute(_M):
    backend: Literal["haiku", "jev_cloudflare", "jev_openrouter", "lfm_tower"]
    provider: str
    locality: Literal["hosted", "local"]
    preset: str | None = None
    model: str | None = None
    key_ref: str | None = None
    live: bool = True
    # Jev: the exact version its responses must report (owner, 2026-10-23);
    # required on every Jev route, so no route skips the drift guard (review A8)
    version_pin: str | None = None

    @model_validator(mode="after")
    def _jev_is_pinned(self) -> DeciderRoute:
        if self.backend.startswith("jev_") and not self.version_pin:
            raise ValueError(f"a {self.backend} route needs its version_pin")
        return self


class BackendsFile(_M):
    presets: dict[str, Preset]
    decider_routes: dict[str, DeciderRoute]


class HiddenTestsPolicy(_M):
    """Host policy for hidden tests (owner, 2026-10-26). Off unless vigild's own
    configs/repos.yaml turns it on for the repo: nothing in a target repo can.
    Off, no author, hidden run or tag exists, and a runner-file task goes to the
    owner as a draft PR on its visible tests."""

    enabled: bool = False


class RepoPolicy(_M):
    # No `visibility`: GitHub's live answer is the only source (owner,
    # 2026-10-23; vigild/repos.py `live_private`).
    decider: list[str]
    backends: list[str]
    worker_overflow: list[str] = Field(default_factory=list)
    auto_merge: bool = False
    # The repo's dev image (owner direction, review round 2; vigild/devimage.py):
    # vigild builds dev_dockerfile from dev_lockfiles at the base commit and
    # validates it by running every dev_suites command, offline, on a clean
    # checkout. dev_excluded names suites that can't run in a Linux container.
    dev_dockerfile: str
    dev_lockfiles: list[str] = Field(min_length=1)
    dev_suites: dict[str, str] = Field(min_length=1)
    dev_excluded: dict[str, str] = Field(default_factory=dict)
    # Files that decide how tests run (round 3, F1): the verifier lays the
    # base's copies over every run, and a worker's change to one is rejected
    # unless the approved spec names its exact path. Strict globs.
    runner_config: list[str] = Field(default_factory=list)
    # Money-adjacent paths (owner, 2026-10-27; vigild/review/ladder.py): a task
    # whose scope touches one is pinned at ladder rung 0 for good. Strict
    # globs, per repo, no global list. No other effect: routing and intake
    # ignore it.
    ladder_pinned_paths: list[str] = Field(default_factory=list)
    hidden_tests: HiddenTestsPolicy = Field(default_factory=HiddenTestsPolicy)
    # Commands a test_cmd may use in the image. A test_cmd that needs anything
    # else is refused at the spec gate (review r1, blocker 4).
    dev_image_tools: list[str]
    # Poll the repo's open issues labelled `vigil:ready` into jobs (plan 2a,
    # item 2). Off unless a repo turns it on.
    intake: bool = False

    @model_validator(mode="after")
    def _pinned_paths_are_well_formed(self) -> RepoPolicy:
        # A malformed entry would never match, so its pin would fail open
        # (#59 review r1): refuse it at load, by the spec gate's own rule.
        from vigild.intake.paths import scope_entry_error

        for entry in self.ladder_pinned_paths:
            if err := scope_entry_error(entry):
                raise ValueError(f"ladder_pinned_paths: {err}")
        return self


class ReposFile(_M):
    repos: dict[str, RepoPolicy]


class Routing(_M):
    tiers: dict[str, str]
    fallback: str
    coord: str
    reviewer: str
    auditor: str
    money_paths: list[str]
    architecture_labels: list[str]
    local_labels: list[str]
    xs_labels: list[str]
    escalate_attempt: int = 3
    escalate_review_rounds: int = 2
    # Where a preset's work goes while its backend breaker is open (plan 2a,
    # item 4), in order. A preset with no healthy fallback pauses dispatch.
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class Budgets(_M):
    daily_usd: float = Field(gt=0)
    per_task_usd: float = Field(gt=0)
    monthly_usd: float = Field(gt=0)  # mandatory: no default
    wip_per_repo: int = Field(ge=1)
    coord_session_usd: float = Field(gt=0)
    decider_call_usd: float = Field(gt=0)
    hidden_tests_session_usd: float = Field(gt=0)  # one task's whole author stage
    # Each model call's output cap for every coord and worker session.
    session_max_output_tokens: int = Field(ge=1024)
    # Reservations for one reviewer and one auditor session (phase 2a). They
    # count against the day and the month, not the task's own cap, so a fix
    # round gets what's left of the worker's budget.
    review_session_usd: float = Field(default=0.40, gt=0)
    audit_session_usd: float = Field(default=0.80, gt=0)
    # Haiku decisions Jev couldn't give, per repo per UTC day (owner ruling 4)
    decider_fallback_per_repo_usd: float = Field(default=1.00, gt=0)
    # The experiment line (owner, 2026-10-27): bench reservations draw from this
    # monthly ledger and never from the day; they count toward monthly_usd.
    experiment_monthly_usd: float = Field(default=30.00, gt=0)
    bench_task_usd: float = Field(default=1.50, gt=0)  # a bench task's own cap


class ShapePolicy(_M):
    rung: int = Field(ge=0, le=3)
    max_rung: int = Field(default=3, ge=0, le=3)


class RungPolicy(_M):
    reviewer: str
    auditor: str
    audit: float = Field(ge=0, le=1)  # base share of reviews audited
    promote_after: int | None = Field(default=None, ge=1)  # weighted PRs; None: never promotes
    min_match: float = Field(default=0.9, ge=0, le=1)
    max_false_per_pr: float | None = Field(default=None, ge=0)


class ReviewPolicy(_M):
    """configs/review.yaml: where each shape starts and what moves it (item 1)."""

    shapes: dict[str, ShapePolicy]
    rungs: dict[int, RungPolicy]
    verify_weight: dict[int, float]
    level0_audit_factor: float = Field(default=1.0, ge=1)
    # The passed local-reviewer eval (design: "a reviewer enters rung 1 only if
    # it matches >= 80% of the auditor's findings on the eval set"), named by
    # its results file. None until phase 7: no shape passes rung 1, and every
    # rung keeps Sonnet reviewing and Opus auditing every review.
    local_reviewer_eval: str | None = None


class LongContext(_M):
    threshold: int
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


class ModelPrice(_M):
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float
    long_context: LongContext | None = None


class Prices(_M):
    as_of: str
    models: dict[str, ModelPrice]


class GatewayEgress(_M):
    host: str
    port: int
    allow_address: str


class ProxyEgress(_M):
    alias: str
    port: int


class CollectorEgress(_M):
    host: str
    port: int


class Egress(_M):
    gateway: GatewayEgress
    collector: CollectorEgress
    proxy: ProxyEgress
    roles: dict[str, list[str]]


class Bind(_M):
    host: str
    port: int
    tailscale_host: str | None = None
    # The UI session cookie's Secure flag: true only when the UI is reached over
    # HTTPS (`tailscale serve`); a Secure cookie is never sent over plain HTTP.
    cookie_secure: bool = False


class Images(_M):
    claude_executor: str
    egress_proxy: str


class Collector(_M):
    container: str
    otlp_http: str


class GitHub(_M):
    owner: str
    claude_mods: str
    claude_mods_sha: str


class ContainerLimits(_M):
    memory: str
    pids_limit: int
    uid: int
    # The verifier's container holds the checkout, the repo's dependencies and
    # a database in tmpfs, so it gets more (review round 2).
    verifier_memory: str = "6g"
    # Verifier containers at once on this machine (round 4): two ParkAgent
    # verifiers at 6 GiB plus tmpfs don't fit the 11.7 GiB Docker VM.
    verifier_concurrency: int = Field(default=1, ge=1)


class Scheduler(_M):
    """The fleet (plan 2a, items 2-3)."""

    sessions: int = Field(default=3, ge=1)  # sessions at once, across every job
    intake_poll_seconds: int = Field(default=300, ge=10)  # the vigil:ready label
    pr_poll_seconds: int = Field(default=120, ge=10)  # merged PRs (deps auto-resume)
    resume_attempts: int = Field(default=2, ge=0)  # crash resumes per task
    checkpoint_seconds: int = Field(default=30, ge=1)  # a worker's checkpoints, at most this often


class BreakerLimits(_M):
    """Circuit breakers (plan 2a, item 4; vigild/breakers.py)."""

    backend_errors: int = Field(default=3, ge=1)  # 5xx or timeouts in a row
    backend_cooldown_seconds: int = Field(default=300, ge=1)  # then one half-open probe
    repo_failures: int = Field(default=3, ge=1)  # failed or abandoned jobs in a row
    verifier_unknowns: int = Field(default=3, ge=1)  # `unknown` verdicts in a row
    max_escalations_per_day: int = Field(default=5, ge=0)


class Vigild(_M):
    data_dir: str
    api: Bind
    gateway: Bind
    fleet_coord: str
    images: Images
    collector: Collector
    github: GitHub
    container: ContainerLimits
    scheduler: Scheduler = Field(default_factory=Scheduler)
    breakers: BreakerLimits = Field(default_factory=BreakerLimits)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()


# --- sandbox hosts (ADR-0004) ---------------------------------------------------

LOCAL_HOST = "local"
DEFAULT_CONTEXT = "default"  # docker.from_env(): the Docker on this machine
PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)


def is_private_address(value: str) -> bool:
    """RFC 1918, or the 100.64.0.0/10 range a private overlay network uses."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(ip in net for net in PRIVATE_NETS)


class SandboxHost(_M):
    name: str
    docker_context: str = DEFAULT_CONTEXT
    arch: Literal["arm64", "amd64"]
    memory_gb: float = Field(gt=0)  # the caps of the host's dedicated slice
    cpus: float = Field(gt=0)
    disk_budget_gb: float = Field(gt=0)  # the most space vigil's images and containers may use
    uses: list[Literal["jobs", "bench"]] = Field(min_length=1)
    gateway_url: str  # how this host's containers reach vigild's gateway
    engine_pin: str | None = None  # this host's exact Docker Engine version
    private_address: str | None = None  # the host's address, the gateway's allowed source
    cgroup_slice: str = "vigil.slice"  # every vigild container sets it as cgroup_parent

    @property
    def remote(self) -> bool:
        return self.docker_context != DEFAULT_CONTEXT

    @model_validator(mode="after")
    def _rules(self) -> SandboxHost:
        if not self.gateway_url.startswith("http://"):
            raise ValueError(f"{self.name}: gateway_url must be http://host:port")
        if self.name == LOCAL_HOST and self.remote:
            raise ValueError("the `local` host stays on the default Docker context")
        if self.remote:
            if not self.engine_pin:
                raise ValueError(f"{self.name}: a remote host needs its engine_pin")
            if not self.private_address:
                raise ValueError(f"{self.name}: a remote host needs its private_address")
            if not is_private_address(self.private_address):
                raise ValueError(f"{self.name}: private_address is not a private address")
            if not self.cgroup_slice.endswith(".slice"):
                raise ValueError(f"{self.name}: cgroup_slice must be a systemd slice")
        return self


class BenchPolicy(_M):
    allow_emulation: bool = False


class SandboxHostsFile(_M):
    bench: BenchPolicy = Field(default_factory=BenchPolicy)
    gateway_bind: str | None = None
    hosts: list[SandboxHost]

    @model_validator(mode="after")
    def _rules(self) -> SandboxHostsFile:
        names = [h.name for h in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("sandbox host names must be unique")
        if names.count(LOCAL_HOST) != 1:
            raise ValueError("configs/sandbox_hosts.yaml needs exactly one `local` host")
        if self.remote_hosts() and not (
            self.gateway_bind and is_private_address(self.gateway_bind)
        ):
            raise ValueError("remote hosts need gateway_bind, a private address")
        return self

    def remote_hosts(self) -> list[SandboxHost]:
        return [h for h in self.hosts if h.remote]

    def host(self, name: str) -> SandboxHost:
        for h in self.hosts:
            if h.name == name:
                return h
        raise ConfigError(f"unknown sandbox host {name!r}")

    @property
    def local(self) -> SandboxHost:
        return self.host(LOCAL_HOST)


class Config(_M):
    backends: BackendsFile
    repos: ReposFile
    routing: Routing
    budgets: Budgets
    prices: Prices
    egress: Egress
    vigild: Vigild
    review: ReviewPolicy
    sandbox_hosts: SandboxHostsFile

    def preset(self, name: str) -> Preset:
        try:
            return self.backends.presets[name]
        except KeyError as e:
            raise ConfigError(f"unknown backend preset {name!r}") from e

    def repo(self, name: str) -> RepoPolicy:
        try:
            return self.repos.repos[name]
        except KeyError as e:
            raise ConfigError(f"repo {name!r} is not in configs/repos.yaml") from e


FILES = {
    "backends": "backends.yaml",
    "repos": "repos.yaml",
    "routing": "routing.yaml",
    "budgets": "budgets.yaml",
    "prices": "prices.yaml",
    "egress": "egress.yaml",
    "vigild": "vigild.yaml",
    "review": "review.yaml",
    "sandbox_hosts": "sandbox_hosts.yaml",
}


def load(config_dir: Path = CONFIG_DIR, overrides: Mapping[str, object] | None = None) -> Config:
    raw: dict[str, object] = {}
    for key, fname in FILES.items():
        raw[key] = yaml.safe_load((config_dir / fname).read_text())
    raw.update(overrides or {})
    cfg = Config.model_validate(raw)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    from vigild.repos import validate_repo_policies  # import here: repos imports config's types

    presets = cfg.backends.presets
    for tier, preset in cfg.routing.tiers.items():
        if preset not in presets:
            raise ConfigError(f"routing tier {tier} names unknown preset {preset}")
    for role in ("fallback", "coord", "reviewer", "auditor"):
        if getattr(cfg.routing, role) not in presets:
            raise ConfigError(f"routing.{role} names an unknown preset")
    for preset, falls in cfg.routing.fallbacks.items():
        for name in (preset, *falls):
            if name not in presets:
                raise ConfigError(f"routing.fallbacks names an unknown preset {name}")
    for name, route in cfg.backends.decider_routes.items():
        if route.preset and route.preset not in presets:
            raise ConfigError(f"decider route {name} names unknown preset {route.preset}")
    for p in presets.values():
        if p.model and p.locality == "hosted" and p.model not in cfg.prices.models:
            raise ConfigError(f"no price for hosted model {p.model} in prices.yaml")
    for repo, pol in cfg.repos.repos.items():
        df = CONFIG_DIR.parent / pol.dev_dockerfile
        if not pol.dev_dockerfile.startswith("images/dev/") or not df.is_file():
            raise ConfigError(f"{repo}: dev_dockerfile {pol.dev_dockerfile} is not in images/dev/")
        for path in pol.dev_lockfiles:
            if path.startswith("/") or ".." in Path(path).parts:
                raise ConfigError(f"{repo}: bad dev_lockfiles entry {path!r}")
    validate_repo_policies(cfg)
    from vigild.review.ladder import validate_policy

    validate_policy(cfg)


# --- the env file -------------------------------------------------------------


class Secrets:
    """Values from ~/.config/vigil/env. Never printed: repr shows names only."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        v = self._values.get(name)
        return v if v else None

    def names(self) -> list[str]:
        return sorted(k for k, v in self._values.items() if v)

    def __repr__(self) -> str:
        return f"Secrets(names={self.names()})"


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def load_secrets(path: Path = DEFAULT_ENV_FILE) -> Secrets:
    """vigild's startup read of the env file. Refuses a file others can read."""
    if not path.exists():
        return Secrets({})
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(f"{path} must be mode 600 (is {oct(mode)})")
    return Secrets(parse_env(path.read_text()))


def env_override(name: str) -> str | None:
    """Non-secret overrides for tests and CI (e.g. VIGIL_DATA_DIR)."""
    return os.environ.get(name) or None
