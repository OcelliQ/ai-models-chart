"""Check indicative tier boundaries, unknowns, and shared-weight concurrency."""

import csv
import json
import unittest
from pathlib import Path

from build_hardware import classify, memory_for_sessions, parameter_tier

HERE = Path(__file__).resolve().parent


class HostingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = json.loads((HERE / "hosting_tiers.json").read_text())

    def test_parameter_boundaries(self):
        for count, expected in [(32, 0), (32.1, 1), (180, 1), (180.1, 2),
                                (360, 2), (360.1, 3), (800, 3), (800.1, 4),
                                (1600, 4), (1600.1, 5), (2800, 5)]:
            with self.subTest(count=count):
                self.assertEqual(parameter_tier(count, self.policy["tiers"]), expected)
        for invalid in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                parameter_tier(invalid, self.policy["tiers"])

    def test_shared_weights_are_not_multiplied_by_sessions(self):
        one = memory_for_sessions(320, 224, 1, self.policy)
        four = memory_for_sessions(320, 224, 4, self.policy)
        self.assertAlmostEqual(four - one, 3 * 16 * 1e9 / 2**30)
        self.assertLess(four, 2 * one)

    def test_large_resident_estimate_can_raise_parameter_tier(self):
        model = {"canonical_id": "example/model", "open_weights": "true", "context_length": "131072"}
        count = {"total_params_b": "120", "count_status": "sourced", "sources": "https://example.com/model", "checked_on": "2026-10-02"}
        research = {"required_memory_gib": 300, "weights_gib": 250, "sources": ["https://example.com/model"], "notes": "Full checkpoint with resident lookup tables.", "context_tokens": 108000}
        result = classify(model, research, count, self.policy)
        self.assertEqual(result["hardware_tier"], "small_server")
        self.assertEqual(result["classification_basis"], "memory_adjusted")
        self.assertEqual(result["concurrent_sessions"], 4)
        self.assertGreater(result["required_memory_gib"], 300)

    def test_missing_count_short_context_and_closed_weights(self):
        model = {"canonical_id": "example/model", "open_weights": "true", "context_length": "131072"}
        unknown = classify(model, None, None, self.policy)
        self.assertEqual((unknown["hosting_status"], unknown["hardware_tier"]), ("unknown", ""))
        count = {"total_params_b": "27", "count_status": "sourced", "sources": "https://example.com/model", "checked_on": "2026-10-02"}
        short = classify(dict(model, context_length="32768"), None, count, self.policy)
        self.assertEqual((short["hosting_status"], short["hardware_tier"]), ("unknown", ""))
        closed = classify(dict(model, open_weights="false"), None, count, self.policy)
        self.assertEqual((closed["hosting_status"], closed["hardware_tier"]), ("api_only", ""))
        self.assertEqual(closed["hardware_cost_min_sgd"], "")

    def test_generated_dataset_preserves_requested_examples(self):
        with (HERE / "models.csv").open() as f:
            models = {r["canonical_id"]: r for r in csv.DictReader(f)}
        with (HERE / "model_hardware.csv").open() as f:
            hosting = {r["canonical_id"]: r for r in csv.DictReader(f)}
        self.assertEqual(set(hosting), set(models))
        for key, tier, sessions in [("z-ai/glm-5.3-flash", "large_desktop", "1"),
                                    ("deepseek/deepseek-v4.1-flash", "large_desktop", "1"),
                                    ("moonshotai/kimi-k3", "cluster", "16")]:
            row = hosting[key]
            self.assertEqual((row["hardware_tier"], row["concurrent_sessions"]), (tier, sessions))
        self.assertEqual(float(hosting["moonshotai/kimi-k3"]["total_params_b"]), 2800)
        deepseek = hosting["deepseek/deepseek-v4.1-flash"]
        self.assertEqual(deepseek["classification_basis"], "user_override")
        self.assertEqual(float(deepseek["total_params_b"]), 748)
        self.assertEqual(deepseek["required_memory_gib"], "")
        self.assertIn("disk-backed Engram", deepseek["notes"])


if __name__ == "__main__":
    unittest.main()
