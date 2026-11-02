"""Prevent concurrent mutation of one account session."""
from contextlib import contextmanager
import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AccountBusy(RuntimeError):
    pass


@contextmanager
def account_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "keycash-account.lock"
    with path.open("a+b") as stream:
        if os.name == "nt":
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AccountBusy("This account is busy in another process") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AccountBusy("This account is busy in another process") from None
            yield
