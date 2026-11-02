import asyncio
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs

import httpx

from keycash.backend import Service, account_id
from keycash.client import KeycashClient, parse_wallet
from keycash.credentials import CredentialStore
from test_games import TYPING_HTML

CHALLENGE = {
    "id": "check-1",
    "steps": [
        {"type": "type_text", "prompt": "Type this text exactly", "display": "ab1"},
        {"prompt": "Sum", "display": "1 + 1 = ?"},
        {"prompt": "How many ★ are there?", "display": "★ ☆ ★ ★"},
    ],
}


class Bridge:
    def __init__(self):
        self.events = []

    def emit(self, text):
        self.events.append(json.loads(text))

    def last(self, kind="snapshot"):
        return [event for event in self.events if event["type"] == kind][-1]


class Site:
    """Fake site: endless typing cycles, optionally demanding verification first."""

    def __init__(self, verify=False):
        self.verify = verify
        self.solves = []
        self.tokens = []
        self.challenge = CHALLENGE

    def __call__(self, request):
        if request.method == "GET" and request.url.query == b"c=login":
            return httpx.Response(
                200,
                text="<form><input name='identifier'><input name='password'></form>",
                headers={"set-cookie": "PHPSESSID=a; Path=/; Domain=keycashv2.pro"},
            )
        if request.method == "GET":
            html = TYPING_HTML + "<span id='header-wallet'>₱20.03</span>"
            return httpx.Response(
                200,
                text=html,
                headers={"set-cookie": "PHPSESSID=a; Path=/; Domain=keycashv2.pro"},
            )
        data = parse_qs(request.content.decode())
        if "identifier" in data:
            return httpx.Response(302, headers={"location": "./?c=dashboard"})
        if "game-start" in data:
            if self.verify and "captcha_token" not in data:
                return httpx.Response(
                    200, json={"captcha_required": True, "captcha_challenge": self.challenge}
                )
            self.tokens += data.get("captcha_token", [])
            return httpx.Response(200, json={"started": True, "next_index": 0})
        self.solves.append(data)
        return httpx.Response(
            200,
            json={
                "correct": True,
                "correct_count": 0,
                "next_index": 0,
                "cycle_complete": True,
                "cooldown": 1,
                "added": 0.01,
                "total": 20.04,
            },
        )


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def service(self, site):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Bridge()
        factory = lambda state: KeycashClient(state, transport=httpx.MockTransport(site))
        service = Service(self.bridge, self.temp.name, self.temp.name, client_factory=factory)
        self.addCleanup(service.db.close)
        service.settings.update(solve_delay_min=0, solve_delay_max=0, rests_enabled=False)
        return service

    async def account(self, service, target="0"):
        await service.dispatch(
            {
                "command": "save_account",
                "username": "tester",
                "password": "secret-password",
                "task": "typing",
                "daily_target": target,
            }
        )
        return account_id("tester")

    async def until(self, condition):
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(0.05)
        self.fail("condition was not reached")

    async def test_zero_target_keeps_solving_across_cycles_until_stopped(self):
        site = Site()
        service = self.service(site)
        uid = await self.account(service)
        await service.dispatch({"command": "start", "account_id": uid})
        await self.until(lambda: len(site.solves) >= 2)
        await service.dispatch({"command": "stop", "account_id": uid})
        await service.jobs[uid][0]
        service.last_scan = 0
        service.snapshot(True)
        account = self.bridge.last()["accounts"][0]
        self.assertGreaterEqual(account["summary"]["verdicts"]["correct"], 2)
        self.assertEqual(account["balance"]["amount"], "20.04")
        self.assertEqual(account["status"], "Stopped")
        self.assertEqual(account["last_result"]["answer"], "alpha")
        self.assertEqual(account["recent_solves"][0]["answer"], "alpha")
        self.assertEqual(account["daily_points"][:4], str(0.01 * len(site.solves))[:4])

    async def test_daily_target_stops_the_run_and_waits(self):
        site = Site()
        service = self.service(site)
        uid = await self.account(service, target="0.02")
        await service.dispatch({"command": "start", "account_id": uid})
        await service.jobs[uid][0]
        self.assertEqual(len(site.solves), 2)
        self.assertEqual(service.schedule_mode(uid), "waiting")

    async def test_verification_is_solved_automatically(self):
        site = Site(verify=True)
        service = self.service(site)
        uid = await self.account(service, target="0.01")
        await service.dispatch({"command": "start", "account_id": uid})
        await service.jobs[uid][0]
        self.assertEqual(site.tokens, ["check-1|AB1|2|3"])
        self.assertEqual(len(site.solves), 1)
        service.snapshot(True)
        self.assertNotIn("captcha", self.bridge.last()["accounts"][0])

    async def test_verification_activity_is_exposed_and_cleared(self):
        site = Site()
        service = self.service(site)
        uid = await self.account(service)
        account = service.account(uid)

        service.verification_status(account, True, 3)
        service.snapshot(True)
        shown = self.bridge.last()["accounts"][0]
        self.assertEqual(shown["activity"], "verification")
        self.assertEqual(shown["verification_steps"], 3)
        self.assertEqual(shown["status"], "Solving human verification")

        service.verification_status(account, False)
        service.snapshot(True)
        shown = self.bridge.last()["accounts"][0]
        self.assertEqual(shown["activity"], "")
        self.assertEqual(shown["verification_steps"], 0)
        self.assertEqual(shown["status"], "Running")

    async def test_unsolvable_verification_stops_the_account_with_an_error(self):
        site = Site(verify=True)
        site.challenge = {"id": "check-2", "steps": [{}, {}, {}]}
        service = self.service(site)
        uid = await self.account(service)
        await service.dispatch({"command": "start", "account_id": uid})
        await service.jobs[uid][0]
        self.assertEqual(site.solves, [])
        self.assertEqual(service.schedule_mode(uid), "error")
        self.assertIn("could not be solved automatically", service.states[uid]["error"])

    async def test_pause_holds_and_resume_continues(self):
        site = Site()
        service = self.service(site)
        uid = await self.account(service)
        await service.dispatch({"command": "start", "account_id": uid})
        await self.until(lambda: len(site.solves) >= 1)
        await service.dispatch({"command": "pause", "account_id": uid})
        await asyncio.sleep(0.1)
        held = len(site.solves)
        await asyncio.sleep(1.3)
        self.assertEqual(len(site.solves), held)
        await service.dispatch({"command": "resume", "account_id": uid})
        await self.until(lambda: len(site.solves) > held)
        await service.dispatch({"command": "stop", "account_id": uid})
        await service.jobs[uid][0]

    async def test_saved_cli_login_appears_and_passwords_stay_out_of_snapshots(self):
        site = Site()
        first = self.service(site)
        await self.account(first)
        first.db.execute("DELETE FROM accounts")
        first.db.commit()
        second = Service(self.bridge, self.temp.name, self.temp.name)
        self.addCleanup(second.db.close)
        self.assertEqual([a["username"] for a in second.accounts], ["tester"])
        second.snapshot(True)
        self.assertNotIn("secret-password", json.dumps(self.bridge.events))
        store = CredentialStore(Path(self.temp.name) / "accounts" / account_id("tester"))
        self.assertEqual(store.load("tester"), "secret-password")

    def test_wallet_is_read_from_the_page_header(self):
        self.assertEqual(parse_wallet("<b id='header-wallet'>₱1,020.03</b>"), "1020.03")
        self.assertIsNone(parse_wallet("<b>nothing</b>"))

    def test_existing_outcomes_table_gains_answer_column(self):
        with TemporaryDirectory() as data:
            db = sqlite3.connect(Path(data) / "keycash.sqlite3")
            db.executescript(
                """
                CREATE TABLE outcomes (
                    account_id TEXT NOT NULL, username TEXT NOT NULL, run_id TEXT NOT NULL,
                    challenge INTEGER NOT NULL, task TEXT NOT NULL, verdict TEXT NOT NULL,
                    decided_at TEXT NOT NULL, points TEXT,
                    PRIMARY KEY (account_id, run_id, challenge));
                """
            )
            db.commit()
            db.close()
            service = Service(Bridge(), data, data)
            self.addCleanup(service.db.close)
            columns = {
                row[1] for row in service.db.execute("PRAGMA table_info(outcomes)")
            }
            self.assertIn("answer", columns)


if __name__ == "__main__":
    unittest.main()
