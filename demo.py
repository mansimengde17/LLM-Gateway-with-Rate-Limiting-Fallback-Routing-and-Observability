"""Offline load test and failover demo for the LLM gateway.

Runs entirely on the deterministic simulation providers:
1. Mixed traffic from three teams flows through the gateway.
2. A simulated OpenAI outage triggers retries, then fallback routing.
3. The circuit breaker opens, traffic shifts, and the breaker recovers.
4. Rate limits and budget caps reject the right requests.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from gateway.core import Gateway, GatewayError

PROMPTS = [
    "Summarize the incident report for the payments outage",
    "Classify this ticket: my invoice is wrong",
    "Write a release note for gateway version 1.0",
    "Extract the vendor name from this contract text",
    "Translate the onboarding guide introduction to French",
]


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main() -> None:
    gateway = Gateway(sleep=lambda _: None)

    section("Phase 1: mixed traffic from three teams")
    traffic = [("gw-search-7f3a", "gpt-4o"), ("gw-batch-2c9d", "gpt-4o-mini"),
               ("gw-support-5e1b", "claude-sonnet")]
    ok = 0
    for i in range(60):
        key, model = traffic[i % 3]
        try:
            gateway.complete(key, model, PROMPTS[i % len(PROMPTS)])
            ok += 1
        except GatewayError as error:
            print(f"  rejected: {error.detail}")
    print(f"  {ok}/60 requests served")

    section("Phase 2: simulated OpenAI outage, fallback routing")
    gateway.pool.providers["openai"].force("down")
    for i in range(6):
        result = gateway.complete("gw-search-7f3a", "gpt-4o",
                                  PROMPTS[i % len(PROMPTS)])
        print(f"  requested gpt-4o -> served by {result['model_served']}"
              f" ({result['provider']}), fell_back={result['fell_back']}")
    print(f"  fallback events recorded:"
          f" {len(gateway.router.fallback_events)}")
    print(f"  openai breaker: "
          f"{gateway.router.breakers['openai'].state}")

    section("Phase 3: provider recovers, breaker closes after probe")
    gateway.pool.providers["openai"].force("healthy")
    breaker = gateway.router.breakers["openai"]
    breaker.cooldown_seconds = 0  # skip the wait for the demo
    result = gateway.complete("gw-search-7f3a", "gpt-4o", PROMPTS[0])
    print(f"  served by {result['model_served']},"
          f" breaker now {breaker.state}")

    section("Phase 4: budget cap enforcement")
    gateway.budget.configure("team-batch", 0.0005)
    blocked = 0
    for i in range(40):
        try:
            gateway.complete("gw-batch-2c9d", "gpt-4o-mini", PROMPTS[i % 5])
        except GatewayError as error:
            blocked += 1
            if blocked == 1:
                print(f"  first block: {error.detail}")
    print(f"  {blocked} requests blocked by the budget cap")

    section("Phase 5: health and metrics snapshot")
    gateway.health.probe_all()
    for name, info in gateway.health.status().items():
        print(f"  {name}: {info['status']}"
              f" error_rate={info['error_rate']}")
    summary = gateway.metrics.summary()
    for key, value in list(summary["counters"].items())[:8]:
        print(f"  {key} = {value}")
    print("\nDemo complete. Start the API with:"
          " uvicorn gateway.api:app --app-dir src")


if __name__ == "__main__":
    main()
