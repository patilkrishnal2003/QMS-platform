"""Knowledge index: SOP versions with an effective flag, the deviation template,
executed records pushed in by the gateway, keyword search and the requirement
traceability stub. Keyword search only - semantic search comes in Phase 3."""
import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from .loader import Document, LoadError, load_sops, load_template, load_traceability

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "is", "are", "for", "be", "by", "on", "with", "as", "at",
        "that", "this", "it", "from", "was", "were", "not"}
TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


class IndexError_(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


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


def raw_tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


def tokens(text: str) -> list[str]:
    return [stem(t) for t in raw_tokens(text)]


def default_today() -> date:
    env = os.environ.get("KNOWLEDGE_TODAY")
    return date.fromisoformat(env) if env else date.today()


class KnowledgeIndex:
    def __init__(self, data_dir: Path | None = None, db_path: str | None = None, today=default_today):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.today = today
        self.docs: list[Document] = load_sops(self.data_dir / "sops")
        self.template = load_template(self.data_dir / "templates" / "deviation_template.json")
        self.trace = load_traceability(self.data_dir / "traceability.json")
        self._check_one_effective_version()
        self._check_traceability_targets()
        self.db_path = db_path or os.environ.get("KNOWLEDGE_DB", str(self.data_dir / "knowledge.db"))
        c = self._conn()
        try:
            with c:
                c.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, status TEXT, event_type TEXT,"
                          " severity TEXT, site TEXT, payload TEXT NOT NULL, indexed_at TEXT NOT NULL)")
        finally:
            c.close()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---- load-time validation ----
    def _check_one_effective_version(self):
        eff: dict[str, list[str]] = {}
        for d in self.docs:
            if d.is_effective(self.today()):
                eff.setdefault(d.sop_number, []).append(d.version)
        dup = {k: v for k, v in eff.items() if len(v) > 1}
        if dup:
            raise LoadError(f"more than one effective version: {dup}")

    def _check_traceability_targets(self):
        for m in self.trace["mappings"]:
            for t in m["implemented_by"]:
                versions = [d for d in self.docs if d.sop_number == t["sop_number"] and d.section(t["section"])]
                if not versions:
                    raise LoadError(f"traceability {m['requirement_id']}: {t} does not exist in any SOP version")

    # ---- documents ----
    def _doc_dict(self, d: Document, with_sections=False) -> dict:
        out = {"sop_number": d.sop_number, "title": d.title, "version": d.version, "status": d.status,
               "effective_date": d.effective_date.isoformat() if d.effective_date else None,
               "effective": d.is_effective(self.today()), "supersedes": d.supersedes, "kind": "sop",
               "synthetic": True, "file": d.file}
        if with_sections:
            out["sections"] = [{"section_id": s.section_id, "title": s.title, "text": s.text} for s in d.sections]
        return out

    def list_documents(self, include_all: bool = False) -> list[dict]:
        today = self.today()
        return [self._doc_dict(d) for d in sorted(self.docs, key=lambda x: (x.sop_number, x.version))
                if include_all or d.is_effective(today)]

    def get_document(self, sop_number: str, version: str | None = None) -> dict:
        today = self.today()
        cands = [d for d in self.docs if d.sop_number == sop_number]
        if not cands:
            raise IndexError_(f"unknown SOP {sop_number}", 404)
        if version:
            d = next((x for x in cands if x.version == version), None)
            if not d:
                raise IndexError_(f"{sop_number} has no version {version}", 404)
        else:
            d = next((x for x in cands if x.is_effective(today)), None)
            if not d:
                raise IndexError_(f"{sop_number} has no effective version right now", 404)
        return self._doc_dict(d, with_sections=True)

    def versions(self, sop_number: str) -> list[dict]:
        out = [self._doc_dict(d) for d in self.docs if d.sop_number == sop_number]
        if not out:
            raise IndexError_(f"unknown SOP {sop_number}", 404)
        return sorted(out, key=lambda x: x["version"])

    # ---- template ----
    def templates(self) -> list[dict]:
        t = self.template
        return [{"kind": "template", "record_kind": "template", "template_id": t["template_id"], "title": t["title"],
                 "version": t["version"], "status": t["status"], "effective_date": t["effective_date"],
                 "effective": t["status"] == "effective" and date.fromisoformat(t["effective_date"]) <= self.today(),
                 "synthetic": True, "required_fields": [f["name"] for f in t["fields"] if f["required"]],
                 "fields": t["fields"], "enums": t["enums"]}]

    # ---- executed records (pushed by gateway from mock-eqms) ----
    def index_records(self, records: list[dict], source: str = "mock-eqms") -> dict:
        """Index executed (filled-in) records. A blank template is refused (IK-06)."""
        bad = [r.get("id", "?") for r in records if r.get("record_kind") != "record"]
        if bad:
            raise IndexError_(f"only executed records may be indexed, not templates or unknown kinds: {bad}", 400)
        if any(not r.get("id") for r in records):
            raise IndexError_("every record needs an id")
        unversioned = [r["id"] for r in records if not str(r.get("version", "")).strip()]
        if unversioned:  # IK-05: executed records are cited by record ID and version
            raise IndexError_(f"records must carry a version so they can be cited: {unversioned}", 400)
        c = self._conn()
        try:
            with c:
                for r in records:
                    c.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,datetime('now'))",
                              (r["id"], r.get("status"), r.get("event_type"), r.get("severity"), r.get("site"),
                               json.dumps(r)))
        finally:
            c.close()
        return {"indexed": len(records), "source": source}

    def list_records(self, status: str | None = None, event_type: str | None = None) -> list[dict]:
        q, args = "SELECT payload FROM records WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if event_type:
            q += " AND event_type=?"; args.append(event_type)
        c = self._conn()
        try:
            return [self._record_dict(json.loads(r["payload"])) for r in c.execute(q + " ORDER BY id", args)]
        finally:
            c.close()

    def get_record(self, record_id: str) -> dict:
        c = self._conn()
        try:
            r = c.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        finally:
            c.close()
        if not r:
            raise IndexError_(f"record {record_id} not indexed", 404)
        return self._record_dict(json.loads(r["payload"]))

    @staticmethod
    def _record_dict(r: dict) -> dict:
        return {**r, "kind": "executed_record", "citation": f"{r['id']} v{r['version']}"}

    # ---- search ----
    def _corpus(self, kinds: set[str], include_non_effective: bool) -> list[dict]:
        today = self.today()
        items = []
        if "sop" in kinds:
            for d in self.docs:
                eff = d.is_effective(today)
                if not eff and not include_non_effective:
                    continue
                for s in d.sections:
                    items.append({"kind": "sop", "id": f"{d.sop_number}:{s.section_id}", "sop_number": d.sop_number,
                                  "version": d.version, "effective": eff, "section_id": s.section_id,
                                  "title": f"{d.title} - {s.title}", "text": s.text})
        if "template" in kinds:
            for t in self.templates():
                text = " ".join(f"{f['label']} {f['name']}" for f in t["fields"])
                items.append({"kind": "template", "id": t["template_id"], "version": t["version"],
                              "effective": t["effective"], "title": t["title"], "text": text})
        if "executed_record" in kinds:
            for r in self.list_records():
                text = " ".join(str(r.get(k, "")) for k in (
                    "event_type", "severity", "product", "equipment_id", "description", "immediate_action",
                    "investigation_summary", "root_cause", "root_cause_category", "product_impact"))
                capa = r.get("capa") or {}
                text += " " + " ".join(str(v) for v in capa.values())
                items.append({"kind": "executed_record", "id": r["id"], "version": r["version"],
                              "citation": r["citation"], "status": r.get("status"),
                              "event_type": r.get("event_type"), "severity": r.get("severity"),
                              "effective": None, "title": f"{r.get('event_type')} / {r.get('product')}",
                              "text": text})
        return items

    def search(self, q: str, kind: str | None = None, include_non_effective: bool = False, limit: int = 10,
               include_text: bool = False) -> list[dict]:
        qt = tokens(q)
        if not qt:
            raise IndexError_("empty query")
        kinds = {kind} if kind else {"sop", "template", "executed_record"}
        if not kinds <= {"sop", "template", "executed_record"}:
            raise IndexError_("kind must be sop, template or executed_record")
        items = self._corpus(kinds, include_non_effective)
        toks = [(it, Counter(tokens(it["title"] + " " + it["text"])), Counter(tokens(it["title"]))) for it in items]
        n = len(toks) or 1
        df = Counter(t for _, tf, _ in toks for t in tf)
        hits = []
        for it, tf, tt in toks:
            score = 0.0
            for t in set(qt):
                if t in tf:
                    score += (1 + math.log(tf[t])) * (math.log(1 + n / df[t])) + (1.5 if t in tt else 0)
            if score > 0:
                hits.append({**{k: v for k, v in it.items() if include_text or k != "text"}, "score": round(score, 4),
                             "snippet": self._snippet(it["text"], raw_tokens(q))})
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return hits[:limit]

    @staticmethod
    def _snippet(text: str, qt: list[str], width: int = 160) -> str:
        low = text.lower()
        pos = min((low.find(t) for t in qt if low.find(t) >= 0), default=0)
        start = max(0, pos - 40)
        return ("..." if start else "") + text[start:start + width].strip()

    # ---- traceability (IK-07 stub, hand-authored) ----
    def traceability(self, requirement_id: str | None = None) -> list[dict]:
        today = self.today()
        out = []
        for m in self.trace["mappings"]:
            if requirement_id and m["requirement_id"] != requirement_id:
                continue
            impl = []
            for t in m["implemented_by"]:
                eff = next((d for d in self.docs if d.sop_number == t["sop_number"] and d.is_effective(today)), None)
                sec = eff.section(t["section"]) if eff else None
                impl.append({**t, "resolved": sec is not None, "version": eff.version if eff else None,
                             "section_title": sec.title if sec else None,
                             "stale": sec is None,
                             "note": None if sec else "no effective version of this SOP contains this section"})
            out.append({"requirement_id": m["requirement_id"], "citation": m["citation"], "implemented_by": impl})
        if requirement_id and not out:
            raise IndexError_(f"no mapping for requirement {requirement_id}", 404)
        return out

    def requirements_for_section(self, sop_number: str, section_id: str) -> list[str]:
        return [m["requirement_id"] for m in self.trace["mappings"]
                if any(t["sop_number"] == sop_number and t["section"] == section_id for t in m["implemented_by"])]
