"""Passwords, login sessions and API keys."""
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import store

COOKIE = "at_session"
LOGIN_TTL = 30 * 86400


def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def new_login(user_id):
    token = secrets.token_urlsafe(32)
    now = time.time()
    store.ex("INSERT INTO logins(token_hash, user_id, created, expires) VALUES (?,?,?,?)", (sha(token), user_id, now, now + LOGIN_TTL))
    store.ex("DELETE FROM logins WHERE expires < ?", (now,))
    return token


def end_login(token):
    if token:
        store.ex("DELETE FROM logins WHERE token_hash=?", (sha(token),))


def current_user(request: Request, required=True):
    token = request.cookies.get(COOKIE)
    if token:
        r = store.q1("SELECT u.* FROM logins l JOIN users u ON u.id=l.user_id WHERE l.token_hash=? AND l.expires>?",
                     (sha(token), time.time()))
        if r:
            return dict(r)
    if required:
        raise HTTPException(401, "not logged in")
    return None


def new_api_key(user_id, name):
    key = "atk_" + secrets.token_urlsafe(30)
    store.ex("INSERT INTO api_keys(user_id, name, prefix, key_hash, created) VALUES (?,?,?,?,?)",
             (user_id, name, key[:12], sha(key), time.time()))
    return key


def agent_key(request: Request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(401, "missing API key")
    key = h[7:].strip()
    r = store.q1("SELECT * FROM api_keys WHERE key_hash=? AND revoked IS NULL", (sha(key),))
    if not r:
        raise HTTPException(401, "invalid or revoked API key")
    now = time.time()
    if not r["last_used"] or now - r["last_used"] > 60:
        store.ex("UPDATE api_keys SET last_used=? WHERE id=?", (now, r["id"]))
    return dict(r)
