"""Persistent, exclusive proxy assignments for Keycash account traffic."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHECK_PATH = "/?c=login"


class ProxyError(RuntimeError):
    pass


class ProxyUnavailable(ProxyError):
    pass


class ProxyChanged(ProxyError):
    """The current route failed and the operation should be restarted."""

    def __init__(self, message, status="ready", reason=""):
        super().__init__(message)
        self.status = status
        self.reason = reason or message


@dataclass(frozen=True)
class ProxyEntry:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def line(self):
        return f"{self.host}:{self.port}:{self.username}:{self.password}"

    @property
    def key(self):
        return hashlib.sha256(self.line.encode()).hexdigest()

    @property
    def endpoint(self):
        return f"{self.host}:{self.port}"

    @property
    def url(self):
        return (
            f"http://{quote(self.username, safe='')}:"
            f"{quote(self.password, safe='')}@{self.endpoint}"
        )


def parse_proxies(text):
    entries, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"Line {number}: expected host:port:username:password")
        host, port, username, password = parts
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
            or not port.isascii()
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError(f"Line {number}: invalid host or port")
        if not username or not password or any(ord(c) < 32 for c in username + password):
            raise ValueError(f"Line {number}: username and password are required")
        entry = ProxyEntry(host.lower(), int(port), username, password)
        if entry.key not in seen:
            seen.add(entry.key)
            entries.append(entry)
    return entries


def cloudflare_challenge(response):
    text = response.content[:65536].decode("utf-8", "ignore").lower()
    return response.headers.get("cf-mitigated", "").lower() == "challenge" or (
        "just a moment" in text and ("cf-chl" in text or "challenge-platform" in text)
    )


def login_page(response):
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    return bool(
        form
        and form.find("input", attrs={"name": "identifier"})
        and form.find("input", attrs={"name": "password"})
    )


def atomic_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            try:
                os.fchmod(stream.fileno(), 0o600)
            except (AttributeError, OSError):
                pass
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class ProxyPool:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "proxies.txt"
        self.state_path = self.root / "proxy_state.json"
        self.dead_path = self.root / "dead_proxies.txt"
        self._mutex = threading.RLock()

    def text(self):
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._mutex:
            if fcntl is None:
                yield
                return
            fd = os.open(self.root / ".proxy-lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield

    def read_state(self):
        if not self.state_path.exists():
            return {"assignments": {}, "proxies": {}}
        try:
            state = json.loads(self.state_path.read_text())
            if not isinstance(state.get("assignments"), dict) or not isinstance(
                state.get("proxies"), dict
            ):
                raise ValueError()
            if any(
                not isinstance(account, str) or not isinstance(key, str)
                for account, key in state["assignments"].items()
            ):
                raise ValueError()
            for key, record in state["proxies"].items():
                if (
                    not isinstance(key, str)
                    or not isinstance(record, dict)
                    or record.get("status") not in ("ready", "dead", "blocked")
                ):
                    raise ValueError()
                if (
                    not isinstance(record.get("reason", ""), str)
                    or not isinstance(record.get("failures", 0), int)
                    or record.get("failures", 0) < 0
                ):
                    raise ValueError()
            return state
        except (OSError, ValueError, AttributeError):
            raise ProxyUnavailable(
                "Proxy state cannot be read; restore or remove proxy_state.json"
            ) from None

    def read(self):
        try:
            entries = parse_proxies(self.text())
        except ValueError as exc:
            raise ProxyUnavailable(f"Proxy list needs correction: {exc}") from None
        return entries, self.read_state()

    def write(self, entries, state):
        atomic_private(self.state_path, json.dumps(state, indent=2) + "\n")
        dead = []
        for entry in entries:
            record = state["proxies"].get(entry.key, {})
            if record.get("status") == "dead":
                dead.extend(
                    [
                        f"# {record.get('checked_at', '')} | "
                        f"{record.get('reason', 'Unavailable')}",
                        entry.line,
                    ]
                )
        atomic_private(self.dead_path, "\n".join(dead) + ("\n" if dead else ""))

    def save(self, text):
        entries = parse_proxies(text)
        with self.locked():
            state = self.read_state()
            keys = {entry.key for entry in entries}
            state["assignments"] = {
                account: key
                for account, key in state["assignments"].items()
                if key in keys
            }
            state["proxies"] = {
                key: value for key, value in state["proxies"].items() if key in keys
            }
            atomic_private(self.path, "".join(entry.line + "\n" for entry in entries))
            self.write(entries, state)
        return len(entries)

    def release(self, account_id):
        if not self.state_path.exists():
            return
        with self.locked():
            entries, state = self.read()
            state["assignments"].pop(str(account_id), None)
            self.write(entries, state)

    def release_all(self):
        if not self.state_path.exists():
            return
        with self.locked():
            entries, state = self.read()
            state["assignments"] = {}
            self.write(entries, state)

    def acquire(self, account_id, enabled=False):
        if not enabled:
            return None
        account_id = str(account_id)
        with self.locked():
            entries, state = self.read()
            by_key = {entry.key: entry for entry in entries}
            assignments = state["assignments"]
            assignments = {
                account: key
                for account, key in assignments.items()
                if key in by_key
                and state["proxies"].get(key, {}).get("status", "ready") == "ready"
            }
            state["assignments"] = assignments
            key = assignments.get(account_id)
            if key:
                return by_key[key]
            used = set(assignments.values())
            for entry in entries:
                if (
                    entry.key not in used
                    and state["proxies"].get(entry.key, {}).get("status", "ready")
                    == "ready"
                ):
                    assignments[account_id] = entry.key
                    self.write(entries, state)
                    return entry
            raise ProxyUnavailable("No available proxy")

    def endpoint_for(self, account_id):
        if not self.state_path.exists():
            return ""
        try:
            entries, state = self.read()
        except ProxyUnavailable:
            return ""
        by_key = {entry.key: entry for entry in entries}
        entry = by_key.get(state["assignments"].get(str(account_id)))
        return entry.endpoint if entry else ""

    def record(self, entry, status, reason="", failure=False):
        with self.locked():
            entries, state = self.read()
            if entry.key not in {item.key for item in entries}:
                return
            previous = state["proxies"].get(entry.key, {})
            count = previous.get("failures", 0) + 1 if failure else 0
            if failure:
                status = "dead" if count >= 3 else previous.get("status", "ready")
            state["proxies"][entry.key] = {
                "status": status,
                "reason": reason if status != "ready" else "",
                "failures": count,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if status != "ready":
                state["assignments"] = {
                    account: key
                    for account, key in state["assignments"].items()
                    if key != entry.key
                }
            self.write(entries, state)

    def record_temporary(self, entry, reason):
        with self.locked():
            entries, state = self.read()
            if entry.key not in {item.key for item in entries}:
                return
            record = state["proxies"].setdefault(
                entry.key, {"status": "ready", "failures": 0}
            )
            record["reason"] = reason
            record["checked_at"] = datetime.now(timezone.utc).isoformat()
            self.write(entries, state)

    def success(self, entry):
        with self.locked():
            entries, state = self.read()
            record = state["proxies"].get(entry.key)
            if record and (
                (record.get("status") == "ready" and record.get("failures"))
                or record.get("reason")
            ):
                record.update(status="ready", reason="", failures=0)
                record["checked_at"] = datetime.now(timezone.utc).isoformat()
                self.write(entries, state)

    def snapshot(self):
        if not self.path.exists():
            return []
        with self.locked():
            entries, state = self.read()
            reverse = {
                key: account for account, key in state["assignments"].items()
            }
            return [
                {
                    "endpoint": entry.endpoint,
                    "status": "assigned"
                    if entry.key in reverse
                    else state["proxies"].get(entry.key, {}).get("status", "ready"),
                    "reason": state["proxies"].get(entry.key, {}).get("reason", ""),
                    "account_id": reverse.get(entry.key, ""),
                }
                for entry in entries
            ]

    async def check(self, base_url, callback=None):
        with self.locked():
            entries, _ = self.read()
        semaphore = asyncio.Semaphore(3)

        async def inspect(entry):
            async with semaphore:
                for attempt in range(3):
                    try:
                        async with httpx.AsyncClient(
                            proxy=entry.url,
                            trust_env=False,
                            headers={"User-Agent": USER_AGENT},
                            follow_redirects=True,
                            timeout=12.0,
                        ) as client:
                            response = await client.get(
                                base_url.rstrip("/") + CHECK_PATH
                            )
                        if cloudflare_challenge(response):
                            self.record(
                                entry, "blocked", "Cloudflare challenge detected"
                            )
                        elif response.status_code == 407:
                            self.record(entry, "dead", "Proxy authentication rejected")
                        elif response.status_code == 200 and login_page(response):
                            self.record(entry, "ready")
                        else:
                            self.record_temporary(
                                entry,
                                f"Keycash check returned HTTP {response.status_code}; "
                                "retry later",
                            )
                        break
                    except httpx.ProxyError as exc:
                        if "407" in str(exc):
                            self.record(entry, "dead", "Proxy authentication rejected")
                            break
                        if "502" in str(exc) or "503" in str(exc):
                            self.record_temporary(
                                entry, "Proxy connection temporarily unavailable"
                            )
                            break
                        if attempt == 2:
                            self.record(
                                entry, "dead", "Proxy connection failed after 3 attempts"
                            )
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                            self.record_temporary(
                                entry, "TLS certificate verification failed; retry later"
                            )
                            break
                        if attempt == 2:
                            self.record(
                                entry, "dead", "Proxy connection failed after 3 attempts"
                            )
                    except httpx.HTTPError:
                        self.record_temporary(entry, "Keycash request failed; retry later")
                        break
                    await asyncio.sleep(0.5)
                if callback:
                    callback()

        checks = [asyncio.create_task(inspect(entry)) for entry in entries]
        try:
            await asyncio.gather(*checks)
        finally:
            for task in checks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*checks, return_exceptions=True)
