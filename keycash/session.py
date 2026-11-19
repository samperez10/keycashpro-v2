"""Private, atomic cookie storage retaining cookie security metadata."""
import json
import os
import secrets
import tempfile
from http.cookiejar import Cookie
from pathlib import Path

ALLOWED = {"keycashv2.pro", "www.keycashv2.pro"}


class SessionStore:
    def __init__(self, directory):
        self.path = Path(directory).expanduser().resolve() / "session.json"

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["version"] != 1 or not isinstance(data["username"], str):
                raise ValueError
            if not isinstance(data["fingerprint"], str):
                raise ValueError
            cookies = [Cookie(**item) for item in data["cookies"]]
            if any(c.domain.lstrip(".").casefold() not in ALLOWED for c in cookies):
                raise ValueError
            return data, cookies
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(
                "Saved session is invalid; run 'python main.py logout --local' to reset it."
            ) from exc

    def save(self, username, fingerprint, jar):
        cookies = []
        for cookie in jar:
            if cookie.is_expired():
                continue
            item = vars(cookie).copy()
            item["rest"] = item.pop("_rest", {})
            cookies.append(item)
        if not cookies:
            raise RuntimeError("The server did not provide a session cookie.")
        data = {"version": 1, "username": username, "fingerprint": fingerprint, "cookies": cookies}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd, name = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear(self):
        self.path.unlink(missing_ok=True)

    @staticmethod
    def new_fingerprint():
        return secrets.token_hex(32)
