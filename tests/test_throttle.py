import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mojatools_agent as agent


class ThrottleTest(unittest.TestCase):
    def test_first_run_pushes(self):
        self.assertTrue(agent.should_push({}, 300))

    def test_recent_push_skips(self):
        now = agent.utcnow()
        state = {"last_push_at": (now - timedelta(seconds=60)).isoformat()}
        self.assertFalse(agent.should_push(state, 300, now=now))

    def test_due_push_runs(self):
        now = agent.utcnow()
        state = {"last_push_at": (now - timedelta(seconds=600)).isoformat()}
        self.assertTrue(agent.should_push(state, 300, now=now))

    def test_corrupt_timestamp_pushes(self):
        self.assertTrue(agent.should_push({"last_push_at": "garbage"}, 300))

    def test_non_string_timestamp_pushes(self):
        self.assertTrue(agent.should_push({"last_push_at": 12345}, 300))


if __name__ == "__main__":
    unittest.main()
