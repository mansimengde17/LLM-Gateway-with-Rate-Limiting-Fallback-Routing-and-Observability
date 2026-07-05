# LLM Gateway with Rate Limiting, Fallback Routing, and Observability

A production API gateway that sits in front of every LLM call an organization
makes. It authenticates teams, enforces rate limits and budgets, retries and
falls back across providers during outages, opens circuit breakers on
repeated failure, and exports traces and metrics for every request.

Live demo: https://mansimengde17.github.io/LLM-Gateway-with-Rate-Limiting-Fallback-Routing-and-Observability/

## Why this exists

Once more than one team calls LLM APIs, every company ends up needing the
same piece of infrastructure: a single choke point that answers who called
which model, what it cost, what happens when the provider goes down, and how
a team is stopped from burning the monthly budget in an afternoon. This
gateway is that choke point.

## Architecture

```
client -> auth -> rate limiter -> budget check -> resilient router -> provider
                                                      |                (openai,
                                                      |                anthropic,
                                                 retry w/ backoff      ollama)
                                                 fallback chain
                                                 circuit breaker
                    every hop emits spans and Prometheus metrics
```

- `src/gateway/providers.py` unified provider interface with a deterministic
  simulation mode so everything runs offline
- `src/gateway/ratelimit.py` token buckets per team for requests and tokens,
  with priority headroom so realtime traffic beats batch jobs
- `src/gateway/budget.py` daily spend caps with a warning at 80 percent
- `src/gateway/resilience.py` retry with exponential backoff, tier based
  fallback chains, and per provider circuit breakers
- `src/gateway/health.py` rolling window health status per provider
- `src/gateway/telemetry.py` span tracing and a Prometheus text endpoint
- `src/gateway/api.py` FastAPI app with completion, admin, and metrics routes

## Quick start

```bash
pip install -r requirements.txt
python demo.py                       # offline load test and failover demo
python -m unittest discover tests    # test suite
uvicorn gateway.api:app --app-dir src --port 8000
```

Example request:

```bash
curl -s localhost:8000/v1/completions \
  -H "Authorization: Bearer gw-search-7f3a" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "prompt": "Summarize this incident"}'
```

Key endpoints:

| Route | Purpose |
|-------|---------|
| `POST /v1/completions` | unified completion API for all providers |
| `GET /v1/health` | provider health and circuit breaker states |
| `GET /admin/teams` | limits, budgets, and utilization per team |
| `PATCH /admin/teams/{team}` | hot reload limits without a restart |
| `GET /metrics` | Prometheus exposition format |
| `GET /v1/fallback-events` | recent failovers with their causes |

## Configuration

Teams live in `config/teams.yaml` (API key, allowed models, limits, budget,
priority). Fallback chains, retry policy, and breaker thresholds live in
`config/fallbacks.yaml`. Both are hot reloadable through the admin API.

## Docker

```bash
docker compose up --build
```

## Notes

Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OLLAMA_HOST` to route real
traffic. Without keys the deterministic simulation providers serve
responses, which keeps the demo and the test suite fully reproducible.
