import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from keycash.client import KeycashClient
from keycash.proxy import (
    ProxyEntry,
    ProxyPool,
    ProxyUnavailable,
    parse_proxies,
)


class ProxyParsingTests(unittest.TestCase):
    def test_proxy_lines_are_strict_and_deduplicated(self):
        text = """
        # primary
        proxy1.example.com:8000:user1:pass1
        proxy1.example.com:8000:user1:pass1
        proxy2.example.com:9000:user2:pass2
        """
        entries = parse_proxies(text)
        self.assertEqual(
            [entry.endpoint for entry in entries],
            ["proxy1.example.com:8000", "proxy2.example.com:9000"],
        )

    def test_invalid_proxy_line_reports_its_number(self):
        with self.assertRaisesRegex(ValueError, "Line 2"):
            parse_proxies("proxy1.example.com:8000:user:pass\nnot-valid")

    def test_proxy_url_escapes_credentials(self):
        entry = ProxyEntry("proxy.example.com", 8000, "user name", "p@ss")
        self.assertEqual(
            entry.url,
            "http://user%20name:p%40ss@proxy.example.com:8000",
        )


class ProxyPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = ProxyPool(self.root)
        self.pool.save(
            "proxy1.example.com:8000:user1:pass1\n"
            "proxy2.example.com:8000:user2:pass2\n"
        )

    def test_assignments_are_sticky_and_exclusive(self):
        first = self.pool.acquire("account-a", True)
        self.assertEqual(first.endpoint, "proxy1.example.com:8000")
        self.assertEqual(
            self.pool.acquire("account-a", True).endpoint, first.endpoint
        )
        second = self.pool.acquire("account-b", True)
        self.assertEqual(second.endpoint, "proxy2.example.com:8000")
        with self.assertRaisesRegex(ProxyUnavailable, "No available proxy"):
            self.pool.acquire("account-c", True)

    def test_assignments_survive_restart_and_release(self):
        first = self.pool.acquire("account-a", True)
        restarted = ProxyPool(self.root)
        self.assertEqual(
            restarted.acquire("account-a", True).endpoint, first.endpoint
        )
        restarted.release("account-a")
        self.assertEqual(
            restarted.acquire("account-b", True).endpoint, first.endpoint
        )

    def test_proxy_dies_after_three_failures(self):
        entry = self.pool.acquire("account-a", True)
        for _ in range(2):
            self.pool.record(entry, "ready", "timeout", failure=True)
        self.assertEqual(self.pool.acquire("account-a", True).endpoint, entry.endpoint)
        self.pool.record(entry, "ready", "timeout", failure=True)
        snapshot = self.pool.snapshot()
        self.assertEqual(snapshot[0]["status"], "dead")
        replacement = self.pool.acquire("account-a", True)
        self.assertEqual(replacement.endpoint, "proxy2.example.com:8000")

    def test_blocked_proxy_is_released_immediately(self):
        entry = self.pool.acquire("account-a", True)
        self.pool.record(entry, "blocked", "Cloudflare challenge detected")
        snapshot = self.pool.snapshot()
        self.assertEqual(snapshot[0]["status"], "blocked")
        replacement = self.pool.acquire("account-a", True)
        self.assertEqual(replacement.endpoint, "proxy2.example.com:8000")

    def test_snapshot_never_exposes_credentials(self):
        self.pool.acquire("account-a", True)
        encoded = json.dumps(self.pool.snapshot())
        self.assertNotIn("user1", encoded)
        self.assertNotIn("pass1", encoded)
        self.assertIn("proxy1.example.com:8000", encoded)

    def test_disabled_pool_returns_direct(self):
        self.assertIsNone(self.pool.acquire("account-a", False))
        self.assertEqual(self.pool.snapshot()[0]["status"], "ready")

    def test_proxy_can_be_used_by_the_client(self):
        entry = ProxyEntry("proxy.example.com", 8000, "user", "pass")
        with patch("keycash.client.httpx.AsyncClient") as client:
            KeycashClient(self.root, proxy=entry)
            self.assertEqual(client.call_args.kwargs["proxy"], entry.url)
            self.assertTrue(client.call_args.kwargs["trust_env"] is False)


if __name__ == "__main__":
    unittest.main()
