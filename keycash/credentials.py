"""Private local storage for a verified account password."""
import json
import os
import tempfile
from pathlib import Path


class CredentialStore:
    def __init__(self, directory):
        self.path = Path(directory).expanduser().resolve() / "credentials.json"

    def load(self, username):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["version"] != 1 or data["username"].casefold() != username.casefold():
                raise ValueError
            password = data["password"]
            if not isinstance(password, str) or not password:
                raise ValueError
            return password
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("Saved credentials are invalid; forget and save them again.") from exc

    def save(self, username, password):
        if not password:
            raise ValueError("Password cannot be blank")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd, name = tempfile.mkstemp(prefix=".credentials-", dir=self.path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "username": username, "password": password},
                    stream,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear(self):
        self.path.unlink(missing_ok=True)
