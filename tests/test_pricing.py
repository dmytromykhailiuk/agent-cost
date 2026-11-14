import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "agent-cost" / "lib"))

import helpers  # noqa: F401,E402
from costlib import pricing  # noqa: E402


class PricingTests(unittest.TestCase):
    def test_resolve_model(self):
        self.assertEqual(pricing.resolve_model("claude-opus-5-5"), "claude-opus-5-5")
        self.assertEqual(pricing.resolve_model("opus"), "claude-opus-5-5")
        self.assertEqual(pricing.resolve_model("claude-sonnet-5-5-20260101"), "claude-sonnet-5-5")
        self.assertEqual(pricing.resolve_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertIsNone(pricing.resolve_model("gpt-5"))
        self.assertIsNone(pricing.resolve_model(None))

    def test_cache_write_split(self):
        usage = {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 3000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000}}
        expected = (10 * 4 + 50 * 20 + 1000 * 0.2 + 2000 * 5 + 1000 * 8) / 1e6
        self.assertAlmostEqual(pricing.request_cost("claude-opus-5-5", usage), expected)

    def test_legacy_cache_write_is_5m(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000}
        self.assertAlmostEqual(pricing.request_cost("claude-sonnet-5-5", usage), 1000 * 2.5 / 1e6)

    def test_long_context_rate(self):
        usage = {"input": 50000, "output": 1000, "cache_read": 60000, "cache_write_5m": 0, "cache_write_1h": 0}
        expected = (50000 * 0.5 + 60000 * 0.05 + 1000 * 2.5) / 1e6
        self.assertAlmostEqual(pricing.request_cost("claude-haiku-5-5", usage), expected)
        small = dict(usage, input=1000, cache_read=0)
        self.assertAlmostEqual(pricing.request_cost("claude-haiku-5-5", small), (1000 * 0.1 + 1000 * 0.5) / 1e6)

    def test_unknown_model(self):
        self.assertIsNone(pricing.request_cost("gpt-5", {"input_tokens": 10}))

    def test_price_by_model(self):
        by_model = {
            "claude-sonnet-5-5": {"input": 1000, "output": 100, "cache_read": 0, "cache_write_5m": 0,
                                  "cache_write_1h": 0, "requests": 1},
            "gpt-5": {"input": 5, "output": 5, "requests": 1},
        }
        total, priced, unknown = pricing.price_by_model(by_model)
        self.assertAlmostEqual(total, (1000 * 2 + 100 * 10) / 1e6)
        self.assertEqual(unknown, ["gpt-5"])
        self.assertIsNone(priced["gpt-5"]["cost_usd"])
        self.assertEqual(priced["claude-sonnet-5-5"]["requests"], 1)

    def test_price_by_model_all_unknown(self):
        total, _, unknown = pricing.price_by_model({"gpt-5": {"input": 5}})
        self.assertIsNone(total)
        self.assertEqual(unknown, ["gpt-5"])
        self.assertEqual(pricing.price_by_model({})[0], 0.0)

    def test_price_by_model_keeps_precomputed_cost(self):
        total, priced, _ = pricing.price_by_model({"claude-opus-5-5": {"input": 1, "cost_usd": 0.5}})
        self.assertEqual(total, 0.5)
        self.assertEqual(priced["claude-opus-5-5"]["cost_usd"], 0.5)


if __name__ == "__main__":
    unittest.main()
