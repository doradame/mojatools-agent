import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mojatools_agent as agent


class PostWithRetriesTest(unittest.TestCase):
    def setUp(self):
        # keep tests fast: no real backoff sleeping
        sleep_patcher = mock.patch.object(agent.time, "sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_succeeds_on_third_attempt(self):
        with mock.patch.object(agent, "http_post") as post:
            post.side_effect = [
                urllib.error.URLError("conn refused"),
                urllib.error.URLError("conn refused"),
                (200, {"ok": True}),
            ]
            result = agent.post_with_retries("https://example.test", "/agent/v1/push",
                                             {"k": "v"}, token="t")
        self.assertEqual(result, (200, {"ok": True}))
        self.assertEqual(post.call_count, 3)

    def test_http_error_not_retried(self):
        err = urllib.error.HTTPError("https://example.test", 403, "Forbidden",
                                     {}, io.BytesIO(b"forbidden"))
        with mock.patch.object(agent, "http_post") as post:
            post.side_effect = err
            with self.assertRaises(RuntimeError) as ctx:
                agent.post_with_retries("https://example.test", "/agent/v1/push",
                                        {"k": "v"}, token="t")
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(post.call_count, 1)

    def test_network_error_exhausts_retries(self):
        with mock.patch.object(agent, "http_post") as post:
            post.side_effect = urllib.error.URLError("down")
            with self.assertRaises(RuntimeError) as ctx:
                agent.post_with_retries("https://example.test", "/agent/v1/push",
                                        {"k": "v"}, token="t")
        self.assertIn("failed after retries", str(ctx.exception))
        # initial attempt + len(RETRY_DELAYS) retries
        self.assertEqual(post.call_count, 1 + len(agent.RETRY_DELAYS))


class HttpPostGuardsTest(unittest.TestCase):
    def test_payload_size_guard(self):
        # checked before any network activity (after the https:// check)
        big = {"data": "x" * (agent.MAX_BODY_BYTES + 1)}
        with self.assertRaises(ValueError):
            agent.http_post("https://example.test", "/agent/v1/push", big)

    def test_https_required(self):
        with self.assertRaises(ValueError):
            agent.http_post("http://example.test", "/agent/v1/push", {})


if __name__ == "__main__":
    unittest.main()
