"""One account's continuous solving run, with pause, stop, countdowns and rests."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from typing import Callable

from .games import GameRunner, SubmissionOutcomeUnknown

MAX_SERVER_WAIT = 3600


@dataclass
class Control:
    paused: bool = False
    stopped: bool = False


@dataclass
class Job:
    client: object
    game: str
    control: Control
    pacer: object
    reauthenticate: Callable
    should_continue: Callable
    on_row: Callable
    on_wait: Callable
    on_wallet: Callable
    log: Callable
    network_retries: int = 3
    retry_delay: float = 5
    on_unknown: str = "stop"
    clock: Callable = time.monotonic
    sleep: Callable = asyncio.sleep
    on_verification: Callable = lambda active, steps=0: None


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def amount_text(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(amount) if amount.is_finite() and amount >= 0 else None


def record(index, row):
    return {
        "index": index,
        "verdict": "correct" if row["correct"] else "incorrect",
        "answer": row.get("answer"),
        "decided_at": timestamp(),
        "earned_points": amount_text(row["earned"]) if row["correct"] else None,
        "batch_points": row["correct_count"],
        "batch_size": row["cycle_target"],
    }


async def run(job):
    control, pacer = job.control, job.pacer

    def allowed():
        return not control.stopped and job.should_continue()

    async def wait(seconds, status, rest=False):
        remaining = max(0, seconds)
        try:
            while True:
                if not allowed():
                    return False
                paused = control.paused
                job.on_wait(status, remaining)
                if not paused and remaining <= 0:
                    return True
                before = job.clock()
                await job.sleep(min(remaining, 0.5) if remaining > 0 and not paused else 0.2)
                elapsed = job.clock() - before
                if not paused or rest:
                    remaining = max(0, remaining - elapsed)
                if paused and not rest:
                    pacer.pause_work_clock(elapsed)
        finally:
            job.on_wait("Paused" if control.paused else "Running", 0)

    async def checkpoint(seconds):
        if not await wait(seconds, "Cooldown"):
            return False
        if pacer.rest_due():
            if not await wait(pacer.rest_seconds(), "Resting", rest=True):
                return False
            pacer.rests_completed += 1
            pacer.begin_work()
        return True

    runner = GameRunner(
        job.client,
        max_wait=MAX_SERVER_WAIT,
        reauthenticate=job.reauthenticate,
        retry_delay=job.retry_delay,
        min_interval=pacer.delay,
        max_failures=job.network_retries,
        checkpoint=checkpoint,
        log=job.log,
        on_verification=job.on_verification,
    )
    index = 0
    pacer.begin_work()
    while True:
        try:
            async for row in runner.play(job.game):
                index += 1
                job.client.save()
                job.on_row(record(index, row))
                wallet = amount_text(row["wallet_total"])
                if wallet is not None:
                    job.on_wallet(wallet)
                if not allowed():
                    return
            return
        except SubmissionOutcomeUnknown:
            index += 1
            job.client.save()
            job.on_row(
                {
                    "index": index,
                    "verdict": "submission_outcome_unknown",
                    "decided_at": timestamp(),
                    "earned_points": None,
                    "batch_points": None,
                    "batch_size": None,
                }
            )
            if job.on_unknown != "continue":
                raise
            if not await wait(job.retry_delay, "Retrying connection"):
                return
