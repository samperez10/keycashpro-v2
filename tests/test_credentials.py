import stat
import tempfile
import unittest
from keycash.credentials import CredentialStore


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CredentialStore(self.temp.name)

    def test_verified_password_storage_is_private_and_account_bound(self):
        self.store.save("tester", "test-password")
        self.assertEqual(self.store.load("tester"), "test-password")
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        with self.assertRaises(RuntimeError):
            self.store.load("someone-else")

    def test_clear_forgets_password(self):
        self.store.save("tester", "test-password")
        self.store.clear()
        self.assertIsNone(self.store.load("tester"))
