"""SQLite access for the finals demo (S01).

- One connection per request, foreign keys on, rows as sqlite3.Row.
- Writes go through `tx()` so a failure rolls the whole change back.
- Parameterised SQL only; never build SQL from user input.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"

# Synthetic demo accounts. Login codes come from DEMO_LOGIN_CODES or are generated at start-up (see auth.py).
SEED_ACCOUNTS = [
    # login_name, role, display_name, is_demo_admin, case display_code, case label
    ("employer_a", "employer", "示範雇主 A（家庭看護）", 0, "DEMO-A01", "合成示範｜家庭看護"),
    ("employer_b", "employer", "示範雇主 B（產業）", 0, "DEMO-B01", "合成示範｜製造業"),
    ("officer", "officer", "示範承辦人", 1, None, None),
]


def db_path() -> Path:
    return Path(os.environ.get("ZP_DB_PATH") or HERE / "data" / "zhupinan.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # one connection per request, used sequentially; async handlers may run it on another thread
    con = sqlite3.connect(p, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    """Run a block in one transaction: commit on success, roll back on any exception."""
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def get_db():
    """FastAPI dependency: a connection for this request, always closed."""
    con = connect()
    con.isolation_level = None  # explicit transactions via tx()
    try:
        yield con
    finally:
        con.close()


def init_db() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        con.isolation_level = None
        with tx(con):
            seed_accounts(con)
            seed_cases(con)
    finally:
        con.close()


def seed_accounts(con: sqlite3.Connection) -> None:
    for login, role, name, admin, _, _ in SEED_ACCOUNTS:
        con.execute(
            "INSERT OR IGNORE INTO accounts(login_name, role, display_name, is_demo_admin, created_at) VALUES (?,?,?,?,?)",
            (login, role, name, admin, now()))


def seed_cases(con: sqlite3.Connection) -> None:
    for login, role, _, _, code, label in SEED_ACCOUNTS:
        if role != "employer":
            continue
        owner = con.execute("SELECT id FROM accounts WHERE login_name=?", (login,)).fetchone()["id"]
        con.execute(
            "INSERT OR IGNORE INTO cases(owner_id, display_code, label, created_at, updated_at) VALUES (?,?,?,?,?)",
            (owner, code, label, now(), now()))


CASE_DATA_TABLES = ("audit_events", "notification_log", "line_bindings", "proofs", "tasks", "documents", "cases")


def reset_demo(con: sqlite3.Connection) -> None:
    """Delete all case data, LINE bindings and notification logs, then rebuild the empty seed cases.
    Accounts, login codes and sessions are kept. Caller removes upload files after commit."""
    with tx(con):
        for t in CASE_DATA_TABLES:
            con.execute(f"DELETE FROM {t}")  # table names are constants, not user input
        seed_cases(con)


def audit(con: sqlite3.Connection, actor_id: int | None, case_id: int | None, entity_type: str,
          entity_id: str | int | None, action: str) -> None:
    con.execute(
        "INSERT INTO audit_events(actor_id, case_id, entity_type, entity_id, action, created_at) VALUES (?,?,?,?,?,?)",
        (actor_id, case_id, entity_type, None if entity_id is None else str(entity_id), action, now()))
