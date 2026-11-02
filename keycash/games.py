"""Fresh-page parsers and a cautious prototype runner for Keycash games."""
import asyncio
import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import operator
import random
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


class GameError(RuntimeError):
    pass


class ManualCaptchaRequired(GameError):
    """Raised when a challenge step cannot be solved automatically."""

    def __init__(self, challenge):
        self.challenge = challenge
        super().__init__("Human verification could not be solved automatically")


class SubmissionOutcomeUnknown(GameError):
    pass


class TransientGameError(GameError):
    """Nothing was submitted, so reopening the game is safe."""


class SessionExpired(GameError):
    pass


class RateLimited(GameError):
    """The server refused the request unprocessed and asked for a slower pace."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class TypingPage:
    ticket: str
    words: tuple[str, ...]
    themes: tuple[str, ...]
    cycle_target: int
    cooldown: int
    index: int


@dataclass(frozen=True)
class EmojiPage:
    ticket: str
    emojis: tuple[str, ...]
    names: dict[str, str]
    cycle_target: int
    cooldown: int
    index: int


@dataclass(frozen=True)
class MathPage:
    ticket: str
    cycle_target: int
    wait_time: int
    remaining: int


def _javascript(html):
    soup = BeautifulSoup(html, "html.parser")
    return "\n".join(
        script.get_text("\n", strip=False)
        for script in soup.find_all("script")
        if not script.get("src")
    )


def _string(js, name):
    match = re.search(
        rf"(?:const|let)\s+{re.escape(name)}\s*=\s*(['\"])(.*?)\1\s*;",
        js,
        re.S,
    )
    if not match or not match.group(2):
        raise GameError(f"{name} is missing from the game page")
    return match.group(2)


def _integer(js, name, default=None):
    match = re.search(rf"(?:const|let)\s+{re.escape(name)}\s*=\s*(\d+)\s*;", js)
    if not match:
        if default is not None:
            return default
        raise GameError(f"{name} is missing from the game page")
    return int(match.group(1))


def _json_value(js, name, opening, closing):
    marker = re.search(rf"(?:const|let)\s+{re.escape(name)}\s*=\s*\{opening}", js)
    if not marker:
        raise GameError(f"{name} is missing from the game page")
    start = marker.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(js)):
        char = js[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                raw = js[start : index + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    raise GameError(f"{name} is not valid JSON") from None
    raise GameError(f"{name} is incomplete")


def _list(js, name):
    value = _json_value(js, name, "[", "]")
    if not isinstance(value, list) or not value:
        raise GameError(f"{name} must be a non-empty list")
    return value


def _object(js, name):
    value = _json_value(js, name, "{", "}")
    if not isinstance(value, dict) or not value:
        raise GameError(f"{name} must be a non-empty object")
    return value


def parse_typing_page(html):
    js = _javascript(html)
    words = _list(js, "WORDS")
    themes = _list(js, "WORD_THEMES")
    if not all(isinstance(word, str) and word for word in words):
        raise GameError("Typing words are invalid")
    if len(themes) != len(words) or not all(isinstance(theme, str) for theme in themes):
        raise GameError("Typing themes do not match the fresh word list")
    index = _integer(js, "wordIndex", 0)
    if not 0 <= index < len(words):
        raise GameError("Typing word index is outside the fresh word list")
    return TypingPage(
        ticket=_string(js, "TICKET"),
        words=tuple(words),
        themes=tuple(themes),
        cycle_target=_integer(js, "CYCLE_TARGET"),
        cooldown=_integer(js, "DEFAULT_COOLDOWN"),
        index=index,
    )


def parse_emoji_page(html):
    js = _javascript(html)
    emojis = _list(js, "EMOJIS")
    names = _object(js, "EMOJI_MAP")
    if not all(isinstance(emoji, str) and emoji for emoji in emojis):
        raise GameError("Emoji list is invalid")
    if any(not isinstance(names.get(emoji), str) or not names[emoji] for emoji in emojis):
        raise GameError("One or more fresh emojis have no answer mapping")
    index = _integer(js, "idx", 0)
    if not 0 <= index < len(emojis):
        raise GameError("Emoji index is outside the fresh list")
    return EmojiPage(
        ticket=_string(js, "TICKET"),
        emojis=tuple(emojis),
        names=names,
        cycle_target=_integer(js, "CYCLE_TARGET"),
        cooldown=_integer(js, "DEFAULT_COOLDOWN"),
        index=index,
    )


def parse_math_page(html):
    js = _javascript(html)
    return MathPage(
        ticket=_string(js, "TICKET"),
        cycle_target=_integer(js, "CYCLE_TARGET"),
        wait_time=_integer(js, "WAIT_TIME"),
        remaining=_integer(js, "SERVER_REMAINING", 0),
    )


def solve_math_problem(a, operator, b, options):
    try:
        left = Decimal(str(a))
        right = Decimal(str(b))
    except InvalidOperation:
        raise GameError("Math problem contains an invalid number") from None
    if not left.is_finite() or not right.is_finite():
        raise GameError("Math problem contains a non-finite number")
    if operator == "+":
        expected = left + right
    elif operator in {"-", "−"}:
        expected = left - right
    elif operator in {"*", "×", "x", "X"}:
        expected = left * right
    elif operator in {"/", "÷"}:
        if right == 0:
            raise GameError("Math problem divides by zero")
        expected = left / right
    else:
        raise GameError(f"Unsupported math operator: {operator}")
    matches = []
    for option in options:
        try:
            value = Decimal(str(option))
        except InvalidOperation:
            continue
        if value.is_finite() and value == expected:
            matches.append(option)
    if len(matches) != 1:
        raise GameError("Calculated math answer is missing or ambiguous in server options")
    return matches[0]


def captcha_token(challenge, answers):
    challenge_id = challenge.get("id") if isinstance(challenge, dict) else None
    steps = challenge.get("steps") if isinstance(challenge, dict) else None
    if not challenge_id or not isinstance(steps, list) or len(steps) != 3:
        raise GameError("Unsupported human-verification challenge")
    if len(answers) != len(steps) or any(not str(answer).strip() for answer in answers):
        raise GameError("Every human-verification step needs an answer")
    cleaned = []
    for step, answer in zip(steps, answers):
        text = str(answer).strip()
        prompt = str(step.get("prompt") or "").casefold()
        is_echo = step.get("type") == "type_text" or "type this text" in prompt
        cleaned.append(text.upper() if is_echo else text)
    encoded = [quote(answer, safe="~()*!.'-") for answer in cleaned]
    return "|".join([str(challenge_id), *encoded])


# ---------- automatic challenge solver ----------

def _clean_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _eval_expr(node):
    if isinstance(node, ast.Expression):
        return _eval_expr(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_expr(node.left), _eval_expr(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = _eval_expr(node.operand)
        return val if isinstance(node.op, ast.UAdd) else -val
    raise ValueError("Unsupported expression")


def _solve_count(block):
    m = re.search(
        r"how many\s+(.+?)\s+(?:in\s+(?:the\s+)?below|are\s+there)\b",
        block,
        re.IGNORECASE,
    )
    if not m:
        return None
    symbol = _strip_variation(m.group(1).strip())
    rest = block[m.end():]
    # Count only in the displayed content (after the prompt line) when present,
    # so symbols mentioned in the instruction itself are not counted.
    if "\n" in rest:
        rest = rest.split("\n", 1)[1]
    found = _strip_variation(rest).count(symbol) if symbol else 0
    # Zero means the symbol was named in words or lives elsewhere: give up
    # rather than submit a confident wrong answer.
    return found or None


def _strip_variation(text):
    return text.replace("\ufe0f", "").replace("\ufe0e", "")


def _solve_equation(block):
    m = re.search(r"([\d\s+\-*/xX×÷().]+?)\s*=\s*\?", block)
    if not m:
        return None
    expr = (
        m.group(1)
        .replace("×", "*")
        .replace("÷", "/")
        .replace("x", "*")
        .replace("X", "*")
        .strip()
    )
    if not expr or not re.fullmatch(r"[\d\s+\-*/().]+", expr):
        return None
    try:
        return _clean_number(_eval_expr(ast.parse(expr, mode="eval")))
    except (ValueError, ZeroDivisionError, SyntaxError):
        return None


def _solve_echo(block):
    m = re.search(r"type this text exactly[^\n]*\n\s*([^\n]+)", block, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


_SOLVERS = (_solve_count, _solve_equation, _solve_echo)


def solve_challenge_steps(challenge):
    """Solve each step of a challenge dict, returning answers in step order.

    Each step looks like {"prompt": "<instruction>", "display": "<content>"}.
    The two fields are combined into a single block so the same regex solvers
    that work on a full challenge paragraph also work per step.

    Returns a list with one entry per step, using None for any step the
    regex solvers could not handle (so the caller can raise or fall back).
    """
    steps = challenge.get("steps") if isinstance(challenge, dict) else None
    if not isinstance(steps, list) or not steps:
        return []

    answers = []
    for step in steps:
        prompt = str(step.get("prompt") or "").strip()
        display = str(step.get("display") or "").strip()
        block = f"{prompt}\n{display}" if prompt and display else (prompt or display)

        solved = None
        for solver in _SOLVERS:
            solved = solver(block)
            if solved is not None:
                break
        answers.append(solved)
    return answers


def _seconds(value):
    if isinstance(value, bool):
        return 0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, math.ceil(seconds)) if math.isfinite(seconds) else 0


def cooldown_seconds(payload, default=0):
    """`cooldown` is a flag on some replies and a duration on cycle completion."""
    return (
        _seconds(payload.get("cooldown_remaining"))
        or _seconds(payload.get("cooldown"))
        or default
    )


def _index(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


PARSERS = {
    "typing": parse_typing_page,
    "emoji": parse_emoji_page,
    "math": parse_math_page,
}


@dataclass
class Round:
    """One opened game page. Its ticket and list stay valid across cycles."""

    game: str
    page: object
    index: int
    wait: int


class GameRunner:
    MAX_TRANSIENT_FAILURES = 5
    MAX_REAUTHENTICATIONS = 2
    MAX_COOLDOWN_REPLIES = 5
    MAX_REJECTIONS = 2
    MAX_RATE_LIMITS = 5
    RATE_LIMIT_DELAY = 30
    DEFAULT_CHALLENGE_DELAY = (6.0, 10.0)

    def __init__(
        self,
        client,
        *,
        max_wait=60,
        stop=None,
        reauthenticate=None,
        retry_delay=5,
        min_interval=0,
        max_failures=MAX_TRANSIENT_FAILURES,
        checkpoint=None,
        sleep=asyncio.sleep,
        log=lambda message: None,
        challenge_delay=DEFAULT_CHALLENGE_DELAY,
        on_verification=None,
    ):
        self.client = client
        self.max_wait = max_wait
        self.stop = stop or asyncio.Event()
        self.reauthenticate = reauthenticate
        self.retry_delay = retry_delay
        self.min_interval = min_interval
        self.max_failures = max_failures
        self.checkpoint = checkpoint
        self.sleep = sleep
        self.log = log
        self.challenge_delay = challenge_delay
        self.on_verification = on_verification or (lambda active, steps=0: None)

    async def _post(self, data, *, submission=False):
        try:
            response = await self.client.http.post(
                "/",
                data=data,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )
        except httpx.RequestError as exc:
            if submission:
                raise SubmissionOutcomeUnknown(
                    "Submission response was lost; the answer will not be replayed"
                ) from exc
            raise TransientGameError("Game request failed before a usable response") from exc
        if response.is_redirect or response.status_code in (401, 403):
            raise SessionExpired("Game request needs a fresh login and ticket")
        if response.status_code == 429:
            raise RateLimited(
                "Server rate limit (HTTP 429)", _seconds(response.headers.get("retry-after"))
            )
        if response.status_code >= 500:
            if submission:
                raise SubmissionOutcomeUnknown(
                    f"Submission returned HTTP {response.status_code}; "
                    "the answer will not be replayed"
                )
            raise TransientGameError(f"Game request returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            if submission:
                raise SubmissionOutcomeUnknown(
                    "Submission returned non-JSON; the answer will not be replayed"
                ) from None
            raise TransientGameError("Game request returned non-JSON") from None
        if not isinstance(payload, dict):
            raise GameError("Game request returned an unexpected payload")
        if "too many requests" in str(payload.get("error") or "").casefold():
            raise RateLimited(f"Server rate limit (HTTP {response.status_code})")
        if response.is_error and not payload.get("captcha_required"):
            message = str(payload.get("error") or f"HTTP {response.status_code}")
            raise GameError(message)
        return payload

    def _challenge_delay(self):
        """Return the seconds to wait before submitting a solved challenge."""
        spec = self.challenge_delay
        if spec is None:
            return 0.0
        if isinstance(spec, (int, float)):
            return max(0.0, float(spec))
        try:
            low, high = spec
            low = max(0.0, float(low))
            high = max(low, float(high))
        except (TypeError, ValueError):
            return 0.0
        return random.uniform(low, high)

    async def _answer_captcha(self, challenge):
        """Solve the challenge automatically. No manual input is ever requested."""
        automatic = solve_challenge_steps(challenge)
        steps = challenge.get("steps") if isinstance(challenge, dict) else None
        if (
            not isinstance(steps, list)
            or len(automatic) != len(steps)
            or any(answer is None or not str(answer).strip() for answer in automatic)
        ):
            self.log(f"Challenge not solvable: steps={steps!r} answers={automatic!r}")
            raise ManualCaptchaRequired(challenge)
        self.log(f"Challenge steps: {steps!r}")
        self.log(f"Challenge solved automatically: {automatic!r}")
        return captcha_token(challenge, automatic)

    async def _with_captcha(self, data, *, submission=False):
        payload = await self._post(data, submission=submission)
        verifying = False
        try:
            for _ in range(3):
                if not payload.get("captcha_required"):
                    return payload
                challenge = payload.get("captcha_challenge")
                steps = challenge.get("steps") if isinstance(challenge, dict) else None
                if not verifying:
                    self.on_verification(
                        True, len(steps) if isinstance(steps, list) else 0
                    )
                    verifying = True
                token = await self._answer_captcha(challenge)
                delay = self._challenge_delay()
                if delay and not await self._pause(delay):
                    raise TransientGameError("Stop requested before challenge was submitted")
                payload = await self._post(
                    {**data, "captcha_token": token},
                    submission=submission,
                )
                if payload.get("captcha_required"):
                    reply = {k: v for k, v in payload.items() if k != "captcha_challenge"}
                    self.log(f"Challenge answer rejected; server said: {reply!r}")
            raise GameError("Human verification failed repeatedly")
        finally:
            if verifying:
                self.on_verification(False, 0)

    async def _open(self, slug):
        try:
            response = await self.client.follow(
                await self.client.http.get(
                    f"/?start-game={slug}",
                    headers={
                        "Cache-Control": "no-store",
                        "Referer": "https://keycashv2.pro/?c=games",
                    },
                )
            )
            if response.status_code == 429:
                raise RateLimited(
                    "Server rate limit (HTTP 429)",
                    _seconds(response.headers.get("retry-after")),
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TransientGameError("Game page could not be loaded") from exc
        if self.client.is_login(response.url) or self.client.has_login_form(response.text):
            raise SessionExpired("Session expired while opening the game")
        return response.text

    async def _start(self, slug, ticket):
        payload = await self._with_captcha(
            {"game-start": "1", "slug": slug, "ticket": ticket}
        )
        if not payload.get("started"):
            raise GameError(str(payload.get("error") or "Server did not start the game"))
        return payload

    async def _begin(self, game):
        page = PARSERS[game](await self._open(game))
        started = await self._start(game, page.ticket)
        if game == "math":
            return Round(game, page, 0, cooldown_seconds(started, page.remaining))
        return Round(
            game,
            page,
            _index(started.get("next_index"), page.index),
            cooldown_seconds(started),
        )

    async def _pause(self, seconds):
        """Sleep, returning False as soon as a stop is requested."""
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            stopper.cancel()
        return not self.stop.is_set()

    async def _wait(self, seconds):
        seconds = max(0, int(seconds or 0))
        if seconds > self.max_wait:
            raise GameError(
                f"Server requires a {seconds}s wait, above the configured {self.max_wait}s maximum"
            )
        if self.checkpoint is not None:
            return await self.checkpoint(seconds)
        if seconds == 0:
            return not self.stop.is_set()
        return await self._pause(seconds)

    def _gap(self):
        return self.min_interval() if callable(self.min_interval) else self.min_interval

    async def _submit_typing(self, current):
        words = current.page.words
        if not 0 <= current.index < len(words):
            raise TransientGameError("Server moved past the fresh word list")
        answer = words[current.index]
        payload = await self._with_captcha(
            {
                "typing-solve": "1",
                "ticket": current.page.ticket,
                "word": answer,
            },
            submission=True,
        )
        return payload, answer

    async def _submit_emoji(self, current):
        emojis = current.page.emojis
        if not 0 <= current.index < len(emojis):
            raise TransientGameError("Server moved past the fresh emoji list")
        emoji = emojis[current.index]
        name = current.page.names[emoji]
        payload = await self._with_captcha(
            {
                "emoji-solve": "1",
                "ticket": current.page.ticket,
                "answer": name,
            },
            submission=True,
        )
        return payload, f"{emoji} {name}"

    async def _submit_math(self, current):
        ticket = current.page.ticket
        problem = await self._with_captcha({"math-problem": "1", "ticket": ticket})
        if not problem.get("success"):
            raise GameError(str(problem.get("error") or "Server did not issue a math problem"))
        token = problem.get("token")
        options = problem.get("options")
        if not token:
            raise GameError("Math problem has no token")
        if not isinstance(options, list) or not options:
            raise GameError("Math problem has no answer options")
        answer = solve_math_problem(
            problem.get("a"), problem.get("op"), problem.get("b"), options
        )
        payload = await self._with_captcha(
            {"math-solve": "1", "ticket": ticket, "token": token, "answer": answer},
            submission=True,
        )
        return payload, str(answer)

    @staticmethod
    def _next_wait(current, payload):
        if current.game == "math":
            return cooldown_seconds(payload, current.page.wait_time)
        if payload.get("cycle_complete") is True:
            return cooldown_seconds(payload, current.page.cooldown)
        return cooldown_seconds(payload)

    async def play(self, game, *, answers=None, cycles=None):
        """Yield one row per recorded answer until a limit is met or a stop is requested.

        With neither limit this runs until stopped. Like the website, it keeps one
        ticket and list across cycles and reopens the page only to recover.
        """
        if game not in PARSERS:
            raise GameError(f"Unsupported game: {game}")
        if (answers is not None and answers < 1) or (cycles is not None and cycles < 1):
            raise GameError("Answer and cycle limits must be at least one")
        submit = getattr(self, f"_submit_{game}")
        verdict = "success" if game == "math" else "correct"
        current = None
        wait = number = completed = 0
        failures = logins = cooldown_replies = rejections = limited = 0
        while True:
            try:
                if not await self._wait(wait):
                    return
                wait = 0
                if current is None:
                    current = await self._begin(game)
                    if not await self._wait(current.wait):
                        return
                payload, answer = await submit(current)
            except TransientGameError as exc:
                failures += 1
                if failures > self.max_failures:
                    raise GameError(f"Gave up after repeated failures: {exc}") from exc
                current = None
                wait = min(self.max_wait, 60, self.retry_delay * 2 ** (failures - 1))
                continue
            except RateLimited as exc:
                limited += 1
                if limited > self.MAX_RATE_LIMITS:
                    raise GameError(f"Gave up after repeated rate limits: {exc}") from exc
                backoff = self.RATE_LIMIT_DELAY * 2 ** (limited - 1)
                wait = min(self.max_wait, max(backoff, exc.retry_after))
                self.log(f"{exc}; waiting {wait}s")
                continue
            except SessionExpired:
                logins += 1
                if self.reauthenticate is None or logins > self.MAX_REAUTHENTICATIONS:
                    raise
                await self.reauthenticate()
                current = None
                continue
            if payload.get("error"):
                raise GameError(str(payload["error"]))
            correct = payload.get(verdict) is True
            remaining = _seconds(payload.get("cooldown_remaining"))
            if not correct and payload.get("cooldown") and remaining > 0:
                cooldown_replies += 1
                if cooldown_replies > self.MAX_COOLDOWN_REPLIES:
                    raise GameError("Server kept reporting a cooldown after it had elapsed")
                current.index = _index(payload.get("next_index"), current.index)
                wait = remaining
                continue
            cooldown_replies = 0
            row = self._row(number, correct, payload, answer)
            row["cycle_target"] = current.page.cycle_target
            number += 1
            if correct:
                failures = logins = rejections = limited = 0
                current.index = _index(payload.get("next_index"), current.index + 1)
                completed += payload.get("cycle_complete") is True
                wait = max(self._next_wait(current, payload), self._gap())
            else:
                rejections += 1
                wait = max(self._next_wait(current, payload), self._gap())
                current = None
            yield row
            if not correct and rejections >= self.MAX_REJECTIONS:
                raise GameError(f"Server rejected {rejections} answers in a row")
            if answers is not None and number >= answers:
                return
            if cycles is not None and completed >= cycles:
                return

    async def run(self, game, count):
        if count < 1:
            raise GameError("Answer count must be at least one")
        return [row async for row in self.play(game, answers=count)]

    @staticmethod
    def _row(number, correct, payload, answer=None):
        return {
            "answer_number": number + 1,
            "correct": correct,
            "answer": answer,
            "correct_count": payload.get("correct_count"),
            "completed_cycles": payload.get("completed_cycles"),
            "cycle_complete": payload.get("cycle_complete") is True,
            "earned": payload.get("added"),
            "wallet_total": payload.get("total"),
        }