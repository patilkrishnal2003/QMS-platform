"""Append-only, hash-chained audit log. UPDATE/DELETE are blocked by triggers and
verify_chain() detects tampering made by bypassing them."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL,
  detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""
GENESIS = "0" * 64


def _hash(prev, ts, actor, action, subject, detail) -> str:
    return hashlib.sha256("|".join([prev, ts, actor, action, subject, detail]).encode()).hexdigest()


def append(conn: sqlite3.Connection, actor: str, action: str, subject: str, detail: dict) -> int:
    """Append inside the caller's transaction so the audit row commits with the change."""
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row[0] if row else GENESIS
    ts = datetime.now(timezone.utc).isoformat()
    d = json.dumps(detail, sort_keys=True, default=str)
    cur = conn.execute(
        "INSERT INTO audit(ts,actor,action,subject,detail,prev_hash,hash) VALUES (?,?,?,?,?,?,?)",
        (ts, actor, action, subject, d, prev, _hash(prev, ts, actor, action, subject, d)))
    return cur.lastrowid


def entries(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY seq")]


def verify_chain(conn: sqlite3.Connection) -> bool:
    prev = GENESIS
    for r in entries(conn):
        if r["prev_hash"] != prev or r["hash"] != _hash(prev, r["ts"], r["actor"], r["action"], r["subject"], r["detail"]):
            return False
        prev = r["hash"]
    return True
