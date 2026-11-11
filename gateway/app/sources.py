"""Normalised, citable source objects + on-demand fetching of a cited source from the real stores.

A normalised source:
  {type, source_id, version, title, effective, status, content_kind, link, passages:[{locator,text}], lines:[...], score}
Citations point at (source_id, version, locator). Links are service URLs of the passage's source."""
import re

from . import allowlist


class SourceFetchError(Exception):
    """A store could not be queried. This is an infrastructure failure, NOT proof the source does not exist."""


def _link(service: str, path: str) -> str:
    return allowlist.service_base(service) + path


def sop_source(doc: dict, only: set | None = None, score: float | None = None) -> dict:
    ver = doc["version"]
    return {"type": "sop", "source_id": f"sop:{doc['sop_number']}", "version": ver,
            "title": f"{doc['sop_number']} {doc['title']}", "effective": bool(doc["effective"]), "status": doc["status"],
            "link": _link("knowledge", f"/documents/{doc['sop_number']}?version={ver}"), "score": score,
            "passages": [{"locator": s["section_id"], "text": f"{s['title']}. {s['text']}"}
                         for s in doc["sections"] if only is None or s["section_id"] in only]}


def library_source(item: dict, score: float | None = None) -> dict:
    return {"type": "library", "source_id": f"library:{item['id']}", "version": str(item["version_no"]),
            "title": f"{item.get('title') or item.get('source_url')} ({item.get('version_or_docket') or ''})".strip(),
            "effective": bool(item.get("is_current", True)), "status": item.get("status"),
            "content_kind": item.get("content_kind", "full_text"), "legal_weight": item.get("legal_weight"),
            "link": _link("library", f"/library/{item['id']}"), "score": score,
            "lines": (item.get("text") or "").split("\n"), "passages": []}


RECORD_FIELDS = ("event_type", "severity", "description", "immediate_action", "investigation_summary", "root_cause",
                 "root_cause_category", "product_impact", "batch_disposition")


def record_source(rec: dict, score: float | None = None) -> dict:
    passages = [{"locator": f, "text": str(rec[f])} for f in RECORD_FIELDS if rec.get(f)]
    for k, v in (rec.get("capa") or {}).items():
        passages.append({"locator": f"capa.{k}", "text": str(v)})
    return {"type": "record", "source_id": f"record:{rec['id']}", "version": str(rec["version"]),
            "title": f"{rec['id']} ({rec.get('event_type')}, {rec.get('severity')}, {rec.get('status')})",
            "effective": None, "status": rec.get("status"), "link": _link("knowledge", f"/records/{rec['id']}"),
            "score": score, "passages": passages}


def req_source(entry: dict, score: float | None = None) -> dict:
    passages = []
    for i in entry["implemented_by"]:
        ver = f" (v{i['version']})" if i.get("version") else ""
        title = f" {i['section_title']}" if i.get("section_title") else ""
        passages.append({"locator": f"{i['sop_number']}#{i['section']}",
                         "text": f"{entry['citation']} is implemented by {i['sop_number']} section {i['section']}"
                                 f"{title}{ver}."})
    return {"type": "req", "source_id": f"req:{entry['requirement_id']}", "version": None,
            "title": entry["citation"], "effective": None, "status": "hand-authored traceability map",
            "link": _link("knowledge", f"/traceability/{entry['requirement_id']}"), "score": score,
            "passages": passages}


def _ok(r, what: str):
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise SourceFetchError(f"{what}: HTTP {r.status_code}")
    return r.json()


def fetch_source(source_id: str, version: str | None, get=None) -> dict | None:
    """Fetch the FULL cited source from its owning store. None = it does not exist (at that version)."""
    get = get or allowlist.service_get
    kind, _, ident = (source_id or "").partition(":")
    ver = str(version).strip().lstrip("vV") if version else ""
    if not ident:
        return None
    if kind == "sop":
        if not ver:
            return None
        doc = _ok(get("knowledge", f"/documents/{ident}", {"version": ver}), f"sop {ident}")
        return sop_source(doc) if doc else None
    if kind == "library":
        if not ident.isdigit():
            return None
        item = _ok(get("library", f"/library/{ident}"), f"library {ident}")
        if not item or str(item["version_no"]) != ver:
            return None
        return library_source(item)
    if kind == "record":
        rec = _ok(get("knowledge", f"/records/{ident}"), f"record {ident}")
        if not rec or str(rec.get("version")) != ver or rec.get("record_kind") != "record":
            return None
        return record_source(rec)
    if kind == "answer":
        rid, _, key = ident.partition(":")
        if not rid or not key or not ver:
            return None
        src = _ok(get("workflow", f"/internal/deviations/{rid}/sources/{key}"), f"answer {ident}")
        return src if src and str(src.get("version")) == ver else None
    if kind == "req":
        if not re.fullmatch(r"\d{3}\.\d+", ident):
            return None
        entry = _ok(get("knowledge", f"/traceability/{ident}"), f"requirement {ident}")
        return req_source(entry) if entry else None
    return None
