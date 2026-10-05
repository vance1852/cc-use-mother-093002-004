import unittest

from digital_trade_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertTrue(result["governance_authorized"])
        self.assertTrue(result["incident_duplicate_collapsed"])
        self.assertTrue(result["clinical_fact_preserved"])
        self.assertEqual(
            ["reported", "paused", "tracking_started", "review_completed", "recovered"],
            result["incident_actions"])
        self.assertEqual(["patient-1001", "patient-1002"], result["affected_patients"])


if __name__ == "__main__":
    unittest.main()
