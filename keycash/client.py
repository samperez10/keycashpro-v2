"""Keycash V2 Pro form login with verified, persistent cookie sessions."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit
import httpx
from bs4 import BeautifulSoup
from .session import SessionStore

BASE_URL = "https://keycashv2.pro"
LOGIN_PATH = "/?c=login"


class AuthenticationError(RuntimeError):
    pass


def parse_wallet(html):
    """Read the wallet shown in the page header, or None if it is absent."""
    tag = BeautifulSoup(html, "html.parser").find(id="header-wallet")
    match = re.search(r"\d[\d,]*(?:\.\d+)?", tag.get_text()) if tag else None
    return match.group().replace(",", "") if match else None


class KeycashClient:
    def __init__(self, state_dir, *, transport=None):
        self.store = SessionStore(state_dir)
        self.username = None
        self.fingerprint = self.store.new_fingerprint()
        self.reused_session = False
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "User-Agent": "KeycashV2ProSession/0.1 (Python HTTP client)",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )

    async def __aenter__(self):
        try:
            saved = self.store.load()
            if saved:
                data, cookies = saved
                self.username = data["username"]
                self.fingerprint = data["fingerprint"]
                for cookie in cookies:
                    if not cookie.is_expired():
                        self.http.cookies.jar.set_cookie(cookie)
            return self
        except BaseException:
            await self.http.aclose()
            raise

    async def __aexit__(self, *_):
        await self.http.aclose()

    def save(self):
        self.store.save(self.username, self.fingerprint, self.http.cookies.jar)

    def account_matches(self, username):
        if self.username and self.username.casefold() != username.strip().casefold():
            raise AuthenticationError(
                "Saved session belongs to another account; use a separate account directory."
            )

    @staticmethod
    def redirect_target(response):
        if not response.is_redirect:
            return None
        target = urlsplit(urljoin(str(response.request.url), response.headers.get("location", "")))
        if target.scheme != "https" or target.hostname not in {"keycashv2.pro", "www.keycashv2.pro"}:
            raise AuthenticationError("Keycash redirected to an unexpected site; request stopped.")
        return target.geturl()

    @staticmethod
    def is_login(url):
        return parse_qs(urlsplit(str(url)).query).get("c") == ["login"]

    @staticmethod
    def has_login_form(html):
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form")
        return bool(
            form
            and form.find("input", attrs={"name": "identifier"})
            and form.find("input", attrs={"name": "password"})
        )

    async def follow(self, response, limit=5):
        for _ in range(limit):
            target = self.redirect_target(response)
            if target is None:
                return response
            response = await self.http.get(target, headers={"Cache-Control": "no-store"})
        raise AuthenticationError("Keycash returned too many redirects.")

    async def status(self, *, persist=True):
        if not self.username:
            return None
        response = await self.http.get("/", headers={"Cache-Control": "no-store"})
        if response.is_redirect and self.is_login(self.redirect_target(response)):
            return None
        response = await self.follow(response)
        if response.status_code in (401, 403) or self.is_login(response.url):
            return None
        response.raise_for_status()
        if self.has_login_form(response.text):
            return None
        if persist:
            self.save()
        return {
            "username": self.username,
            "authenticated": True,
            "reused_session": self.reused_session,
            "page": str(response.url),
            "wallet": parse_wallet(response.text),
        }

    async def restore(self, username=None):
        self.reused_session = False
        if username:
            self.account_matches(username)
        profile = await self.status()
        if profile:
            self.reused_session = True
            profile["reused_session"] = True
        return profile

    async def login(self, username, password):
        username = username.strip()
        if not username or not password:
            raise AuthenticationError("Username and password are required.")
        self.account_matches(username)
        self.reused_session = False
        self.http.cookies.clear()

        page = await self.follow(
            await self.http.get(LOGIN_PATH, headers={"Cache-Control": "no-store"})
        )
        page.raise_for_status()
        if not self.has_login_form(page.text):
            raise AuthenticationError("Keycash login form was not found; the site may have changed.")

        response = await self.follow(
            await self.http.post(
                "/",
                data={"identifier": username, "password": password, "login": "1"},
                headers={"Origin": BASE_URL, "Referer": BASE_URL + LOGIN_PATH},
            )
        )
        if (
            response.status_code in (401, 403)
            or self.is_login(response.url)
            or self.has_login_form(response.text)
        ):
            raise AuthenticationError(
                "Login rejected. Check the credentials or sign in on the website for details."
            )
        response.raise_for_status()
        self.username = username
        profile = await self.status(persist=False)
        if not profile:
            self.username = None
            raise AuthenticationError(
                "Login response was accepted, but the session could not be verified."
            )
        self.save()
        return profile

    async def ensure_session(self, username, password=None):
        profile = await self.restore(username)
        if profile:
            return profile
        if password:
            return await self.login(username, password)
        return None

    async def logout(self, *, local=False):
        try:
            if not local and self.username:
                response = await self.http.get(
                    "/?logout", headers={"Referer": BASE_URL + "/?c=account"}
                )
                await self.follow(response)
        finally:
            self.http.cookies.clear()
            self.store.clear()
            self.username = None
            self.reused_session = False
