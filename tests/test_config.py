import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mojatools_agent as agent

GOOD = {
    "server_time": "2026-07-26T10:00:00Z",
    "agent_version_latest": "1.0.0",
    "checks": [{
        "check_id": 7, "type": "liveness_full", "interval_seconds": 300,
        "thresholds": {"cpu_percent": 95, "disk_percent": 90},
        "expected_ports": [22, 80], "expected_containers": ["nginx"],
        "evil_extra_key": "rm -rf /",  # must be dropped
    }],
}


class ServerConfigTest(unittest.TestCase):
    def test_good_config(self):
        clean = agent.validate_server_config(GOOD)
        self.assertEqual(clean["checks"][0]["check_id"], 7)
        self.assertNotIn("evil_extra_key", clean["checks"][0])
        self.assertEqual(agent.effective_mode(clean), "full")
        self.assertEqual(agent.effective_interval(clean), 300)

    def test_interval_clamped(self):
        cfg = agent.validate_server_config({"checks": [
            {"check_id": 1, "type": "liveness_light", "interval_seconds": 5}]})
        self.assertEqual(cfg["checks"][0]["interval_seconds"], agent.MIN_INTERVAL_S)

    def test_bad_type_rejected(self):
        with self.assertRaises(ValueError):
            agent.validate_server_config({"checks": [{"check_id": 1, "type": "http"}]})

    def test_bad_ports_rejected(self):
        with self.assertRaises(ValueError):
            agent.validate_server_config({"checks": [
                {"check_id": 1, "type": "liveness_full", "expected_ports": [99999]}]})

    def test_checks_not_list_rejected(self):
        with self.assertRaises(ValueError):
            agent.validate_server_config({"checks": "nope"})


if __name__ == "__main__":
    unittest.main()
