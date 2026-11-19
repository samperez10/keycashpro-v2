#!/usr/bin/env python3
"""Solve one Keycash game for a set number of answers or cycles, or until stopped."""
import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import signal
import sys

import httpx

from keycash.client import AuthenticationError, KeycashClient
from keycash.credentials import CredentialStore
from keycash.games import GameError, GameRunner, SubmissionOutcomeUnknown
from keycash.locking import account_lock
from main import account_directory, ROOT


def request_stop_on_interrupt(stop):
    """First Ctrl+C stops at the next safe point; a second one quits immediately."""
    loop = asyncio.get_running_loop()

    def handler(*_):
        signal.signal(signal.SIGINT, signal.default_int_handler)
        print("\nStopping; press Ctrl+C again to quit now.", file=sys.stderr)
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handler)


def earned_total(rows):
    total = Decimal(0)
    for row in rows:
        try:
            total += Decimal(str(row["earned"] or 0))
        except InvalidOperation:
            pass
    return str(total)


def describe(row):
    line = f"#{row['answer_number']} {'correct' if row['correct'] else 'wrong'}"
    if row.get("answer"):
        line += f" · {row['answer']}"
    if row["cycle_complete"]:
        line += f" | cycle complete +{row['earned']} wallet {row['wallet_total']}"
    return line


async def run(args):
    state = account_directory(args.state_dir, args.username)
    with account_lock(state):
        credentials = CredentialStore(state)
        password = credentials.load(args.username)
        if not password:
            raise AuthenticationError("Run main.py login first to save verified credentials")
        async with KeycashClient(state) as client:
            profile = await client.restore(args.username)
            if not profile:
                profile = await client.login(args.username, password)

            async def reauthenticate():
                print("Session expired; signing in again.", file=sys.stderr)
                await client.login(args.username, password)

            stop = asyncio.Event()
            request_stop_on_interrupt(stop)
            runner = GameRunner(
                client,
                max_wait=args.max_wait,
                stop=stop,
                reauthenticate=reauthenticate,
                min_interval=args.min_interval,
                log=lambda message: print(message, file=sys.stderr, flush=True),
            )
            rows = []
            error = None
            exit_code = 0
            try:
                async for row in runner.play(
                    args.game,
                    answers=None if args.forever or args.cycles else args.answers,
                    cycles=args.cycles,
                ):
                    rows.append(row)
                    client.save()
                    print(describe(row), file=sys.stderr, flush=True)
            except SubmissionOutcomeUnknown as exc:
                error, exit_code = str(exc), 2
            except (AuthenticationError, GameError, httpx.HTTPError) as exc:
                error, exit_code = str(exc) or "Keycash request failed", 1
            finally:
                client.save()
            return exit_code, {
                "username": args.username,
                "game": args.game,
                "recorded_answers": len(rows),
                "cycles_completed": sum(row["cycle_complete"] for row in rows),
                "earned_total": earned_total(rows),
                "stopped_by_user": stop.is_set(),
                "error": error,
                "results": rows,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--game", choices=["typing", "math", "emoji"], required=True)
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--answers", type=int, default=1, help="Stop after N answers (default 1)")
    limit.add_argument("--cycles", type=int, help="Stop after N completed cycles")
    limit.add_argument("--forever", action="store_true", help="Run until stopped with Ctrl+C")
    parser.add_argument("--max-wait", type=int, default=60)
    parser.add_argument(
        "--min-interval", type=int, default=6, help="Minimum seconds between answers (default 6)"
    )
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    try:
        exit_code, result = asyncio.run(run(parser.parse_args()))
        print(json.dumps(result, indent=2))
        if result["error"]:
            print(result["error"], file=sys.stderr)
        return exit_code
    except (AuthenticationError, GameError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            wait = exc.response.headers.get("retry-after", "a few minutes")
            print(f"Keycash is rate limiting this connection; try again after {wait}.", file=sys.stderr)
            return 1
        print(f"Keycash request failed: HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Keycash request failed", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
