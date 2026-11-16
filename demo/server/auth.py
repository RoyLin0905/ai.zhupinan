"""Demo login, server-side sessions, roles and same-origin checks (S01).

Login: pre-built demo account + login code. Codes come from DEMO_LOGIN_CODES ("account:code,...", reusable,
for the team) or from `python auth.py issue <account>` (single use, for judges). Without DEMO_LOGIN_CODES the
server generates codes at start-up and prints them to its own console only.

Session: random token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 hash is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response

import db

COOKIE = "zp_session"
SESSION_HOURS = 8
MAX_FAILS = 5
FAIL_WINDOW = 10 * 60  # seconds; also the lock-out length
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- login codes
def load_env_codes(con: sqlite3.Connection) -> dict[str, str]:
    """Install reusable team codes. Returns {account: code} only for codes generated here (to print)."""
    raw = os.environ.get("DEMO_LOGIN_CODES", "").strip()
    generated = {}
    pairs = []
    if raw:
        for part in raw.split(","):
            if ":" in part:
                acct, code = part.split(":", 1)
                pairs.append((acct.strip(), code.strip()))
    else:
        for (acct,) in con.execute("SELECT login_name FROM accounts ORDER BY id").fetchall():
            code = secrets.token_urlsafe(6)
            pairs.append((acct, code))
            generated[acct] = code
    with db.tx(con):
        con.execute("DELETE FROM login_codes WHERE single_use = 0")  # env codes replace the previous set
        for acct, code in pairs:
            row = con.execute("SELECT id FROM accounts WHERE login_name=?", (acct,)).fetchone()
            if row and code:
                con.execute("INSERT INTO login_codes(account_id, code_hash, single_use, created_at) VALUES (?,?,0,?)",
                            (row["id"], h(code), db.now()))
    return generated


def issue_code(con: sqlite3.Connection, login_name: str, hours: int = 12) -> str:
    row = con.execute("SELECT id FROM accounts WHERE login_name=?", (login_name,)).fetchone()
    if not row:
        raise ValueError(f"unknown account {login_name}")
    code = secrets.token_urlsafe(6)
    with db.tx(con):
        con.execute("INSERT INTO login_codes(account_id, code_hash, single_use, expires_at, created_at) VALUES (?,?,1,?,?)",
                    (row["id"], h(code), _utc(datetime.now(timezone.utc) + timedelta(hours=hours)), db.now()))
    return code


# ---------------------------------------------------------------- brute-force limit (single demo process)
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def _key(request: Request, account: str) -> str:
    return f"{account}|{request.client.host if request.client else '-'}"


def _locked(key: str) -> bool:
    with _lock:
        recent = [t for t in _fails.get(key, []) if time.monotonic() - t < FAIL_WINDOW]
        _fails[key] = recent
        return len(recent) >= MAX_FAILS


def _fail(key: str) -> None:
    with _lock:
        _fails.setdefault(key, []).append(time.monotonic())


def login(con: sqlite3.Connection, request: Request, response: Response, account: str, code: str) -> sqlite3.Row:
    key = _key(request, account)
    if _locked(key):
        raise HTTPException(429, "嘗試次數過多，請 10 分鐘後再試")
    acct = con.execute("SELECT * FROM accounts WHERE login_name=?", (account,)).fetchone()
    match = None
    if acct:
        now = db.now()
        for row in con.execute(
                "SELECT id, code_hash, single_use FROM login_codes WHERE account_id=? AND used_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)", (acct["id"], now)).fetchall():
            if hmac.compare_digest(row["code_hash"], h(code)):
                match = row
                break
    if match is None:
        _fail(key)
        raise HTTPException(401, "帳號或登入碼不正確")  # same message whether or not the account exists
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    with db.tx(con):
        if match["single_use"]:
            con.execute("UPDATE login_codes SET used_at=? WHERE id=?", (db.now(), match["id"]))
        con.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now(),))
        con.execute("INSERT INTO sessions(id_hash, account_id, created_at, expires_at) VALUES (?,?,?,?)",
                    (h(token), acct["id"], db.now(), _utc(expires)))
        db.audit(con, acct["id"], None, "session", None, "login")
    with _lock:
        _fails.pop(key, None)
    response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")
    return acct


def logout(con: sqlite3.Connection, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        with db.tx(con):
            con.execute("DELETE FROM sessions WHERE id_hash=?", (h(token),))
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=_secure(request))


def _secure(request: Request) -> bool:
    return (os.environ.get("COOKIE_SECURE", "").lower() == "true" or request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https")


# ---------------------------------------------------------------- dependencies
def current_account(request: Request, con: sqlite3.Connection = Depends(db.get_db)) -> sqlite3.Row:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "請先登入")
    row = con.execute(
        "SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id WHERE s.id_hash=? AND s.expires_at > ?",
        (h(token), db.now())).fetchone()
    if row is None:
        raise HTTPException(401, "登入已過期，請重新登入")
    return row


def require_employer(acct: sqlite3.Row = Depends(current_account)) -> sqlite3.Row:
    if acct["role"] != "employer":
        raise HTTPException(403, "此功能僅限雇主帳號")
    return acct


def require_officer(acct: sqlite3.Row = Depends(current_account)) -> sqlite3.Row:
    if acct["role"] != "officer":
        raise HTTPException(403, "此功能僅限承辦人帳號")
    return acct


def require_demo_admin(acct: sqlite3.Row = Depends(current_account)) -> sqlite3.Row:
    if not acct["is_demo_admin"]:
        raise HTTPException(403, "僅示範管理帳號可重置資料")
    return acct


# ---------------------------------------------------------------- same-origin check for writes
def cross_site(request: Request) -> bool:
    """True when a state-changing request plainly comes from another site.
    Browsers send Origin on cross-origin POST/DELETE and Sec-Fetch-Site on all modern requests."""
    if request.method not in UNSAFE:
        return False
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return urlsplit(origin).netloc.lower() != host.lower()
    return False


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "issue":
        db.init_db()
        c = db.connect()
        c.isolation_level = None
        hours = int(sys.argv[3]) if len(sys.argv) > 3 else 12
        print(f"{sys.argv[2]} 一次性登入碼（{hours} 小時內有效）：{issue_code(c, sys.argv[2], hours)}")
        c.close()
    else:
        print("用法：python demo/server/auth.py issue <employer_a|employer_b|officer> [hours]")
