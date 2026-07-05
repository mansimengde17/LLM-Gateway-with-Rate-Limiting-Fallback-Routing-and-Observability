"""Continuous provider health checking over rolling windows."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .providers import ProviderError, ProviderPool

WINDOW = 50


@dataclass
class HealthRecord:
    results: list[tuple[bool, float]] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    status: str = "healthy"

    def record(self, ok: bool, latency_ms: float) -> None:
        self.results.append((ok, latency_ms))
        if len(self.results) > WINDOW:
            self.results.pop(0)

    def evaluate(self) -> str:
        if not self.results:
            return "healthy"
        error_rate = sum(1 for ok, _ in self.results if not ok) / len(self.results)
        if error_rate > 0.5:
            new_status = "down"
        elif error_rate > 0.1:
            new_status = "degraded"
        else:
            new_status = "healthy"
        if new_status != self.status:
            self.history.append({"from": self.status, "to": new_status,
                                 "error_rate": round(error_rate, 3),
                                 "at": time.time()})
            self.status = new_status
        return self.status


class HealthChecker:
    """Sends lightweight probe requests and keeps rolling status."""

    def __init__(self, pool: ProviderPool):
        self.pool = pool
        self.records = {name: HealthRecord() for name in pool.providers}

    def probe_all(self) -> dict:
        for name, provider in self.pool.providers.items():
            record = self.records[name]
            start = time.perf_counter()
            try:
                model = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet",
                         "ollama": "llama3-local"}[name]
                provider.complete(model, "health probe", max_tokens=8)
                record.record(True, (time.perf_counter() - start) * 1000)
            except ProviderError:
                record.record(False, (time.perf_counter() - start) * 1000)
            record.evaluate()
        return self.status()

    def status(self) -> dict:
        out = {}
        for name, record in self.records.items():
            latencies = sorted(l for ok, l in record.results if ok)
            p99 = latencies[int(0.99 * (len(latencies) - 1))] if latencies else 0
            out[name] = {"status": record.status,
                         "checks": len(record.results),
                         "error_rate": round(
                             sum(1 for ok, _ in record.results if not ok)
                             / max(1, len(record.results)), 3),
                         "latency_p99_ms": round(p99, 1),
                         "transitions": record.history[-5:]}
        return out
