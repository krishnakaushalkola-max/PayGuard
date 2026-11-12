"""SQLite storage for PayGuard."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '[]',
    unmatched_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    total_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    parser TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    paypal_order_id TEXT NOT NULL DEFAULT '',
    approve_url TEXT NOT NULL DEFAULT '',
    capture_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY,
    request_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""

UPDATABLE = {"items_json", "unmatched_json", "summary", "total_cents", "status", "decision",
             "reasons_json", "parser", "approved_by", "paypal_order_id", "approve_url", "capture_id"}


def db_path() -> str:
    return os.environ.get("DB_PATH", "payguard.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)


def _row(r) -> dict:
    d = dict(r)
    d["items"] = json.loads(d.pop("items_json"))
    d["unmatched"] = json.loads(d.pop("unmatched_json"))
    d["reasons"] = json.loads(d.pop("reasons_json"))
    return d


def create_request(text, items, unmatched, summary, total_cents, status, decision, reasons, parser) -> int:
    now = int(time.time())
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO requests (text, items_json, unmatched_json, summary, total_cents, status, decision,"
            " reasons_json, parser, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (text, json.dumps(items), json.dumps(unmatched), summary, total_cents, status, decision,
             json.dumps(reasons), parser, now, now))
        return cur.lastrowid


def get_request(req_id: int):
    with get_db() as db:
        r = db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return _row(r) if r else None


def get_request_by_order(order_id: str):
    if not order_id:
        return None
    with get_db() as db:
        r = db.execute("SELECT * FROM requests WHERE paypal_order_id=?", (order_id,)).fetchone()
    return _row(r) if r else None


def list_requests(limit: int = 30) -> list:
    with get_db() as db:
        rows = db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def update_request(req_id: int, **fields) -> None:
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    with get_db() as db:
        db.execute(f"UPDATE requests SET {sets} WHERE id=?",
                   (*fields.values(), int(time.time()), req_id))


def transition(req_id: int, from_status: str, **fields) -> bool:
    """Atomically change status only if the request is currently in from_status."""
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    with get_db() as db:
        cur = db.execute(f"UPDATE requests SET {sets} WHERE id=? AND status=?",
                         (*fields.values(), int(time.time()), req_id, from_status))
        return cur.rowcount == 1


def add_event(request_id, actor: str, event: str, detail: str = "") -> None:
    with get_db() as db:
        db.execute("INSERT INTO events (request_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
                   (request_id, int(time.time()), actor, event, detail))


def list_events(limit: int = 60) -> list:
    with get_db() as db:
        rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def spent_today_cents() -> int:
    now = int(time.time())
    start = now - (now % 86400)  # start of the current UTC day
    with get_db() as db:
        r = db.execute("SELECT COALESCE(SUM(total_cents),0) AS s FROM requests "
                       "WHERE status='paid' AND updated_at>=?", (start,)).fetchone()
    return int(r["s"])


def create_nonce(nonce: str, request_id: int, ttl: int) -> int:
    now = int(time.time())
    with get_db() as db:
        db.execute("DELETE FROM nonces WHERE expires_at < ?", (now - 3600,))
        db.execute("INSERT INTO nonces (nonce, request_id, expires_at, used) VALUES (?,?,?,0)",
                   (nonce, request_id, now + ttl))
    return now + ttl


def consume_nonce(nonce: str, request_id: int) -> bool:
    """One-time use, must be unexpired and issued for this exact request."""
    now = int(time.time())
    with get_db() as db:
        cur = db.execute("UPDATE nonces SET used=1 WHERE nonce=? AND request_id=? AND used=0 AND expires_at>=?",
                         (nonce, request_id, now))
        return cur.rowcount == 1
