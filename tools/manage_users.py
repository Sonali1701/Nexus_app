"""Build the APP_USERS value for team logins (with hashed passwords).

Usage:
  python tools/manage_users.py add alice "Alice Smith"      # prompts for password
  python tools/manage_users.py add bob "Bob Lee" --password s3cret
  python tools/manage_users.py hash                          # just hash one password

'add' prints (and can merge into) an APP_USERS JSON blob you paste into .env or
Render's environment. Existing APP_USERS in the environment is used as the base
so you can add users incrementally.
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth


def _existing_users():
    raw = os.getenv("APP_USERS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add/replace a user and print APP_USERS")
    a.add_argument("username")
    a.add_argument("name", nargs="?", default="")
    a.add_argument("--password", help="password (omit to be prompted)")

    sub.add_parser("hash", help="hash a single password and print it")

    args = ap.parse_args()

    if args.cmd == "hash":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Confirm: "):
            print("Passwords do not match."); return 1
        print(auth.hash_password(pw))
        return 0

    pw = args.password or getpass.getpass(f"Password for {args.username}: ")
    if not args.password:
        if pw != getpass.getpass("Confirm: "):
            print("Passwords do not match."); return 1
    if not pw:
        print("Password cannot be empty."); return 1

    users = _existing_users()
    users[args.username] = {
        "name": args.name or args.username,
        "password": auth.hash_password(pw),
    }
    blob = json.dumps(users, separators=(",", ":"))
    print("\nUser added. Set this as APP_USERS (in .env locally, or Render env):\n")
    print("APP_USERS=" + blob)
    print("\n(Contains hashed passwords — safe to store; plaintext is never kept.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
