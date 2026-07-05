"""Retry, fallback routing, and circuit breakers.

Order of defense for every request:
1. Retry the primary with exponential backoff (retryable errors only).
2. If retries exhaust, walk the fallback chain for the model tier.
3. A circuit breaker per provider stops sending traffic to a provider
   that keeps failing, then probes it with a single half open request
   after the cooldown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .providers import MODEL_CATALOG, ProviderError, ProviderPool


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    window_seconds: float = 30.0
    cooldown_seconds: float = 20.0
    state: str = "closed"  # closed | open | half_open
    failures: list[float] = field(default_factory=list)
    opened_at: float = 0.0
    transitions: list[dict] = field(default_factory=list)

    def _transition(self, new_state: str, reason: str) -> None:
        self.transitions.append({"from": self.state, "to": new_state,
                                 "reason": reason, "at": time.time()})
        self.state = new_state

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.cooldown_seconds:
                self._transition("half_open", "cooldown elapsed, probing")
                return True
            return False
        return True  # half_open lets the single probe through

    def record_success(self) -> None:
        if self.state == "half_open":
            self._transition("closed", "probe succeeded")
        self.failures.clear()

    def record_failure(self) -> None:
        now = time.monotonic()
        if self.state == "half_open":
            self.opened_at = now
            self._transition("open", "probe failed")
            return
        self.failures = [t for t in self.failures
                         if now - t < self.window_seconds]
        self.failures.append(now)
        if len(self.failures) >= self.failure_threshold:
            self.opened_at = now
            self._transition("open",
                             f"{len(self.failures)} failures in window")


class ResilientRouter:
    def __init__(self, pool: ProviderPool, fallback_config: dict,
                 sleep=time.sleep):
        self.pool = pool
        self.tiers = fallback_config["tiers"]
        retry = fallback_config.get("retry", {})
        self.max_attempts = retry.get("max_attempts", 3)
        self.base_delay = retry.get("base_delay_ms", 100) / 1000
        cb = fallback_config.get("circuit_breaker", {})
        self.breakers = {name: CircuitBreaker(
            cb.get("failure_threshold", 5), cb.get("window_seconds", 30),
            cb.get("cooldown_seconds", 20)) for name in pool.providers}
        self._sleep = sleep
        self.fallback_events: list[dict] = []

    def chain_for(self, model: str) -> list[str]:
        for tier in self.tiers.values():
            if model in tier["members"]:
                chain = [model] + [m for m in tier["chain"] if m != model]
                return chain
        return [model]

    def _try_model(self, model: str, prompt: str, max_tokens: int):
        provider = self.pool.for_model(model)
        breaker = self.breakers[provider.name]
        if not breaker.allow():
            raise ProviderError(f"circuit open for {provider.name}",
                                retryable=True)
        for attempt in range(self.max_attempts):
            try:
                response = provider.complete(model, prompt, max_tokens)
                breaker.record_success()
                return response, attempt + 1
            except ProviderError as error:
                breaker.record_failure()
                if not error.retryable:
                    raise
                if attempt < self.max_attempts - 1:
                    self._sleep(self.base_delay * (2 ** attempt))
        raise ProviderError(f"{model} exhausted retries", retryable=True)

    def complete(self, model: str, prompt: str, max_tokens: int = 256):
        chain = self.chain_for(model)
        errors = []
        for candidate in chain:
            try:
                response, attempts = self._try_model(candidate, prompt,
                                                     max_tokens)
                if candidate != model:
                    self.fallback_events.append(
                        {"requested": model, "served_by": candidate,
                         "errors": errors, "at": time.time()})
                return response, candidate, attempts
            except ProviderError as error:
                errors.append(str(error))
                if not error.retryable:
                    break
        raise ProviderError(
            f"all providers failed for {model}: {errors}", retryable=False)

    def breaker_states(self) -> dict:
        return {name: {"state": b.state,
                       "recent_failures": len(b.failures),
                       "transitions": b.transitions[-5:]}
                for name, b in self.breakers.items()}
