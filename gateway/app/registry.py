"""Source Registry: CRUD for regulatory sources with a two-person rule.
Adding, changing or retiring a source is only a *proposal*; it becomes active when a
different steward/QA approves it. Every step is written to the audit log."""
import json
import os
import sqlite3
from pathlib import Path

from . import audit
from .allowlist import UrlNotAllowed, check_url
from .auth import APPROVER_ROLES, User

FREQUENCIES = {"manual", "daily", "weekly", "monthly"}
LEGAL_WEIGHTS = {"regulation", "guidance", "standard"}
FIELDS = ("name", "url", "jurisdiction", "product_types", "document_types",
          "check_frequency", "legal_weight", "auto_accept")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,              -- pending | active | retired | rejected
  name TEXT, url TEXT, jurisdiction TEXT, product_types TEXT, document_types TEXT,
  check_frequency TEXT, legal_weight TEXT, auto_accept INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  action TEXT NOT NULL,              -- add | edit | retire
  proposed TEXT NOT NULL, before TEXT,
  state TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_by TEXT, decided_at TEXT, note TEXT);
"""


class RegistryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_db_path() -> str:
    return os.environ.get("GATEWAY_DB", str(Path(__file__).resolve().parent.parent / "data" / "gateway.db"))


def _validate(f: dict) -> dict:
    missing = [k for k in FIELDS if k not in f]
    if missing:
        raise RegistryError(f"missing fields: {missing}")
    try:
        check_url(f["url"])
    except UrlNotAllowed as e:
        raise RegistryError(f"source url rejected by allow-list: {e}") from e
    if not str(f["name"]).strip() or not str(f["jurisdiction"]).strip():
        raise RegistryError("name and jurisdiction are required")
    if f["check_frequency"] not in FREQUENCIES:
        raise RegistryError(f"check_frequency must be one of {sorted(FREQUENCIES)}")
    if f["legal_weight"] not in LEGAL_WEIGHTS:
        raise RegistryError(f"legal_weight must be one of {sorted(LEGAL_WEIGHTS)}")
    for k in ("product_types", "document_types"):
        if not isinstance(f[k], list) or not all(isinstance(x, str) for x in f[k]):
            raise RegistryError(f"{k} must be a list of strings")
    return {**{k: f[k] for k in FIELDS}, "auto_accept": bool(f["auto_accept"])}


def _row_fields(r) -> dict:
    d = {k: r[k] for k in FIELDS}
    d["product_types"] = json.loads(d["product_types"] or "[]")
    d["document_types"] = json.loads(d["document_types"] or "[]")
    d["auto_accept"] = bool(d["auto_accept"])
    return d


class Registry:
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = self.conn()
        try:
            with c:
                c.executescript(SCHEMA + audit.SCHEMA)
        finally:
            c.close()

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _tx(self):
        """Context manager: one connection, one transaction, always closed."""
        reg = self

        class _Tx:
            def __enter__(self):
                self.c = reg.conn()
                return self.c

            def __exit__(self, et, ev, tb):
                try:
                    (self.c.rollback if et else self.c.commit)()
                finally:
                    self.c.close()
                return False

        return _Tx()

    # ---- reads ----
    def _source(self, c, sid: int) -> dict:
        r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if not r:
            raise RegistryError(f"source {sid} not found", 404)
        d = {"id": r["id"], "status": r["status"], **_row_fields(r)}
        ch = c.execute("SELECT * FROM changes WHERE source_id=? AND state='pending'", (sid,)).fetchone()
        d["pending_change"] = None if not ch else {
            "id": ch["id"], "action": ch["action"], "proposed_by": ch["proposed_by"],
            "proposed": json.loads(ch["proposed"])}
        return d

    def get_source(self, sid: int) -> dict:
        with self._tx() as c:
            return self._source(c, sid)

    def list_sources(self, status: str | None = None) -> list[dict]:
        with self._tx() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM sources" + (" WHERE status=?" if status else "") + " ORDER BY id",
                (status,) if status else ())]
            return [self._source(c, i) for i in ids]

    def list_changes(self, state: str | None = None) -> list[dict]:
        with self._tx() as c:
            q = "SELECT * FROM changes" + (" WHERE state=?" if state else "") + " ORDER BY id"
            return [{**dict(r), "proposed": json.loads(r["proposed"]),
                     "before": json.loads(r["before"]) if r["before"] else None}
                    for r in c.execute(q, (state,) if state else ())]

    def audit_entries(self) -> list[dict]:
        with self._tx() as c:
            return audit.entries(c)

    def audit_find(self, subject: str, action: str | None = None) -> list[dict]:
        with self._tx() as c:
            q, a = "SELECT * FROM audit WHERE subject=?", [subject]
            if action:
                q += " AND action=?"
                a.append(action)
            return [dict(r) for r in c.execute(q + " ORDER BY seq", a)]

    def verify_audit(self) -> bool:
        with self._tx() as c:
            return audit.verify_chain(c)

    # ---- proposals ----
    @staticmethod
    def _need_role(user: User):
        if user.role not in APPROVER_ROLES:
            raise RegistryError(f"role {user.role!r} may not propose or approve source changes", 403)

    @staticmethod
    def _store(c, sid, f):
        c.execute("UPDATE sources SET name=?,url=?,jurisdiction=?,product_types=?,document_types=?,"
                  "check_frequency=?,legal_weight=?,auto_accept=? WHERE id=?",
                  (f["name"], f["url"], f["jurisdiction"], json.dumps(f["product_types"]),
                   json.dumps(f["document_types"]), f["check_frequency"], f["legal_weight"],
                   int(f["auto_accept"]), sid))

    def propose_add(self, fields: dict, user: User) -> dict:
        self._need_role(user)
        f = _validate(fields)
        with self._tx() as c:
            if c.execute("SELECT 1 FROM sources WHERE url=? AND status IN ('pending','active')", (f["url"],)).fetchone():
                raise RegistryError("a pending or active source with this url already exists", 409)
            sid = c.execute("INSERT INTO sources(status) VALUES ('pending')").lastrowid
            self._store(c, sid, f)
            cid = c.execute("INSERT INTO changes(source_id,action,proposed,proposed_by) VALUES (?,?,?,?)",
                            (sid, "add", json.dumps(f), user.name)).lastrowid
            audit.append(c, user.name, "source.propose_add", f"source:{sid}", {"change": cid, "fields": f})
            return self._source(c, sid)

    def propose_edit(self, sid: int, patch: dict, user: User) -> dict:
        self._need_role(user)
        with self._tx() as c:
            cur = self._source(c, sid)
            if cur["status"] != "active":
                raise RegistryError("only active sources can be edited", 409)
            if cur["pending_change"]:
                raise RegistryError("source already has a pending change", 409)
            before = {k: cur[k] for k in FIELDS}
            f = _validate({**before, **{k: v for k, v in patch.items() if k in FIELDS}})
            if f == before:
                raise RegistryError("no change proposed")
            cid = c.execute("INSERT INTO changes(source_id,action,proposed,before,proposed_by) VALUES (?,?,?,?,?)",
                            (sid, "edit", json.dumps(f), json.dumps(before), user.name)).lastrowid
            audit.append(c, user.name, "source.propose_edit", f"source:{sid}", {"change": cid, "before": before, "after": f})
            return self._source(c, sid)

    def propose_retire(self, sid: int, user: User) -> dict:
        self._need_role(user)
        with self._tx() as c:
            cur = self._source(c, sid)
            if cur["status"] != "active":
                raise RegistryError("only active sources can be retired", 409)
            if cur["pending_change"]:
                raise RegistryError("source already has a pending change", 409)
            before = {k: cur[k] for k in FIELDS}
            cid = c.execute("INSERT INTO changes(source_id,action,proposed,before,proposed_by) VALUES (?,?,?,?,?)",
                            (sid, "retire", json.dumps(before), json.dumps(before), user.name)).lastrowid
            audit.append(c, user.name, "source.propose_retire", f"source:{sid}", {"change": cid})
            return self._source(c, sid)

    def decide(self, change_id: int, approve: bool, user: User, note: str = "") -> dict:
        self._need_role(user)
        with self._tx() as c:
            ch = c.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
            if not ch:
                raise RegistryError(f"change {change_id} not found", 404)
            if ch["state"] != "pending":
                raise RegistryError("change already decided", 409)
            if ch["proposed_by"] == user.name:
                raise RegistryError("two-person rule: the proposer cannot decide their own change", 403)
            sid, action = ch["source_id"], ch["action"]
            proposed = json.loads(ch["proposed"])
            c.execute("UPDATE changes SET state=?,decided_by=?,decided_at=datetime('now'),note=? WHERE id=?",
                      ("approved" if approve else "rejected", user.name, note, change_id))
            if approve:
                if action == "retire":
                    c.execute("UPDATE sources SET status='retired' WHERE id=?", (sid,))
                else:
                    self._store(c, sid, proposed)
                    c.execute("UPDATE sources SET status='active' WHERE id=?", (sid,))
            elif action == "add":
                c.execute("UPDATE sources SET status='rejected' WHERE id=?", (sid,))
            audit.append(c, user.name, "source.approve" if approve else "source.reject", f"source:{sid}",
                         {"change": change_id, "action": action, "note": note, "proposed_by": ch["proposed_by"]})
            return self._source(c, sid)

    def log(self, actor: str, action: str, subject: str, detail: dict) -> None:
        with self._tx() as c:
            audit.append(c, actor, action, subject, detail)
