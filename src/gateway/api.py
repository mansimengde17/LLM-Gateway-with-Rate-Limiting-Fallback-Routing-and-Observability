"""FastAPI surface for the gateway."""

from __future__ import annotations

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .core import Gateway, GatewayError

app = FastAPI(title="LLM Gateway", version="1.0.0")
gateway = Gateway()


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = 256


class TeamUpdate(BaseModel):
    actor: str
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    daily_budget_usd: float | None = None


@app.post("/v1/completions")
def completions(request: CompletionRequest,
                authorization: str = Header(default="")):
    api_key = authorization.removeprefix("Bearer ").strip()
    try:
        return gateway.complete(api_key, request.model, request.prompt,
                                request.max_tokens)
    except GatewayError as error:
        headers = {}
        if error.retry_after:
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse(status_code=error.status,
                            content={"error": error.detail}, headers=headers)


@app.get("/v1/health")
def health():
    gateway.health.probe_all()
    return {"providers": gateway.health.status(),
            "circuit_breakers": gateway.router.breaker_states()}


@app.get("/admin/teams")
def teams():
    return {"teams": {name: {k: v for k, v in cfg.items() if k != "api_key"}
                      for name, cfg in gateway.teams.items()},
            "rate_limits": {name: gateway.limiter.status(name)
                            for name in gateway.teams},
            "budgets": gateway.budget.report()}


@app.patch("/admin/teams/{team}")
def update_team(team: str, update: TeamUpdate):
    changes = {k: v for k, v in update.model_dump().items()
               if v is not None and k != "actor"}
    try:
        return gateway.update_team(team, update.actor, **changes)
    except GatewayError as error:
        return JSONResponse(status_code=error.status,
                            content={"error": error.detail})


@app.get("/admin/audit")
def audit():
    return gateway.audit_log[-100:]


@app.get("/metrics")
def metrics():
    return PlainTextResponse(gateway.metrics.prometheus())


@app.get("/v1/fallback-events")
def fallback_events():
    return gateway.router.fallback_events[-50:]
