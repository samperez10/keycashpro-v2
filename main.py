#!/usr/bin/env python3
"""Keycash login utility with cookie reuse and verified-password reauthentication."""
import argparse
import asyncio
import getpass
import hashlib
import json
from pathlib import Path
import sys
import httpx
from keycash.client import AuthenticationError, KeycashClient
from keycash.credentials import CredentialStore
from keycash.locking import account_lock

ROOT = Path(__file__).resolve().parent


def account_directory(root, username):
    identifier = hashlib.sha256(username.strip().casefold().encode()).hexdigest()[:20]
    return Path(root) / "accounts" / identifier


async def run(args):
    username = args.username.strip()
    if not username:
        raise AuthenticationError("Enter a username.")
    state = account_directory(args.state_dir, username)
    with account_lock(state):
        credentials = CredentialStore(state)
        async with KeycashClient(state) as client:
            if args.command == "logout":
                await client.logout(local=args.local)
                if args.forget_password:
                    credentials.clear()
                return {
                    "username": username,
                    "local_session_cleared": True,
                    "password_forgotten": args.forget_password,
                }
            if not getattr(args, "force", False):
                profile = await client.restore(username)
                if profile:
                    return profile
            password = credentials.load(username)
            saved_password = password is not None
            if args.command == "status" and not password:
                return {
                    "username": username,
                    "authenticated": False,
                    "login_needed": True,
                    "automatic_relogin_available": False,
                }
            if not password:
                password = getpass.getpass("Password: ")
            try:
                profile = await client.login(username, password)
                credentials.save(username, password)
                if saved_password:
                    profile["reauthenticated"] = True
                return profile
            finally:
                password = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "status", "logout"):
        child = sub.add_parser(command)
        child.add_argument("--username", required=True)
        child.add_argument("--state-dir", type=Path, default=ROOT / ".state")
        if command == "login":
            child.add_argument("--force", action="store_true")
        if command == "logout":
            child.add_argument(
                "--local", action="store_true", help="Clear cookies without contacting the site"
            )
            child.add_argument(
                "--forget-password",
                action="store_true",
                help="Also remove the saved password used for automatic re-login",
            )
    try:
        result = asyncio.run(run(parser.parse_args()))
        print(json.dumps(result, indent=2))
    except (AuthenticationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Keycash request failed; check your connection and try again.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
