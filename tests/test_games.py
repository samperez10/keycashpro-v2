import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs

import httpx

from keycash.client import KeycashClient
from keycash.games import (
    GameError,
    GameRunner,
    ManualCaptchaRequired,
    SessionExpired,
    SubmissionOutcomeUnknown,
    captcha_token,
    parse_emoji_page,
    parse_math_page,
    parse_typing_page,
    solve_challenge_steps,
    solve_math_problem,
)


def html(script):
    return f"<html><script>{script}</script></html>"


TYPING_HTML = html(
    'const WORDS = ["alpha","beta"];'
    'const WORD_THEMES = ["Test","Test"];'
    "const TICKET = 'typing-ticket';"
    "const CYCLE_TARGET = 5;"
    "const DEFAULT_COOLDOWN = 20;"
    "let wordIndex = 0;"
)
EMOJI_HTML = html(
    'const EMOJIS = ["🙂","📷"];'
    'const EMOJI_MAP = {"🙂":"Smile","📷":"Camera"};'
    "const TICKET = 'emoji-ticket';"
    "const CYCLE_TARGET = 8;"
    "const DEFAULT_COOLDOWN = 20;"
    "let idx = 0;"
)
MATH_HTML = html(
    "const TICKET = 'math-ticket';"
    "const WAIT_TIME = 25;"
    "const SERVER_REMAINING = 0;"
    "const CYCLE_TARGET = 5;"
)


class ParserTests(unittest.TestCase):
    def test_typing_uses_fresh_page_values(self):
        page = parse_typing_page(TYPING_HTML)
        self.assertEqual(page.words, ("alpha", "beta"))
        self.assertEqual(page.themes, ("Test", "Test"))
        self.assertEqual(page.ticket, "typing-ticket")
        self.assertEqual(page.cycle_target, 5)

    def test_emoji_requires_every_mapping(self):
        page = parse_emoji_page(EMOJI_HTML)
        self.assertEqual(page.names["📷"], "Camera")
        bad = EMOJI_HTML.replace('"📷":"Camera"', '"other":"Camera"')
        with self.assertRaisesRegex(GameError, "no answer mapping"):
            parse_emoji_page(bad)

    def test_math_page_values(self):
        page = parse_math_page(MATH_HTML)
        self.assertEqual((page.wait_time, page.cycle_target), (25, 5))

    def test_math_answers_must_match_one_server_option(self):
        cases = [
            (7, "+", 5, [11, 12, 13], 12),
            (7, "-", 5, [1, 2, 3], 2),
            (7, "×", 5, [30, 35, 40], 35),
            (8, "÷", 2, [2, 4, 6], 4),
        ]
        for a, op, b, options, expected in cases:
            with self.subTest(op=op):
                self.assertEqual(solve_math_problem(a, op, b, options), expected)
        with self.assertRaisesRegex(GameError, "missing or ambiguous"):
            solve_math_problem(2, "+", 2, [3, 5])

    def test_captcha_token_needs_three_manual_answers(self):
        challenge = {"id": "abc", "steps": [{}, {}, {}]}
        self.assertEqual(captcha_token(challenge, ["A B", "2", "X"]), "abc|A%20B|2|X")
        with self.assertRaises(GameError):
            captcha_token(challenge, ["only one"])


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        self.temp = TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        return KeycashClient(self.temp.name, transport=httpx.MockTransport(handler))

    async def _cleanup(self):
        self.temp.cleanup()

    async def test_typing_runner_submits_fresh_word(self):
        posts = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=TYPING_HTML)
            data = parse_qs(request.content.decode())
            posts.append(data)
            if "game-start" in data:
                return httpx.Response(200, json={"started": True, "next_index": 0})
            self.assertEqual(data["word"], ["alpha"])
            return httpx.Response(
                200,
                json={
                    "correct": True,
                    "correct_count": 1,
                    "next_index": 1,
                    "completed_cycles": 0,
                },
            )

        async with self.client(handler) as client:
            rows = await GameRunner(client).run("typing", 1)
        self.assertTrue(rows[0]["correct"])
        self.assertEqual(rows[0]["answer"], "alpha")
        self.assertEqual(len(posts), 2)

    async def test_emoji_runner_uses_fresh_mapping(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=EMOJI_HTML)
            data = parse_qs(request.content.decode())
            if "game-start" in data:
                return httpx.Response(200, json={"started": True, "next_index": 0})
            self.assertEqual(data["answer"], ["Smile"])
            return httpx.Response(
                200,
                json={"correct": True, "correct_count": 1, "next_index": 1},
            )

        async with self.client(handler) as client:
            rows = await GameRunner(client).run("emoji", 1)
        self.assertTrue(rows[0]["correct"])
        self.assertEqual(rows[0]["answer"], "🙂 Smile")

    async def test_math_runner_uses_problem_token_and_option(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=MATH_HTML)
            data = parse_qs(request.content.decode())
            if "game-start" in data:
                return httpx.Response(200, json={"started": True})
            if "math-problem" in data:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "a": 9,
                        "b": 3,
                        "op": "÷",
                        "options": [2, 3, 6],
                        "token": "problem-token",
                    },
                )
            self.assertEqual(data["token"], ["problem-token"])
            self.assertEqual(data["answer"], ["3"])
            return httpx.Response(
                200,
                json={"success": True, "correct_count": 1},
            )

        async with self.client(handler) as client:
            rows = await GameRunner(client).run("math", 1)
        self.assertTrue(rows[0]["correct"])
        self.assertEqual(rows[0]["answer"], "3")

    async def test_lost_submission_is_not_replayed(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("lost", request=request)

        async with self.client(handler) as client:
            runner = GameRunner(client)
            with self.assertRaises(SubmissionOutcomeUnknown):
                await runner._post({"typing-solve": "1"}, submission=True)
        self.assertEqual(calls, 1)

    async def test_captcha_is_solved_automatically_then_request_is_resubmitted(self):
        challenge = {
            "id": "check-1",
            "steps": [
                {"type": "type_text", "prompt": "Type this text exactly", "display": "ab c"},
                {"prompt": "Solve", "display": "3 × 4 = ?"},
                {"prompt": "How many ★ are there?", "display": "★ ☆ ★ ★"},
            ],
        }
        posts = []

        def handler(request):
            data = parse_qs(request.content.decode())
            posts.append(data)
            if "captcha_token" not in data:
                return httpx.Response(
                    200,
                    json={
                        "captcha_required": True,
                        "captcha_challenge": challenge,
                    },
                )
            return httpx.Response(200, json={"correct": True})

        async with self.client(handler) as client:
            verification = []
            runner = GameRunner(
                client,
                challenge_delay=0,
                on_verification=lambda active, steps=0: verification.append(
                    (active, steps)
                ),
            )
            result = await runner._with_captcha(
                {"emoji-solve": "1", "answer": "Smile"},
                submission=True,
            )

        self.assertTrue(result["correct"])
        self.assertEqual(verification, [(True, 3), (False, 0)])
        self.assertEqual(posts[1]["captcha_token"], ["check-1|AB%20C|12|3"])
        self.assertEqual(posts[1]["answer"], ["Smile"])

    async def test_unsolvable_captcha_raises_without_resubmitting(self):
        challenge = {"id": "check-1", "steps": [{}, {}, {}]}
        posts = []

        def handler(request):
            posts.append(parse_qs(request.content.decode()))
            return httpx.Response(
                200, json={"captcha_required": True, "captcha_challenge": challenge}
            )

        async with self.client(handler) as client:
            verification = []
            with self.assertRaises(ManualCaptchaRequired):
                await GameRunner(
                    client,
                    challenge_delay=0,
                    on_verification=lambda active, steps=0: verification.append(
                        (active, steps)
                    ),
                )._with_captcha({"emoji-solve": "1"}, submission=True)
        self.assertEqual(verification, [(True, 3), (False, 0)])
        self.assertEqual(len(posts), 1)

    def test_challenge_steps_are_solved_per_step(self):
        challenge = {
            "steps": [
                {"prompt": "Type this text exactly", "display": "lim5rd"},
                {"prompt": "", "display": "(2 + 3) x 2 = ?"},
                {"prompt": "Name this colour", "display": "blue"},
            ]
        }
        self.assertEqual(solve_challenge_steps(challenge), ["lim5rd", 10, None])


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class ContinuousTests(unittest.IsolatedAsyncioTestCase):
    client = RunnerTests.client
    _cleanup = RunnerTests._cleanup

    def game_server(self, solve, *, page=None):
        """Serve a game page and `game-start`, passing solves to `solve`."""
        self.gets = 0
        self.starts = 0
        self.solves = []

        def handler(request):
            if request.method == "GET":
                self.gets += 1
                return httpx.Response(200, text=page or TYPING_HTML)
            data = parse_qs(request.content.decode())
            if "game-start" in data:
                self.starts += 1
                return httpx.Response(200, json={"started": True, "next_index": 0})
            self.solves.append(data)
            return solve(data)

        return handler

    @staticmethod
    def word_reply(index, **extra):
        return httpx.Response(
            200, json={"correct": True, "correct_count": 1, "next_index": index, **extra}
        )

    async def test_cycles_continue_on_one_ticket_after_the_server_cooldown(self):
        def solve(data):
            number = len(self.solves)
            if number % 2 == 0:
                return self.word_reply(
                    0, cycle_complete=True, cooldown=20, added="0.01", total="20.04"
                )
            return self.word_reply(1)

        sleeps = Sleeps()
        async with self.client(self.game_server(solve)) as client:
            rows = [
                row
                async for row in GameRunner(client, sleep=sleeps).play("typing", cycles=3)
            ]
        self.assertEqual(sum(row["cycle_complete"] for row in rows), 3)
        self.assertEqual(len(rows), 6)
        self.assertEqual((self.gets, self.starts), (1, 1))
        self.assertEqual({item["ticket"][0] for item in self.solves}, {"typing-ticket"})
        self.assertEqual(sleeps.calls, [20, 20])
        self.assertEqual(rows[-1]["answer_number"], 6)

    async def test_cooldown_reply_is_waited_out_without_recording_an_answer(self):
        replies = [
            httpx.Response(
                200, json={"cooldown": True, "cooldown_remaining": 7, "next_index": 1}
            ),
            self.word_reply(0),
        ]
        sleeps = Sleeps()
        async with self.client(self.game_server(lambda data: replies.pop(0))) as client:
            rows = await GameRunner(client, sleep=sleeps).run("typing", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sleeps.calls, [7])
        self.assertEqual(
            [item["word"] for item in self.solves], [["alpha"], ["beta"]]
        )

    async def test_stop_during_a_cooldown_ends_without_another_submission(self):
        stop = asyncio.Event()

        async def sleep(seconds):
            stop.set()
            await asyncio.Event().wait()

        def solve(data):
            return self.word_reply(1, cycle_complete=True, cooldown=20)

        async with self.client(self.game_server(solve)) as client:
            runner = GameRunner(client, stop=stop, sleep=sleep)
            rows = [row async for row in runner.play("typing")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.solves), 1)

    async def test_lost_submission_stops_and_keeps_earlier_rows(self):
        def solve(data):
            if len(self.solves) == 2:
                raise httpx.ReadTimeout("lost")
            return self.word_reply(1)

        rows = []
        async with self.client(self.game_server(solve)) as client:
            with self.assertRaises(SubmissionOutcomeUnknown):
                async for row in GameRunner(client, sleep=Sleeps()).play("typing"):
                    rows.append(row)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.solves), 2)

    async def test_server_error_on_submission_is_not_replayed(self):
        def solve(data):
            return httpx.Response(502, json={"error": "bad gateway"})

        async with self.client(self.game_server(solve)) as client:
            with self.assertRaises(SubmissionOutcomeUnknown):
                await GameRunner(client, sleep=Sleeps()).run("typing", 2)
        self.assertEqual(len(self.solves), 1)

    async def test_expired_session_signs_in_once_and_reopens_the_game(self):
        replies = [httpx.Response(302, headers={"location": "./?c=login"}), self.word_reply(1)]
        logins = []

        async def reauthenticate():
            logins.append(1)

        handler = self.game_server(lambda data: replies.pop(0))
        async with self.client(handler) as client:
            rows = await GameRunner(
                client, sleep=Sleeps(), reauthenticate=reauthenticate
            ).run("typing", 1)
        self.assertEqual(len(logins), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.gets, 2)

    async def test_expired_session_without_a_login_stops(self):
        handler = self.game_server(
            lambda data: httpx.Response(302, headers={"location": "./?c=login"})
        )
        async with self.client(handler) as client:
            with self.assertRaises(SessionExpired):
                await GameRunner(client, sleep=Sleeps()).run("typing", 1)

    async def test_page_failures_back_off_then_recover(self):
        failures = [503, 503]
        inner = self.game_server(lambda data: self.word_reply(1))

        def handler(request):
            if request.method == "GET" and failures:
                return httpx.Response(failures.pop())
            return inner(request)

        sleeps = Sleeps()
        async with self.client(handler) as client:
            rows = await GameRunner(client, sleep=sleeps).run("typing", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sleeps.calls, [5, 10])

    async def test_page_failures_are_bounded(self):
        async with self.client(lambda request: httpx.Response(503)) as client:
            with self.assertRaisesRegex(GameError, "Gave up"):
                await GameRunner(client, sleep=Sleeps()).run("typing", 1)

    async def test_index_past_the_list_reopens_the_page(self):
        replies = [self.word_reply(99), self.word_reply(1)]
        async with self.client(self.game_server(lambda data: replies.pop(0))) as client:
            rows = await GameRunner(client, sleep=Sleeps()).run("typing", 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.gets, 2)

    async def test_two_rejections_in_a_row_stop_the_run(self):
        reject = lambda data: httpx.Response(200, json={"correct": False})
        rows = []
        async with self.client(self.game_server(reject)) as client:
            with self.assertRaisesRegex(GameError, "rejected 2 answers"):
                async for row in GameRunner(client, sleep=Sleeps()).play("typing"):
                    rows.append(row)
        self.assertEqual([row["correct"] for row in rows], [False, False])
        self.assertEqual(self.gets, 2)

    async def test_math_waits_between_every_question_across_cycles(self):
        def solve(data):
            if "math-problem" in data:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "a": 1,
                        "b": 1,
                        "op": "+",
                        "options": [1, 2],
                        "token": "t",
                    },
                )
            finished = sum("math-solve" in item for item in self.solves)
            return httpx.Response(
                200,
                json={"success": True, "cycle_complete": finished == 2, "added": "0.01"},
            )

        sleeps = Sleeps()
        handler = self.game_server(solve, page=MATH_HTML)
        async with self.client(handler) as client:
            rows = await GameRunner(client, sleep=sleeps).run("math", 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sleeps.calls, [25, 25])

    async def test_rate_limit_backs_off_and_resends_the_unprocessed_answer(self):
        replies = [
            httpx.Response(429, json={"error": "Too many requests. Please slow down."}),
            httpx.Response(200, json={"error": "Too many requests."}),
            self.word_reply(1),
        ]
        sleeps = Sleeps()
        async with self.client(self.game_server(lambda data: replies.pop(0))) as client:
            rows = await GameRunner(client, sleep=sleeps).run("typing", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sleeps.calls, [30, 60])
        self.assertEqual([item["word"] for item in self.solves], [["alpha"]] * 3)

    async def test_rate_limits_are_bounded(self):
        limit = lambda data: httpx.Response(429, json={"error": "slow down"})
        async with self.client(self.game_server(limit)) as client:
            with self.assertRaisesRegex(GameError, "repeated rate limits"):
                await GameRunner(client, sleep=Sleeps()).run("typing", 1)
        self.assertEqual(len(self.solves), 6)

    async def test_rate_limited_page_gets_the_long_backoff(self):
        limited = [429]
        inner = self.game_server(lambda data: self.word_reply(1))

        def handler(request):
            if request.method == "GET" and limited:
                return httpx.Response(limited.pop())
            return inner(request)

        sleeps = Sleeps()
        async with self.client(handler) as client:
            await GameRunner(client, sleep=sleeps).run("typing", 1)
        self.assertEqual(sleeps.calls, [30])

    async def test_retry_after_longer_than_backoff_is_honoured(self):
        replies = [
            httpx.Response(429, headers={"retry-after": "45"}, json={}),
            self.word_reply(1),
        ]
        sleeps = Sleeps()
        async with self.client(self.game_server(lambda data: replies.pop(0))) as client:
            await GameRunner(client, sleep=sleeps).run("typing", 1)
        self.assertEqual(sleeps.calls, [45])

    async def test_min_interval_spaces_answers(self):
        sleeps = Sleeps()
        async with self.client(
            self.game_server(lambda data: self.word_reply(1))
        ) as client:
            await GameRunner(client, sleep=sleeps, min_interval=6).run("typing", 3)
        self.assertEqual(sleeps.calls, [6, 6])

    def test_typed_verification_text_is_uppercased(self):
        challenge = {
            "id": "abc",
            "steps": [{"type": "type_text"}, {"type": "number"}, {}],
        }
        self.assertEqual(captcha_token(challenge, ["ab", "c", "d"]), "abc|AB|c|d")


if __name__ == "__main__":
    unittest.main()
