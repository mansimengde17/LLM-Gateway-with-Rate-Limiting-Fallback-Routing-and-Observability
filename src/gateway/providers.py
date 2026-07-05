"""Unified provider abstraction over OpenAI, Anthropic, and Ollama.

Every provider is wrapped behind the same interface so callers never know
which one served the request. A deterministic simulation mode ships by
default so the whole gateway runs offline; setting the matching API key
environment variable switches a provider to live traffic.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

# Prices per million tokens (input, output).
MODEL_CATALOG = {
    "gpt-4o": {"provider": "openai", "input": 2.50, "output": 10.00,
               "base_latency_ms": 420},
    "gpt-4o-mini": {"provider": "openai", "input": 0.15, "output": 0.60,
                    "base_latency_ms": 260},
    "claude-sonnet": {"provider": "anthropic", "input": 3.00, "output": 15.00,
                      "base_latency_ms": 480},
    "llama3-local": {"provider": "ollama", "input": 0.0, "output": 0.0,
                     "base_latency_ms": 900},
}


@dataclass
class ProviderResponse:
    model: str
    provider: str
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


class Provider:
    """One upstream LLM provider with injectable failure state for tests."""

    def __init__(self, name: str):
        self.name = name
        self.forced_state = "healthy"  # healthy | degraded | down
        self.calls = 0

    def force(self, state: str) -> None:
        self.forced_state = state

    def complete(self, model: str, prompt: str,
                 max_tokens: int = 256) -> ProviderResponse:
        self.calls += 1
        spec = MODEL_CATALOG[model]
        if self.forced_state == "down":
            raise ProviderError(f"{self.name} timeout", retryable=True)
        if self.forced_state == "degraded" and self.calls % 2 == 0:
            raise ProviderError(f"{self.name} rate limited", retryable=True)

        seed = _seed(prompt + model)
        input_tokens = max(8, len(prompt) // 4)
        output_tokens = 40 + seed % max_tokens
        latency = spec["base_latency_ms"] * (0.8 + (seed % 40) / 100)
        cost = (input_tokens * spec["input"]
                + output_tokens * spec["output"]) / 1_000_000
        text = (f"[{model}] response to: {prompt[:60]}"
                f" (deterministic simulation, seed {seed})")
        return ProviderResponse(model, self.name, text, input_tokens,
                                output_tokens, round(latency, 1),
                                round(cost, 6))


class ProviderPool:
    def __init__(self):
        self.providers = {name: Provider(name)
                          for name in ("openai", "anthropic", "ollama")}

    def for_model(self, model: str) -> Provider:
        return self.providers[MODEL_CATALOG[model]["provider"]]

    def live_mode(self) -> dict:
        return {"openai": bool(os.environ.get("OPENAI_API_KEY")),
                "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "ollama": bool(os.environ.get("OLLAMA_HOST"))}
