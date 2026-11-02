"""Service behind the terminal interface. Saved passwords never enter snapshots."""
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

import httpx

from . import engine
from .client import AuthenticationError, KeycashClient
from .credentials import CredentialStore
from .engine import Control, Job
from .games import GameError, RateLimited, solve_math_problem
from .locking import AccountBusy, account_lock as shared_account_lock
from .pacing import DEFAULT_PACING, Pacer, validate_pacing
from .session import SessionStore

TASKS = {"typing": "Typing", "math": "Math", "emoji": "Emoji"}
CYCLE_SIZES = {"typing": 5, "math": 5, "emoji": 8}
DEFAULTS = {
    **DEFAULT_PACING,
    "solve_delay_min": 6,
    "solve_delay_max": 10,
    "resume_time": "08:00",
    "network_retries": 3,
    "retry_delay": 5,
    "on_unknown": "stop",
    "theme": "keycash",
    "reduced_motion": False,
}
MANILA = timezone(timedelta(hours=8), "Asia/Manila")
UNKNOWN = ("unknown", "submission_outcome_unknown")


class UserError(ValueError):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def account_id(username):
    return hashlib.sha256(username.strip().casefold().encode()).hexdigest()[:20]


def cycle_window(resume_time, now=None):
    local = (now or now_utc()).astimezone(MANILA)
    hour, minute = map(int, resume_time.split(":"))
    start = datetime.combine(local.date(), day_time(hour, minute), MANILA)
    if local < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def target_value(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise UserError("Daily target: enter a peso amount") from None
    if not amount.is_finite() or amount < 0:
        raise UserError("Daily target must be zero or greater")
    return str(amount)


def number(value, name, minimum, maximum, integer=False):
    try:
        result = int(str(value)) if integer else float(str(value))
    except (ValueError, TypeError):
        raise UserError(f"{name}: enter a number") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise UserError(f"{name}: use {minimum} to {maximum}")
    return result


def friendly(exc):
    if isinstance(exc, (UserError, AuthenticationError, GameError, AccountBusy)):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "Keycash is rate limiting; try again in a few minutes"
    return type(exc).__name__ + "; check your connection and account details"


@contextmanager
def account_lock(state):
    try:
        with shared_account_lock(state):
            yield
    except AccountBusy as exc:
        raise UserError(str(exc)) from None


class Service:
    def __init__(self, bridge, root, data, client_factory=KeycashClient):
        self.bridge = bridge
        self.root = Path(root)
        self.data = Path(data)
        self.client_factory = client_factory
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data / "keycash.sqlite3"
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                task TEXT NOT NULL, enabled INTEGER NOT NULL,
                daily_target TEXT NOT NULL, position INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS awards (
                account_id TEXT NOT NULL, run_id TEXT NOT NULL, challenge INTEGER NOT NULL,
                awarded_at TEXT NOT NULL, points TEXT NOT NULL,
                PRIMARY KEY (account_id, run_id, challenge));
            CREATE INDEX IF NOT EXISTS awards_cycle ON awards (account_id, awarded_at);
            CREATE TABLE IF NOT EXISTS schedule (
                account_id TEXT PRIMARY KEY, mode TEXT NOT NULL, task TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS balances (
                account_id TEXT PRIMARY KEY, amount TEXT NOT NULL,
                currency TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outcomes (
                account_id TEXT NOT NULL, username TEXT NOT NULL, run_id TEXT NOT NULL,
                challenge INTEGER NOT NULL, task TEXT NOT NULL, verdict TEXT NOT NULL,
                decided_at TEXT NOT NULL, points TEXT, answer TEXT,
                PRIMARY KEY (account_id, run_id, challenge));
            CREATE INDEX IF NOT EXISTS outcomes_account_date ON outcomes (account_id, decided_at);
            """
        )
        outcome_columns = {row[1] for row in self.db.execute("PRAGMA table_info(outcomes)")}
        if "answer" not in outcome_columns:
            with self.db:
                self.db.execute("ALTER TABLE outcomes ADD COLUMN answer TEXT")
        self.settings = dict(DEFAULTS)
        self.settings.update(
            {key: json.loads(value) for key, value in self.db.execute("SELECT key, value FROM settings")}
        )
        self.validate_settings(self.settings)
        self.accounts = []
        columns = ("id", "username", "task", "enabled", "daily_target")
        rows = self.db.execute(
            "SELECT id, username, task, enabled, daily_target FROM accounts ORDER BY position"
        )
        for row in rows:
            account = dict(zip(columns, row))
            account["enabled"] = bool(account["enabled"])
            self.accounts.append(account)
        self.jobs = {}
        self.states = {}
        self.busy = set()
        self.pending_accounts = set()
        self.logs = []
        self.commands = set()
        self.closing = False
        self.last_snapshot = ""
        self.last_scan = 0
        self.last_schedule = 0
        self.history = []
        self.balance_errors = {}
        self.import_saved_accounts()
        self.save()
        with self.db:
            self.db.execute("UPDATE schedule SET mode='stopped' WHERE mode IN ('running','waiting')")

    def import_saved_accounts(self):
        """Show accounts already signed in through main.py."""
        for folder in sorted((self.data / "accounts").glob("*")):
            if any(a["id"] == folder.name for a in self.accounts):
                continue
            try:
                saved = SessionStore(folder).load()
            except RuntimeError:
                continue
            if saved and account_id(saved[0]["username"]) == folder.name:
                self.accounts.append(
                    {
                        "id": folder.name,
                        "username": saved[0]["username"],
                        "task": "typing",
                        "enabled": True,
                        "daily_target": "0",
                    }
                )

    def emit(self, data):
        self.bridge.emit(json.dumps(data, ensure_ascii=True))

    def event(self, message):
        self.logs.append(datetime.now().strftime("%H:%M:%S") + "  " + message)
        self.logs = self.logs[-60:]
        try:
            log = self.data / "events.log"
            if log.exists() and log.stat().st_size > 1_000_000:
                log.replace(self.data / "events.log.1")
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(datetime.now().isoformat(timespec="seconds") + "  " + message + "\n")
        except OSError:
            pass

    def save(self):
        with self.db:
            self.db.execute("DELETE FROM settings")
            self.db.executemany(
                "INSERT INTO settings VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in self.settings.items()],
            )
            self.db.execute("DELETE FROM accounts")
            self.db.executemany(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (a["id"], a["username"], a["task"], int(a["enabled"]), a["daily_target"], position)
                    for position, a in enumerate(self.accounts)
                ],
            )

    def validate_settings(self, settings):
        try:
            validate_pacing(settings)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        settings["network_retries"] = number(settings["network_retries"], "Network retries", 0, 10, True)
        settings["retry_delay"] = number(settings["retry_delay"], "Retry delay", 0, 60)
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", str(settings["resume_time"])):
            raise UserError("Resume time: use HH:MM, from 00:00 to 23:59 (Asia/Manila)")
        if settings["on_unknown"] not in ("stop", "continue"):
            raise UserError("Unknown handling must be stop or continue")
        if not re.fullmatch(r"[a-z0-9-]{1,40}", settings["theme"]):
            raise UserError("Invalid theme name")

    def account(self, uid):
        for account in self.accounts:
            if account["id"] == uid:
                return account
        raise UserError("Select an account first")

    def state_dir(self, account):
        return self.data / "accounts" / account["id"]

    def credentials(self, account):
        return CredentialStore(self.state_dir(account))

    def password(self, account):
        try:
            return self.credentials(account).load(account["username"])
        except RuntimeError:
            return None

    def session_saved(self, account):
        return (self.state_dir(account) / "session.json").exists()

    def is_active(self, uid):
        return uid in self.jobs and not self.jobs[uid][0].done()

    def schedule_mode(self, uid):
        row = self.db.execute("SELECT mode FROM schedule WHERE account_id=?", (uid,)).fetchone()
        return row[0] if row else "stopped"

    def set_schedule(self, uid, mode, task=None):
        previous = self.db.execute("SELECT task FROM schedule WHERE account_id=?", (uid,)).fetchone()
        task = task or (previous[0] if previous else "account")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO schedule VALUES (?, ?, ?)", (uid, mode, task))

    def progress(self, account, now=None):
        start, end = cycle_window(self.settings["resume_time"], now)
        rows = self.db.execute(
            "SELECT points FROM awards WHERE account_id=? AND awarded_at>=? AND awarded_at<?",
            (account["id"], start.isoformat(), end.isoformat()),
        )
        total = sum((Decimal(row[0]) for row in rows), Decimal(0))
        target = Decimal(account.get("daily_target", "0"))
        return {
            "daily_points": str(total),
            "cycle_start": start.isoformat(),
            "next_resume": end.astimezone(MANILA).strftime("%b %d %H:%M"),
            "target_reached": target > 0 and total >= target,
        }

    def cache_balance(self, account, amount):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO balances VALUES (?, ?, ?, ?)",
                (account["id"], str(amount), "PHP", now_utc().isoformat()),
            )
        self.balance_errors.pop(account["id"], None)

    def cache_profile_balance(self, account, profile):
        if profile and profile.get("wallet"):
            self.cache_balance(account, profile["wallet"])
        else:
            self.balance_errors[account["id"]] = "Wallet refresh unavailable"

    def record_outcome(self, account, run_id, task, row):
        amount = row.get("earned_points") if row["verdict"] == "correct" else None
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO outcomes "
                "(account_id, username, run_id, challenge, task, verdict, decided_at, points, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account["id"],
                    account["username"],
                    run_id,
                    row["index"],
                    task,
                    row["verdict"],
                    row["decided_at"],
                    amount,
                    row.get("answer"),
                ),
            )

    def recent_solves(self, uid):
        rows = self.db.execute(
            "SELECT challenge, task, verdict, decided_at, points, answer FROM outcomes "
            "WHERE account_id=? ORDER BY decided_at DESC, rowid DESC LIMIT 5",
            (uid,),
        ).fetchall()
        return [
            {
                "index": index,
                "task": task,
                "verdict": verdict,
                "decided_at": stamp,
                "time_local": datetime.fromisoformat(stamp).astimezone(MANILA).strftime("%H:%M:%S"),
                "earned_points": amount,
                "answer": answer,
            }
            for index, task, verdict, stamp, amount, answer in rows
        ]

    def scan_history(self):
        if time.monotonic() - self.last_scan < 3:
            return
        self.last_scan = time.monotonic()
        identities = {a["id"]: a["username"] for a in self.accounts}
        for uid, name in self.db.execute("SELECT DISTINCT account_id, username FROM outcomes"):
            identities.setdefault(uid, name)
        start, _ = cycle_window(self.settings["resume_time"])
        week = (start - timedelta(days=6)).isoformat()

        def stats(rows):
            correct = sum(r["verdict"] == "correct" for r in rows)
            wrong = sum(r["verdict"] == "incorrect" for r in rows)
            return {
                "correct": correct,
                "incorrect": wrong,
                "decided": correct + wrong,
                "unknown": sum(r["verdict"] in UNKNOWN for r in rows),
                "points": str(
                    sum((Decimal(r["points"]) for r in rows if r["points"] is not None), Decimal(0))
                ),
            }

        items = []
        fields = ("run_id", "task", "verdict", "decided_at", "points")
        for uid, username in identities.items():
            records = [
                dict(zip(fields, row))
                for row in self.db.execute(
                    "SELECT run_id, task, verdict, decided_at, points FROM outcomes "
                    "WHERE account_id=? ORDER BY decided_at",
                    (uid,),
                )
            ]
            tasks = {}
            for task in ("all", *TASKS):
                chosen = [r for r in records if task == "all" or r["task"] == task]
                tasks[task] = {
                    "all": stats(chosen),
                    "today": stats([r for r in chosen if r["decided_at"] >= start.isoformat()]),
                    "week": stats([r for r in chosen if r["decided_at"] >= week]),
                    "days": [],
                    "recent": [
                        int(r["verdict"] == "correct")
                        for r in chosen
                        if r["verdict"] in ("correct", "incorrect")
                    ][-20:],
                }
            runs = []
            for run_id in dict.fromkeys(r["run_id"] for r in records):
                chosen = [r for r in records if r["run_id"] == run_id]
                totals = stats(chosen)
                runs.append(
                    {
                        "report_path": run_id,
                        "username": username,
                        "task": chosen[0]["task"],
                        "started_at": chosen[0]["decided_at"],
                        "recorded_challenges": len(chosen),
                        "decided_submissions": totals["decided"],
                        "verdicts": {
                            "correct": totals["correct"],
                            "incorrect": totals["incorrect"],
                            "unknown": totals["unknown"],
                        },
                        "earned_points_total": totals["points"],
                    }
                )
            items.append(
                {
                    "id": uid,
                    "username": username,
                    "tasks": tasks,
                    "runs": sorted(runs, key=lambda r: r["started_at"], reverse=True),
                    "legacy_timing": False,
                    "summary_only_runs": 0,
                }
            )
        self.history = items

    def clear_history(self, uid):
        known = any(a["id"] == uid for a in self.accounts) or self.db.execute(
            "SELECT 1 FROM outcomes WHERE account_id=?", (uid,)
        ).fetchone()
        if not known:
            raise UserError("Select an account history first")
        with self.db:
            self.db.execute("DELETE FROM outcomes WHERE account_id=?", (uid,))
        self.last_scan = 0
        self.event("Account history cleared; daily earnings retained")

    def action_flags(self, account):
        uid = account["id"]
        active = self.is_active(uid)
        busy = uid in self.busy
        mode = self.schedule_mode(uid)
        stopping = self.states.get(uid, {}).get("status") == "Stopping"
        paused = mode == "paused"
        armed = mode in ("running", "waiting", "paused")
        login = bool(self.session_saved(account) or self.password(account))
        return {
            "start": not (active or busy or paused or mode == "waiting") and login,
            "pause": not busy and not stopping and not paused and (active or armed),
            "resume": not busy and not stopping and paused,
            "stop": not busy and not stopping and (active or armed),
            "edit": True,
            "remove": not busy,
            "check": not (active or busy) and bool(self.password(account)),
            "refresh_balance": not busy,
        }

    def snapshot(self, force=False):
        self.scan_history()
        accounts = []
        for account in self.accounts:
            uid = account["id"]
            mode = self.schedule_mode(uid)
            saved = self.session_saved(account)
            state = dict(self.states.get(uid, {}))
            state.setdefault("status", "Ready" if saved else "Login needed")
            if uid in self.busy:
                state["status"] = "Checking session"
            state.update(self.progress(account))
            state["schedule_mode"] = mode
            state["scheduled"] = mode in ("running", "waiting", "paused")
            if mode == "waiting":
                state["status"] = "Target reached"
            elif mode == "paused" and not self.is_active(uid):
                state["status"] = "Paused"
            balance = self.db.execute(
                "SELECT amount, currency, updated_at FROM balances WHERE account_id=?", (uid,)
            ).fetchone()
            state["balance"] = None
            if balance:
                updated = datetime.fromisoformat(balance[2])
                state["balance"] = {
                    "amount": balance[0],
                    "currency": balance[1],
                    "updated_at": balance[2],
                    "checked_local": updated.astimezone(MANILA).strftime("%H:%M"),
                    "stale": uid in self.balance_errors,
                }
            state["balance_error"] = self.balance_errors.get(uid, "")
            state["actions"] = self.action_flags(account)
            state["recent_solves"] = self.recent_solves(uid)
            state.setdefault("batch_points", None)
            state.setdefault("batch_size", CYCLE_SIZES[state.get("task", account["task"])])
            state.setdefault("activity", "")
            state.setdefault("verification_steps", 0)
            state["remaining"] = max(0, math.ceil(state.get("wait_until", 0) - time.time()))
            accounts.append(
                {
                    **account,
                    "session_saved": saved,
                    "active": self.is_active(uid),
                    "busy": uid in self.busy,
                    "login_saved": bool(self.password(account)),
                    **state,
                }
            )
        text = json.dumps(
            {
                "type": "snapshot",
                "settings": self.settings,
                "accounts": accounts,
                "history": self.history,
                "events": self.logs,
                "closing": self.closing,
            },
            ensure_ascii=True,
        )
        if force or text != self.last_snapshot:
            self.bridge.emit(text)
            self.last_snapshot = text

    async def login(self, account, password=None):
        uid = account["id"]
        if self.is_active(uid) or uid in self.busy:
            raise UserError("Stop this account before changing its session")
        self.busy.add(uid)
        self.snapshot()
        try:
            with account_lock(self.state_dir(account)):
                async with self.client_factory(self.state_dir(account)) as client:
                    password = password or self.password(account)
                    if not password:
                        raise UserError("Edit this account to save a password")
                    profile = await client.login(account["username"], password)
                    self.credentials(account).save(account["username"], password)
                    self.cache_profile_balance(account, profile)
            self.states[uid] = {"status": "Session verified"}
            self.event(account["username"] + ": session verified")
        except Exception as exc:
            self.states[uid] = {"status": "Login needed", "error": friendly(exc)}
            raise
        finally:
            self.busy.discard(uid)

    async def refresh_balance(self, account):
        uid = account["id"]
        if self.is_active(uid):
            return "Wallet updates after each completed cycle"
        if uid in self.busy:
            raise UserError("Account is checking its login; try again shortly")
        self.busy.add(uid)
        self.snapshot()
        try:
            with account_lock(self.state_dir(account)):
                async with self.client_factory(self.state_dir(account)) as client:
                    profile = await client.restore(account["username"])
                    if not profile:
                        password = self.password(account)
                        if not password:
                            raise UserError("Edit this account to save a password")
                        profile = await client.login(account["username"], password)
                    self.cache_profile_balance(account, profile)
            return "Wallet refreshed"
        except Exception:
            self.balance_errors[uid] = "Wallet refresh unavailable"
            raise
        finally:
            self.busy.discard(uid)

    async def edit_account(self, message):
        uid = message.get("account_id")
        old = self.account(uid) if uid else None
        username = str(message.get("username", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,80}", username):
            raise UserError("Username: use 1-80 letters, digits or . _ @ + -")
        if old and uid in self.busy:
            raise UserError("Wait for the login check before saving")
        if old and username != old["username"]:
            raise UserError("Add a new account to change the username")
        ident = account_id(username)
        if not old and any(a["id"] == ident for a in self.accounts):
            raise UserError("This username is already saved")
        if message.get("task") not in TASKS:
            raise UserError("Choose Typing, Math or Emoji")
        account = {
            "id": ident,
            "username": username,
            "task": message["task"],
            "enabled": bool(message.get("enabled", True)),
            "daily_target": target_value(message.get("daily_target", "0")),
        }
        if old and self.is_active(uid):
            if not message.get("stop_and_save"):
                raise UserError("Use Stop & save to edit a running account")
            await self.stop_job(uid)
        if old and old["task"] != account["task"]:
            self.states.pop(uid, None)
        password = message.pop("password", "")
        if old:
            self.accounts[self.accounts.index(old)] = account
        else:
            self.accounts.append(account)
        self.save()
        self.event(username + ": account saved")
        self.snapshot(True)
        self.emit({"type": "account_saved", "id": message.get("id"), "account_id": ident})
        if password:
            try:
                await self.login(account, password)
            except Exception as exc:
                raise UserError("Account saved. Login failed: " + friendly(exc)) from None
        return ident

    async def stop_job(self, uid):
        self.set_schedule(uid, "stopped")
        job, control = self.jobs[uid]
        control.stopped = True
        control.paused = False
        self.states[uid]["status"] = "Stopping"
        self.snapshot()
        await job

    def verification_status(self, account, active, steps=0):
        state = self.states.get(account["id"])
        if state is None:
            return
        if active:
            state["activity"] = "verification"
            state["verification_steps"] = max(0, int(steps or 0))
            if state.get("status") not in ("Stopping", "Paused", "Pause after current"):
                state["status"] = "Solving human verification"
        else:
            state["activity"] = ""
            state["verification_steps"] = 0
            if state.get("status") == "Solving human verification":
                state["status"] = "Running"
        self.snapshot()

    def wait_status(self, account, control, status, remaining):
        state = self.states[account["id"]]
        state["wait_until"] = time.time() + remaining if remaining > 0 else 0
        if remaining > 0 and status == "Resting" and state.get("wait_kind") != "Resting":
            self.event(f"{account['username']}: resting for {math.ceil(remaining / 60)} min")
        state["wait_kind"] = status if remaining > 0 else ""
        state["wait_total"] = max(state.get("wait_total", 0), remaining) if remaining > 0 else 0
        state["status"] = "Stopping" if control.stopped else "Paused" if control.paused else status
        self.snapshot()

    def on_row(self, account, task, run_id, row):
        state = self.states[account["id"]]
        self.record_outcome(account, run_id, task, row)
        earned = row.get("earned_points")
        if row["verdict"] == "correct" and earned and Decimal(earned) > 0:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO awards VALUES (?, ?, ?, ?, ?)",
                    (account["id"], run_id, row["index"], row["decided_at"], earned),
                )
        counts = state.setdefault("counts", Counter())
        counts[row["verdict"]] += 1
        decided = counts["correct"] + counts["incorrect"]
        state["summary"] = {
            "task": task,
            "recorded_challenges": sum(counts.values()),
            "verdicts": dict(counts),
            "decided_submissions": decided,
        }
        state["batch_points"] = row["batch_points"]
        if row["batch_size"]:
            state["batch_size"] = row["batch_size"]
        state["last_result"] = dict(row)
        answer = row.get("answer")
        state["last"] = {
            **row,
            "answer": answer,
            "prediction": answer or "",
        }
        gained = " +PHP " + earned if earned and Decimal(earned) > 0 else ""
        shown = f" ({answer})" if answer else ""
        self.event(f"{account['username']}: #{row['index']} {row['verdict']}{gained}{shown}")
        self.snapshot()

    async def run_account(self, account, task, settings, control):
        uid = account["id"]
        state = self.states[uid]
        run_id = now_utc().strftime("%Y%m%dT%H%M%S%fZ")
        try:
            while control.paused and not control.stopped:
                await asyncio.sleep(0.2)
            if control.stopped:
                state["status"] = "Stopped"
                return
            state["status"] = "Connecting"
            self.snapshot()
            with account_lock(self.state_dir(account)):
                async with self.client_factory(self.state_dir(account)) as client:
                    profile = await client.restore(account["username"])

                    async def reauthenticate():
                        password = self.password(account)
                        if not password:
                            raise UserError("Session expired; edit this account to save a password")
                        self.event(account["username"] + ": signing in again")
                        self.cache_profile_balance(
                            account, await client.login(account["username"], password)
                        )

                    if profile:
                        self.cache_profile_balance(account, profile)
                    else:
                        await reauthenticate()
                    await engine.run(
                        Job(
                            client=client,
                            game=task,
                            control=control,
                            pacer=Pacer(lambda: dict(self.settings)),
                            reauthenticate=reauthenticate,
                            should_continue=lambda: not self.progress(account)["target_reached"],
                            on_row=lambda row: self.on_row(account, task, run_id, row),
                            on_wait=lambda status, remaining: self.wait_status(
                                account, control, status, remaining
                            ),
                            on_wallet=lambda amount: self.cache_balance(account, amount),
                            log=lambda message: self.event(f"{account['username']}: {message}"),
                            on_verification=lambda active, steps=0: self.verification_status(
                                account, active, steps
                            ),
                            network_retries=settings["network_retries"],
                            retry_delay=settings["retry_delay"],
                            on_unknown=settings["on_unknown"],
                        )
                    )
                    client.save()
            mode = self.schedule_mode(uid)
            if mode in ("stopped", "paused"):
                state["status"] = "Paused" if mode == "paused" else "Stopped"
            elif self.progress(account)["target_reached"]:
                self.set_schedule(uid, "waiting")
                state["status"] = "Target reached"
            else:
                state["status"] = "Stopped" if self.closing else "Scheduled"
            self.event(f"{account['username']}: {state['status'].lower()}")
        except asyncio.CancelledError:
            state["status"] = "Stopped"
            raise
        except Exception as exc:
            state["status"] = "Stopped"
            if control.stopped:
                self.set_schedule(uid, "stopped")
            else:
                self.set_schedule(uid, "error")
                state["error"] = friendly(exc)
                self.event(f"{account['username']}: {state['error']}")
        finally:
            state["wait_until"] = 0
            state["activity"] = ""
            state["verification_steps"] = 0
            self.last_scan = 0
            self.snapshot()

    async def start(self, message):
        settings = dict(self.settings)
        chosen = message.get("account_id")
        selected = [self.account(chosen)] if chosen else [a for a in self.accounts if a["enabled"]]
        if not selected:
            raise UserError("Add or enable an account in Accounts first")
        if chosen and (self.is_active(chosen) or chosen in self.busy):
            raise UserError("This account is already active")
        if chosen and self.schedule_mode(chosen) == "paused":
            raise UserError("Use Resume for a paused account")
        selected = [
            a
            for a in selected
            if not self.is_active(a["id"])
            and a["id"] not in self.busy
            and a["id"] not in self.pending_accounts
            and (chosen or self.schedule_mode(a["id"]) not in ("waiting", "paused"))
            and (self.session_saved(a) or self.password(a))
        ]
        if not selected:
            raise UserError("No accounts are ready. Check saved logins and active runs")
        for account in selected:
            uid = account["id"]
            self.set_schedule(uid, "running", "account")
            if self.progress(account)["target_reached"]:
                self.set_schedule(uid, "waiting")
                self.states.setdefault(uid, {})["status"] = "Target reached"
                continue
            control = Control()
            task = account["task"]
            self.states[uid] = {"status": "Queued", "task": task}
            job = asyncio.create_task(self.run_account(dict(account), task, settings, control))
            self.jobs[uid] = (job, control)
        self.event(f"Started {len(selected)} account(s)")

    async def scheduler_tick(self):
        if self.closing:
            return
        for account in list(self.accounts):
            uid = account["id"]
            if (
                self.schedule_mode(uid) not in ("running", "waiting")
                or self.is_active(uid)
                or uid in self.pending_accounts
                or uid in self.busy
                or not account["enabled"]
            ):
                continue
            if self.progress(account)["target_reached"]:
                self.set_schedule(uid, "waiting")
                self.states.setdefault(uid, {})["status"] = "Target reached"
                continue
            self.set_schedule(uid, "stopped")
            try:
                await self.start({"account_id": uid})
            except Exception as exc:
                self.set_schedule(uid, "error")
                self.states.setdefault(uid, {}).update(status="Stopped", error=friendly(exc))

    async def dispatch(self, message):
        command = message.get("command")
        uid = message.get("account_id")
        if command == "save_account":
            ident = await self.edit_account(message)
            message["saved_account_id"] = ident
            state = "login verified" if self.session_saved(self.account(ident)) else "login needed"
            return "Account saved; " + state
        if command == "clear_history":
            if message.get("confirmed") is not True:
                raise UserError("Confirm Clear history first")
            self.clear_history(uid)
            return "History cleared; daily progress and login retained"
        if command == "refresh_balance":
            return await self.refresh_balance(self.account(uid))
        if command == "check":
            await self.login(self.account(uid))
            return "Login refreshed"
        if command == "toggle":
            account = self.account(uid)
            if self.is_active(uid) or uid in self.busy:
                raise UserError("Stop this account before changing it")
            account["enabled"] = not account["enabled"]
            self.save()
            return "Account updated"
        if command in ("remove", "logout"):
            account = self.account(uid)
            if uid in self.busy:
                raise UserError("Wait for the login check first")
            if self.is_active(uid):
                await self.stop_job(uid)
            with account_lock(self.state_dir(account)):
                SessionStore(self.state_dir(account)).clear()
                self.credentials(account).clear()
            self.set_schedule(uid, "stopped")
            self.states.pop(uid, None)
            if command == "remove":
                self.accounts.remove(account)
                with self.db:
                    self.db.execute("DELETE FROM schedule WHERE account_id=?", (uid,))
                    self.db.execute("DELETE FROM balances WHERE account_id=?", (uid,))
            self.save()
            return "Account removed" if command == "remove" else "Local session cleared"
        if command == "settings":
            settings = dict(self.settings)
            settings.update(
                {k: v for k, v in message.get("settings", {}).items() if k in DEFAULTS}
            )
            self.validate_settings(settings)
            self.settings = settings
            self.save()
            return "Settings saved; pacing changes apply at the next interval"
        if command == "start":
            await self.start(message)
            return "Run started"
        if command in ("pause", "resume", "stop"):
            return self.control(command, uid)
        if command == "shutdown":
            self.closing = True
            return "Stopping active accounts before exit"
        raise UserError("Unknown command")

    def control(self, command, uid):
        selected = (
            [uid]
            if uid
            else [
                a["id"]
                for a in self.accounts
                if self.is_active(a["id"])
                or self.schedule_mode(a["id"]) in ("running", "waiting", "paused")
            ]
        )
        selected = [i for i in selected if self.action_flags(self.account(i))[command]]
        if not selected:
            raise UserError("No accounts are eligible for " + command)
        for ident in selected:
            self.set_schedule(
                ident, {"pause": "paused", "resume": "running", "stop": "stopped"}[command]
            )
            if self.is_active(ident):
                control = self.jobs[ident][1]
                if command == "pause":
                    control.paused = True
                elif command == "resume":
                    control.paused = False
                else:
                    control.stopped = True
                    control.paused = False
                self.states[ident]["status"] = {
                    "pause": "Pause after current",
                    "resume": "Running",
                    "stop": "Stopping",
                }[command]
            else:
                self.states.setdefault(ident, {})["status"] = {
                    "pause": "Paused",
                    "resume": "Scheduled",
                    "stop": "Stopped",
                }[command]
        label = {"pause": "Pause requested", "resume": "Resumed", "stop": "Stop requested"}[command]
        return f"{label} for {len(selected)} account(s)"

    async def handle(self, message):
        uid = message.get("account_id")
        guarded = message.get("command") in (
            "save_account",
            "check",
            "refresh_balance",
            "remove",
            "toggle",
            "clear_history",
        )
        if guarded and uid in self.pending_accounts:
            self.emit(
                {
                    "type": "result",
                    "id": message.get("id"),
                    "ok": False,
                    "message": "Account action already pending",
                }
            )
            return
        if guarded and uid:
            self.pending_accounts.add(uid)
        try:
            result = await self.dispatch(message)
            self.emit(
                {
                    "type": "result",
                    "id": message.get("id"),
                    "ok": True,
                    "message": result,
                    "account_id": message.get("saved_account_id"),
                }
            )
        except Exception as exc:
            self.emit(
                {"type": "result", "id": message.get("id"), "ok": False, "message": friendly(exc)}
            )
        finally:
            if guarded:
                self.pending_accounts.discard(uid)
            message.pop("password", None)
            self.snapshot(True)

    async def run(self):
        self.snapshot(True)
        while not self.closing:
            text = await asyncio.to_thread(self.bridge.receive)
            if text:
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                task = asyncio.create_task(self.handle(message))
                self.commands.add(task)
                task.add_done_callback(self.commands.discard)
            if time.monotonic() - self.last_schedule >= 1:
                self.last_schedule = time.monotonic()
                await self.scheduler_tick()
            self.snapshot()
        for _, control in self.jobs.values():
            control.stopped = True
            control.paused = False
        tasks = [job for job, _ in self.jobs.values() if not job.done()] + list(self.commands)
        tasks = [task for task in tasks if task is not asyncio.current_task()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=45)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.emit({"type": "closed"})


def serve(bridge, root, data):
    service = Service(bridge, root, data)
    try:
        asyncio.run(service.run())
    finally:
        service.db.close()


def self_test(root):
    return json.dumps(
        {
            "embedded_python": "ok",
            "root": str(root),
            "math_check": solve_math_problem(2, "+", 3, [4, 5, 6]),
            "network_calls": 0,
        }
    )
