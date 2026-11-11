"""Mock eQMS: a fake system of record holding SYNTHETIC deviation records.

record_kind = 'template' (blank form, no event data) or 'record' (an executed, filled-in
record). Records have status draft | open | closed. Seed records are all closed."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATUSES = {"draft", "open", "closed"}


class EqmsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_db_path() -> str:
    return os.environ.get("EQMS_DB", str(DATA_DIR / "eqms.db"))


class Eqms:
    def __init__(self, db_path: str | None = None, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.db_path = db_path or default_db_path()
        self.form = json.loads((self.data_dir / "deviation_form.json").read_text(encoding="utf-8"))
        seed = json.loads((self.data_dir / "deviations_seed.json").read_text(encoding="utf-8"))
        for doc in (self.form, seed):
            if doc.get("synthetic") is not True:
                raise ValueError("eQMS data must be labelled synthetic")
        if self.form.get("record_kind") != "template":
            raise ValueError("deviation_form.json must be a template")
        c = self._conn()
        try:
            with c:
                c.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, record_kind TEXT NOT NULL,"
                          " status TEXT NOT NULL, event_type TEXT, severity TEXT, site TEXT, created_at TEXT NOT NULL,"
                          " payload TEXT NOT NULL)")
                self._put(c, {**self.form, "id": self.form["template_id"], "status": "effective"}, template=True)
                for r in seed["records"]:
                    self._put(c, r)
        finally:
            c.close()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _put(c, r: dict, template: bool = False):
        c.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?)",
                  (r["id"], "template" if template else "record", r["status"], r.get("event_type"),
                   r.get("severity"), r.get("site"), datetime.now(timezone.utc).isoformat(), json.dumps(r)))

    # ---- read side (IK-02) ----
    def list_records(self, event_type=None, severity=None, status=None, site=None, limit=100) -> list[dict]:
        q, a = "SELECT payload FROM records WHERE record_kind='record'", []
        for col, v in (("event_type", event_type), ("severity", severity), ("status", status), ("site", site)):
            if v:
                q += f" AND {col}=?"
                a.append(v)
        c = self._conn()
        try:
            return [json.loads(r["payload"]) for r in c.execute(q + " ORDER BY id LIMIT ?", (*a, limit))]
        finally:
            c.close()

    def get_record(self, rid: str) -> dict:
        c = self._conn()
        try:
            r = c.execute("SELECT payload FROM records WHERE id=? AND record_kind='record'", (rid,)).fetchone()
        finally:
            c.close()
        if not r:
            raise EqmsError(f"record {rid} not found", 404)
        return json.loads(r["payload"])

    def list_templates(self) -> list[dict]:
        c = self._conn()
        try:
            return [json.loads(r["payload"]) for r in
                    c.execute("SELECT payload FROM records WHERE record_kind='template' ORDER BY id")]
        finally:
            c.close()

    # ---- write side (IK-09, Should priority) ----
    def required_fields(self) -> list[str]:
        return [f["name"] for f in self.form["fields"] if f["required"]]

    def write_record(self, record: dict, human_confirmed: bool, confirmed_by: str) -> dict:
        """Store a NEW draft record. Refused unless a human has explicitly confirmed it.
        The eQMS never submits or closes anything on its own."""
        if human_confirmed is not True or not (confirmed_by or "").strip():
            raise EqmsError("human confirmation required: set human_confirmed=true and confirmed_by "
                            "(nothing is written without it)", 409)
        if record.get("record_kind", "record") != "record":
            raise EqmsError("only executed records can be written, not templates", 400)
        missing = [f for f in self.required_fields() if not str(record.get(f, "")).strip()]
        if missing:
            raise EqmsError(f"missing required fields: {missing}", 422)
        enums = self.form["enums"]
        for f, allowed in enums.items():
            if record[f] not in allowed:
                raise EqmsError(f"{f} must be one of {allowed}", 422)
        c = self._conn()
        try:
            with c:
                n = c.execute("SELECT COUNT(*) FROM records WHERE id LIKE 'SYN-DEV-DRAFT-%'").fetchone()[0] + 1
                rid = f"SYN-DEV-DRAFT-{n:04d}"
                stored = {**{k: v for k, v in record.items() if k not in ("id", "status", "closed_date", "capa")},
                          "id": rid, "version": "0.1", "record_kind": "record", "status": "draft", "synthetic": True,
                          "confirmed_by": confirmed_by.strip(),
                          "confirmed_at": datetime.now(timezone.utc).isoformat()}
                self._put(c, stored)
        finally:
            c.close()
        return stored
