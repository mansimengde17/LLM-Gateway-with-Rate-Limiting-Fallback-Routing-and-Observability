"""Token bucket rate limiting with priority aware admission.

The bucket algorithm matches what a Redis Lua script would do in a
distributed deployment: refill on read, atomic take, and a Retry-After
hint when the bucket is empty. Batch traffic is admitted only while the
bucket holds comfortable headroom so realtime requests are never starved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

BATCH_HEADROOM = 0.30  # batch requests need 30 percent of the bucket free


@dataclass
class Bucket:
    capacity: float
    refill_per_second: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity,
                          self.tokens + (now - self.updated)
                          * self.refill_per_second)
        self.updated = now

    def take(self, amount: float, priority: str = "realtime") -> tuple[bool, float]:
        """Try to take tokens. Returns (allowed, retry_after_seconds)."""
        self._refill()
        floor = self.capacity * BATCH_HEADROOM if priority == "batch" else 0.0
        if self.tokens - amount >= floor:
            self.tokens -= amount
            return True, 0.0
        deficit = amount + floor - self.tokens
        return False, round(deficit / self.refill_per_second, 2)


class RateLimiter:
    def __init__(self):
        self._request_buckets: dict[str, Bucket] = {}
        self._token_buckets: dict[str, Bucket] = {}
        self.rejections = 0

    def configure(self, team: str, rpm: int, tpm: int) -> None:
        self._request_buckets[team] = Bucket(rpm, rpm / 60)
        self._token_buckets[team] = Bucket(tpm, tpm / 60)

    def admit(self, team: str, estimated_tokens: int,
              priority: str) -> tuple[bool, float, str]:
        ok, retry = self._request_buckets[team].take(1, priority)
        if not ok:
            self.rejections += 1
            return False, retry, "requests_per_minute"
        ok, retry = self._token_buckets[team].take(estimated_tokens, priority)
        if not ok:
            self.rejections += 1
            return False, retry, "tokens_per_minute"
        return True, 0.0, ""

    def status(self, team: str) -> dict:
        req, tok = self._request_buckets[team], self._token_buckets[team]
        req._refill(), tok._refill()
        return {"requests_remaining": round(req.tokens, 1),
                "tokens_remaining": round(tok.tokens),
                "requests_capacity": req.capacity,
                "tokens_capacity": tok.capacity}
