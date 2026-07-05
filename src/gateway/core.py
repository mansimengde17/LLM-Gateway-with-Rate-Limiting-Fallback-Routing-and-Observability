"""Gateway core: wires auth, limits, budget, resilience, and telemetry."""

from __future__ import annotations

import pathlib
import time

import yaml

from .budget import BudgetTracker
from .health import HealthChecker
from .providers import MODEL_CATALOG, ProviderError, ProviderPool
from .ratelimit import RateLimiter
from .resilience import ResilientRouter
from .telemetry import Metrics, Tracer

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"


class GatewayError(Exception):
    def __init__(self, status: int, detail: str, retry_after: float = 0.0):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class Gateway:
    def __init__(self, teams_path=None, fallbacks_path=None, sleep=None):
        self.pool = ProviderPool()
        self.limiter = RateLimiter()
        self.budget = BudgetTracker()
        self.tracer = Tracer()
        self.metrics = Metrics()
        self.health = HealthChecker(self.pool)
        self.audit_log: list[dict] = []
        self._load_teams(teams_path or CONFIG_DIR / "teams.yaml")
        fallbacks = yaml.safe_load(
            open(fallbacks_path or CONFIG_DIR / "fallbacks.yaml"))
        kwargs = {"sleep": sleep} if sleep else {}
        self.router = ResilientRouter(self.pool, fallbacks, **kwargs)

    def _load_teams(self, path) -> None:
        self.teams = yaml.safe_load(open(path))["teams"]
        self.keys = {cfg["api_key"]: name
                     for name, cfg in self.teams.items()}
        for name, cfg in self.teams.items():
            self.limiter.configure(name, cfg["requests_per_minute"],
                                   cfg["tokens_per_minute"])
            self.budget.configure(name, cfg["daily_budget_usd"])

    def authenticate(self, api_key: str) -> tuple[str, dict]:
        team = self.keys.get(api_key)
        if team is None:
            raise GatewayError(401, "unknown API key")
        return team, self.teams[team]

    def update_team(self, team: str, actor: str, **changes) -> dict:
        if team not in self.teams:
            raise GatewayError(404, f"unknown team {team}")
        self.teams[team].update(changes)
        cfg = self.teams[team]
        self.limiter.configure(team, cfg["requests_per_minute"],
                               cfg["tokens_per_minute"])
        if "daily_budget_usd" in changes:
            self.budget.configure(team, cfg["daily_budget_usd"])
        entry = {"actor": actor, "team": team, "changes": changes,
                 "at": time.time()}
        self.audit_log.append(entry)
        return entry

    def complete(self, api_key: str, model: str, prompt: str,
                 max_tokens: int = 256, system_inject: str = "") -> dict:
        trace_id = self.tracer.start_trace()
        overall = self.tracer.span(trace_id, "gateway.request", model=model)

        auth_span = self.tracer.span(trace_id, "auth")
        team, cfg = self.authenticate(api_key)
        auth_span.end()

        if model not in MODEL_CATALOG:
            raise GatewayError(400, f"unknown model {model}")
        if model not in cfg["allowed_models"]:
            raise GatewayError(
                403, f"team {team} is not allowed to use {model}")

        limit_span = self.tracer.span(trace_id, "rate_limit", team=team)
        estimated = max(16, len(prompt) // 4) + max_tokens
        allowed, retry_after, which = self.limiter.admit(
            team, estimated, cfg.get("priority", "realtime"))
        limit_span.end()
        if not allowed:
            self.metrics.inc("gateway_rejections_total", team=team,
                             reason=which)
            raise GatewayError(429, f"{which} limit reached for {team}",
                               retry_after=retry_after)

        decision = self.budget.check(team)
        if not decision.allowed:
            self.metrics.inc("gateway_rejections_total", team=team,
                             reason="budget")
            raise GatewayError(402, decision.reason)

        enriched = prompt
        if system_inject:
            enriched = f"[policy] {system_inject}\n{prompt}"

        call_span = self.tracer.span(trace_id, "llm.call", model=model)
        try:
            response, served_by, attempts = self.router.complete(
                model, enriched, max_tokens)
        except ProviderError as error:
            call_span.end()
            self.metrics.inc("gateway_errors_total", team=team, model=model)
            raise GatewayError(503, str(error))
        call_span.end()

        self.budget.record(team, response.cost_usd)
        self.metrics.inc("gateway_requests_total", team=team,
                         model=served_by, provider=response.provider)
        self.metrics.inc("gateway_cost_usd_total", response.cost_usd,
                         team=team)
        self.metrics.inc("gateway_tokens_total",
                         response.input_tokens + response.output_tokens,
                         team=team)
        self.metrics.observe("gateway_latency_ms", response.latency_ms,
                             provider=response.provider)
        if served_by != model:
            self.metrics.inc("gateway_fallbacks_total", requested=model,
                             served=served_by)
        overall.end()

        return {"trace_id": trace_id, "team": team,
                "model_requested": model, "model_served": served_by,
                "provider": response.provider, "text": response.text,
                "attempts": attempts,
                "usage": {"input_tokens": response.input_tokens,
                          "output_tokens": response.output_tokens,
                          "cost_usd": response.cost_usd},
                "latency_ms": response.latency_ms,
                "budget_warning": decision.warning or None,
                "fell_back": served_by != model}
