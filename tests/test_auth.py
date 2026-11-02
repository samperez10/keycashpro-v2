import stat
import tempfile
import unittest
import httpx
from keycash.client import AuthenticationError, BASE_URL, KeycashClient

LOGIN_HTML = """<form method='POST' action='./'>
<input name='identifier'><input name='password' type='password'>
<button name='login' value='1'>Enter Game</button></form>"""
HOME_HTML = "<html><a href='?c=logout'>Logout</a><main>Player dashboard</main></html>"


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.authenticated = False
        self.accept = True
        self.posts = 0
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        cookie = request.headers.get("cookie", "")
        if request.method == "POST" and request.url.path == "/":
            self.posts += 1
            self.assertEqual(request.headers["origin"], BASE_URL)
            body = request.content.decode()
            self.assertIn("identifier=tester", body)
            self.assertIn("password=test-password", body)
            if not self.accept:
                return httpx.Response(302, headers={"location": "./?c=login"})
            self.authenticated = True
            return httpx.Response(302, headers={
                "location": "./?c=dashboard",
                "set-cookie": "PHPSESSID=authenticated; Path=/; Max-Age=3600; Domain=keycashv2.pro; HttpOnly; SameSite=Lax",
            })
        if request.url.query == b"c=login":
            return httpx.Response(200, text=LOGIN_HTML, headers={
                "set-cookie": "PHPSESSID=initial; Path=/; Domain=keycashv2.pro; HttpOnly; SameSite=Lax"
            })
        if request.url.query == b"c=dashboard":
            return httpx.Response(200, text=HOME_HTML)
        if request.url.query == b"logout":
            self.authenticated = False
            return httpx.Response(302, headers={"location": "./?c=login"})
        if request.url.path == "/":
            if self.authenticated and "PHPSESSID=authenticated" in cookie:
                return httpx.Response(200, text=HOME_HTML)
            return httpx.Response(302, headers={"location": "./?c=login"})
        raise AssertionError(str(request.url))

    def client(self):
        return KeycashClient(self.temp.name, transport=httpx.MockTransport(self.handler))

    async def test_restart_reuses_cookie_and_retains_metadata(self):
        async with self.client() as client:
            profile = await client.login("tester", "test-password")
            self.assertTrue(profile["authenticated"])
            fingerprint = client.fingerprint
            path = client.store.path
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("test-password", path.read_text())
        async with self.client() as client:
            profile = await client.ensure_session("tester")
            self.assertTrue(profile["reused_session"])
            self.assertEqual(client.fingerprint, fingerprint)
            cookie = list(client.http.cookies.jar)[0]
            self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
            self.assertEqual(cookie.path, "/")
        self.assertEqual(self.posts, 1)

    async def test_invalid_login_is_not_saved_or_echoed(self):
        self.accept = False
        async with self.client() as client:
            with self.assertRaises(AuthenticationError) as error:
                await client.login("tester", "test-password")
            self.assertNotIn("test-password", str(error.exception))
            self.assertFalse(client.store.path.exists())

    async def test_cross_account_reuse_is_rejected_before_network(self):
        async with self.client() as client:
            await client.login("tester", "test-password")
        count = len(self.requests)
        async with self.client() as client:
            with self.assertRaises(AuthenticationError):
                await client.restore("someone-else")
        self.assertEqual(len(self.requests), count)

    async def test_foreign_redirect_is_rejected(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "https://example.org/login"})
        )
        async with KeycashClient(self.temp.name, transport=transport) as client:
            client.username = "tester"
            with self.assertRaisesRegex(AuthenticationError, "unexpected site"):
                await client.status()

    async def test_local_logout_never_contacts_server(self):
        async with self.client() as client:
            await client.login("tester", "test-password")
        count = len(self.requests)
        async with self.client() as client:
            await client.logout(local=True)
            self.assertFalse(client.store.path.exists())
        self.assertEqual(len(self.requests), count)
