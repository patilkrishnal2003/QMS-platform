"""Versioned library store + steward review queue (SQLite, stdlib only).

Item lifecycle: ingest -> queue_state 'pending' -> accepted | rejected.
Accepting a changed item makes it current and marks the previous current item
status='superseded'. Nothing is ever deleted or overwritten: every retrieved
version is kept with its checksum."""
import difflib
import hashlib
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DOC_STATUSES = {"draft", "final", "withdrawn", "superseded"}
LEGAL_WEIGHTS = {"regulation", "guidance", "standard"}
# full_text = complete document text; metadata_only = source gave only title/date/docket/abstract/URL
CONTENT_KINDS = {"full_text", "metadata_only"}
REVIEWERS = {"steward-a", "steward-b", "qa-1"}  # SIMULATED roles, mirrors the gateway user table
AUTO_ACTOR = "auto-accept-rule"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url TEXT NOT NULL, source_id INTEGER, fetched_url TEXT,
  version_no INTEGER NOT NULL,
  title TEXT, text TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, document_date TEXT, version_or_docket TEXT,
  status TEXT NOT NULL, checksum TEXT NOT NULL, legal_weight TEXT NOT NULL,
  content_kind TEXT NOT NULL DEFAULT 'full_text',
  queue_state TEXT NOT NULL,         -- pending | accepted | rejected
  is_current INTEGER NOT NULL DEFAULT 0,
  previous_item_id INTEGER, submitted_by TEXT,
  decided_by TEXT, decided_at TEXT, decision_note TEXT, auto_accepted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_url, version_no));
CREATE INDEX IF NOT EXISTS items_src ON items(source_url);
"""


_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "is", "are", "for", "be", "by", "on", "with", "as",
         "at", "that", "this", "it", "from", "was", "were", "not", "shall"}


def stem(w: str) -> str:
    """Light word-form normaliser so 'reported / reporting / reports / report' (and 'require / requires /
    required') match. Deliberately simple and deterministic; numbers and short words are left alone."""
    if len(w) <= 3 or w[0].isdigit() or "." in w:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        w = w[:-1]
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def _raw_tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _tokens(text: str) -> list[str]:
    return [stem(t) for t in _raw_tokens(text)]


class LibraryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def checksum(text: str) -> str:
    """SHA-256 of the text with line endings normalised (so CRLF vs LF is not a 'change')."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def default_db_path() -> str:
    return os.environ.get("LIBRARY_DB", str(Path(__file__).resolve().parent.parent / "data" / "library.db"))


def redline(old: str | None, new: str) -> dict:
    """Unified diff plus a structured line list (op = ctx | add | del) for a UI to render."""
    a = (old or "").replace("\r\n", "\n").splitlines()
    b = new.replace("\r\n", "\n").splitlines()
    unified = "\n".join(difflib.unified_diff(a, b, "current", "proposed", lineterm="", n=2))
    lines = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            lines += [{"op": "ctx", "text": t} for t in a[i1:i2]]
        else:
            lines += [{"op": "del", "text": t} for t in a[i1:i2]]
            lines += [{"op": "add", "text": t} for t in b[j1:j2]]
    return {"unified": unified, "lines": lines,
            "added": sum(1 for x in lines if x["op"] == "add"),
            "removed": sum(1 for x in lines if x["op"] == "del")}


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = self._open()
        try:
            with c:
                c.executescript(SCHEMA)
                cols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
                if "content_kind" not in cols:  # DB created before this column existed
                    c.execute("ALTER TABLE items ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'full_text'")
        finally:
            c.close()

    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def _run(self, fn):
        c = self._open()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                c.execute("COMMIT")
                return out
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    @staticmethod
    def _meta(r, with_text=False) -> dict:
        d = {k: r[k] for k in r.keys() if k != "text"}
        d["is_current"] = bool(d["is_current"])
        d["auto_accepted"] = bool(d["auto_accepted"])
        if with_text:
            d["text"] = r["text"]
        return d

    @staticmethod
    def _current(c, source_url):
        return c.execute("SELECT * FROM items WHERE source_url=? AND is_current=1", (source_url,)).fetchone()

    # ---- ingest ----
    def ingest(self, p: dict) -> dict:
        for k in ("source_url", "text", "retrieved_at", "legal_weight"):
            if not p.get(k):
                raise LibraryError(f"missing field: {k}")
        status = p.get("status", "final")
        if status not in DOC_STATUSES:
            raise LibraryError(f"status must be one of {sorted(DOC_STATUSES)}")
        if p["legal_weight"] not in LEGAL_WEIGHTS:
            raise LibraryError(f"legal_weight must be one of {sorted(LEGAL_WEIGHTS)}")
        content_kind = p.get("content_kind", "full_text")
        if content_kind not in CONTENT_KINDS:
            raise LibraryError(f"content_kind must be one of {sorted(CONTENT_KINDS)}")
        text = p["text"].replace("\r\n", "\n")
        digest = checksum(text)

        def tx(c):
            url = p["source_url"]
            cur = self._current(c, url)
            if cur and cur["checksum"] == digest:
                return {"result": "unchanged", "item_id": cur["id"], "version_no": cur["version_no"]}
            dup = c.execute("SELECT id, version_no FROM items WHERE source_url=? AND checksum=? AND queue_state='pending'",
                            (url, digest)).fetchone()
            if dup:
                return {"result": "already_queued", "item_id": dup["id"], "version_no": dup["version_no"]}
            vno = (c.execute("SELECT COALESCE(MAX(version_no),0) FROM items WHERE source_url=?", (url,)).fetchone()[0]) + 1
            iid = c.execute(
                "INSERT INTO items(source_url,source_id,fetched_url,version_no,title,text,retrieved_at,document_date,"
                "version_or_docket,status,checksum,legal_weight,content_kind,queue_state,previous_item_id,submitted_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (url, p.get("source_id"), p.get("fetched_url"), vno, p.get("title"), text, p["retrieved_at"],
                 p.get("document_date"), p.get("version_or_docket"), status, digest, p["legal_weight"], content_kind,
                 cur["id"] if cur else None, p.get("submitted_by"))).lastrowid
            kind = "changed" if cur else "new"
            if p.get("auto_accept"):
                self._accept(c, iid, AUTO_ACTOR, f"auto-accepted ({kind}) by approved per-source rule", auto=True)
                return {"result": f"auto_accepted_{kind}", "item_id": iid, "version_no": vno}
            return {"result": f"queued_{kind}", "item_id": iid, "version_no": vno}

        return self._run(tx)

    # ---- review queue ----
    def queue(self, state: str = "pending") -> list[dict]:
        c = self._open()
        try:
            return [self._meta(r) | {"change_kind": "changed" if r["previous_item_id"] else "new"}
                    for r in c.execute("SELECT * FROM items WHERE queue_state=? ORDER BY id", (state,))]
        finally:
            c.close()

    def item(self, item_id: int, with_text: bool = True) -> dict:
        c = self._open()
        try:
            r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                raise LibraryError(f"item {item_id} not found", 404)
            return self._meta(r, with_text)
        finally:
            c.close()

    def redline_for(self, item_id: int) -> dict:
        c = self._open()
        try:
            r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                raise LibraryError(f"item {item_id} not found", 404)
            if r["queue_state"] == "pending":
                base = self._current(c, r["source_url"])  # diff against what is current *now*
            else:
                base = c.execute("SELECT * FROM items WHERE id=?", (r["previous_item_id"],)).fetchone() \
                    if r["previous_item_id"] else None
            diff = redline(base["text"] if base else None, r["text"])
            return {"item_id": item_id, "base_item_id": base["id"] if base else None,
                    "base_version": base["version_no"] if base else None, "new_version": r["version_no"],
                    "base_checksum": base["checksum"] if base else None, "new_checksum": r["checksum"], **diff}
        finally:
            c.close()

    @staticmethod
    def _check_reviewer(actor: str):
        if actor not in REVIEWERS:
            raise LibraryError("reviewer must be a steward or QA (X-User)", 403)

    def _accept(self, c, item_id, actor, note, auto=False):
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise LibraryError(f"item {item_id} not found", 404)
        if r["queue_state"] != "pending":
            raise LibraryError("item already decided", 409)
        cur = self._current(c, r["source_url"])
        if cur and cur["version_no"] > r["version_no"]:
            raise LibraryError("stale: a newer version is already current; reject this item", 409)
        if cur:
            c.execute("UPDATE items SET is_current=0, status='superseded' WHERE id=?", (cur["id"],))
        c.execute("UPDATE items SET queue_state='accepted', is_current=1, decided_by=?, decided_at=?, "
                  "decision_note=?, auto_accepted=? WHERE id=?",
                  (actor, datetime.now(timezone.utc).isoformat(), note, int(auto), item_id))

    def accept(self, item_id: int, actor: str, note: str = "") -> dict:
        self._check_reviewer(actor)
        self._run(lambda c: self._accept(c, item_id, actor, note))
        return self.item(item_id, with_text=False)

    def reject(self, item_id: int, actor: str, note: str = "") -> dict:
        self._check_reviewer(actor)

        def tx(c):
            r = c.execute("SELECT queue_state FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                raise LibraryError(f"item {item_id} not found", 404)
            if r["queue_state"] != "pending":
                raise LibraryError("item already decided", 409)
            c.execute("UPDATE items SET queue_state='rejected', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                      (actor, datetime.now(timezone.utc).isoformat(), note, item_id))

        self._run(tx)
        return self.item(item_id, with_text=False)

    # ---- library state ----
    def library(self, include_text: bool = False) -> list[dict]:
        c = self._open()
        try:
            return [self._meta(r, include_text) for r in
                    c.execute("SELECT * FROM items WHERE is_current=1 ORDER BY source_url")]
        finally:
            c.close()

    def versions(self, source_url: str) -> list[dict]:
        c = self._open()
        try:
            return [self._meta(r) for r in
                    c.execute("SELECT * FROM items WHERE source_url=? ORDER BY version_no", (source_url,))]
        finally:
            c.close()

    # ---- retrieval support (Phase 3) ----
    def accepted_item(self, item_id: int) -> dict:
        """Full text of an ACCEPTED item (any version). Pending/rejected content is never citable."""
        c = self._open()
        try:
            r = c.execute("SELECT * FROM items WHERE id=? AND queue_state='accepted'", (item_id,)).fetchone()
            if not r:
                raise LibraryError(f"item {item_id} not found (or not accepted)", 404)
            return self._meta(r, with_text=True)
        finally:
            c.close()

    def library_version(self) -> dict:
        """Fingerprint of the current accepted library: changes whenever any current item changes."""
        c = self._open()
        try:
            rows = c.execute("SELECT id, version_no, checksum FROM items WHERE is_current=1 ORDER BY id").fetchall()
        finally:
            c.close()
        if not rows:
            return {"library_version": "lib-0-empty", "items": 0}
        digest = hashlib.sha256("|".join(f"{r['id']}:{r['version_no']}:{r['checksum']}" for r in rows).encode()).hexdigest()
        return {"library_version": f"lib-{len(rows)}-{digest[:12]}", "items": len(rows)}

    def search(self, q: str, include_text: bool = False, limit: int = 10) -> list[dict]:
        """Keyword search over CURRENT accepted items (tf-idf). Keyword only - no embeddings yet."""
        qt = _tokens(q)
        if not qt:
            raise LibraryError("empty query")
        c = self._open()
        try:
            rows = c.execute("SELECT * FROM items WHERE is_current=1").fetchall()
        finally:
            c.close()
        docs = [(r, Counter(_tokens((r["title"] or "") + " " + r["text"])), Counter(_tokens(r["title"] or ""))) for r in rows]
        n = len(docs) or 1
        df = Counter(t for _, tf, _ in docs for t in tf)
        hits = []
        for r, tf, tt in docs:
            score = sum((1 + math.log(tf[t])) * math.log(1 + n / df[t]) + (1.5 if t in tt else 0)
                        for t in set(qt) if t in tf)
            if score > 0:
                low = r["text"].lower()
                pos = min((low.find(t) for t in _raw_tokens(q) if low.find(t) >= 0), default=0)
                d = {"id": r["id"], "source_url": r["source_url"], "title": r["title"], "version_no": r["version_no"],
                     "document_date": r["document_date"], "version_or_docket": r["version_or_docket"],
                     "legal_weight": r["legal_weight"], "content_kind": r["content_kind"], "status": r["status"],
                     "score": round(score, 4), "snippet": r["text"][max(0, pos - 40):pos + 160].strip()}
                if include_text:
                    d["text"] = r["text"]
                hits.append(d)
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return hits[:limit]
