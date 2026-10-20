#!/usr/bin/env python3
"""Create the service's admin account (run once when setting the service up; self-registration never creates admins).

usage (on the server, as root):
  sudo -u aituner TUNER_DB=/var/lib/aituner/tuner.db /opt/aituner/venv/bin/python /opt/aituner/server/deploy/create_admin.py admin@example.com

The password is read from the terminal (or AITUNER_ADMIN_PASSWORD). An existing account with that email is promoted to
admin and keeps its password unless --reset-password is given. The admin then sets the service's OpenRouter key on the
Account page; users need no key.
"""
import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import auth, store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--name", default="Administrator")
    ap.add_argument("--reset-password", action="store_true")
    a = ap.parse_args()
    store.init()
    email = a.email.strip().lower()
    u = store.q1("SELECT id FROM users WHERE email=?", (email,))
    pw = None
    if not u or a.reset_password:
        pw = os.environ.get("AITUNER_ADMIN_PASSWORD") or getpass.getpass(f"password for {email}: ")
        if len(pw) < 8:
            sys.exit("password must have at least 8 characters")
    if u:
        store.ex("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
        if pw:
            store.ex("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(pw), u["id"]))
        print(f"{email} is an admin (user {u['id']})")
    else:
        uid = store.ex("INSERT INTO users(email, name, pw_hash, created, is_admin) VALUES (?,?,?,?,1)",
                       (email, a.name, auth.hash_password(pw), time.time()))
        print(f"admin {email} created (user {uid})")


if __name__ == "__main__":
    main()
