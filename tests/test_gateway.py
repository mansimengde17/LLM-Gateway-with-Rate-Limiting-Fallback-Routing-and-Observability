import sys
import unittest

sys.path.insert(0, "src")

from gateway.core import Gateway, GatewayError
from gateway.ratelimit import Bucket


def make_gateway():
    return Gateway(sleep=lambda _: None)


class RateLimitTests(unittest.TestCase):
    def test_bucket_exhausts_and_reports_retry_after(self):
        bucket = Bucket(capacity=3, refill_per_second=1)
        for _ in range(3):
            ok, _ = bucket.take(1)
            self.assertTrue(ok)
        ok, retry = bucket.take(1)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)

    def test_batch_priority_keeps_headroom(self):
        bucket = Bucket(capacity=10, refill_per_second=1)
        ok, _ = bucket.take(8, priority="batch")
        self.assertFalse(ok)
        ok, _ = bucket.take(8, priority="realtime")
        self.assertTrue(ok)


class GatewayTests(unittest.TestCase):
    def test_rejects_unknown_key(self):
        gw = make_gateway()
        with self.assertRaises(GatewayError) as ctx:
            gw.complete("bad-key", "gpt-4o", "hello")
        self.assertEqual(ctx.exception.status, 401)

    def test_enforces_model_allowlist(self):
        gw = make_gateway()
        with self.assertRaises(GatewayError) as ctx:
            gw.complete("gw-batch-2c9d", "claude-sonnet", "hello")
        self.assertEqual(ctx.exception.status, 403)

    def test_fallback_when_primary_down(self):
        gw = make_gateway()
        gw.pool.providers["openai"].force("down")
        result = gw.complete("gw-search-7f3a", "gpt-4o", "hello world")
        self.assertTrue(result["fell_back"])
        self.assertNotEqual(result["provider"], "openai")

    def test_budget_cap_blocks(self):
        gw = make_gateway()
        gw.budget.configure("team-search", 0.000001)
        gw.budget.record("team-search", 0.001)
        with self.assertRaises(GatewayError) as ctx:
            gw.complete("gw-search-7f3a", "gpt-4o", "hello")
        self.assertEqual(ctx.exception.status, 402)

    def test_circuit_breaker_opens_after_failures(self):
        gw = make_gateway()
        gw.pool.providers["openai"].force("down")
        for _ in range(3):
            try:
                gw.router._try_model("gpt-4o", "x", 16)
            except Exception:
                pass
        self.assertEqual(gw.router.breakers["openai"].state, "open")

    def test_audit_log_records_admin_changes(self):
        gw = make_gateway()
        gw.update_team("team-batch", actor="oncall",
                       requests_per_minute=10)
        self.assertEqual(gw.audit_log[-1]["changes"],
                         {"requests_per_minute": 10})


if __name__ == "__main__":
    unittest.main()
