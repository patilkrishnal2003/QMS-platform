"""Retrieval for the assistant. Keyword search only (no embeddings yet - deliberate, see PROGRESS.md).
Reads from library (regulations/guidance), knowledge (SOP sections, traceability, past deviations) through the
gateway's internal read-only routes. Returns normalised, citable sources."""
import re

from . import allowlist
from .sources import SourceFetchError, library_source, record_source, req_source, sop_source

REQ_RE = re.compile(r"^\s*(?:21\s*CFR\s*)?(?:§\s*)?(\d{3}\.\d+)(?:\([a-z0-9]+\))*\s*$", re.I)


class RetrievalError(Exception):
    pass


def parse_requirement(query: str) -> str | None:
    m = REQ_RE.match(query or "")
    return m.group(1) if m else None


def _json(r, what: str):
    if r.status_code != 200:
        raise RetrievalError(f"{what}: HTTP {r.status_code}")
    return r.json()


def library_version(get=None) -> str:
    get = get or allowlist.service_get
    return _json(get("library", "/library/version"), "library version")["library_version"]


def knowledge_snapshot(get=None) -> str:
    """Fingerprint of the effective SOP set (so a log shows which SOP versions were in force)."""
    import hashlib

    get = get or allowlist.service_get
    docs = _json(get("knowledge", "/documents"), "knowledge documents")
    key = "|".join(sorted(f"{d['sop_number']}@{d['version']}" for d in docs))
    return f"kn-{len(docs)}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def retrieve(query: str, rcfg: dict, get=None) -> dict:
    get = get or allowlist.service_get
    sources: list[dict] = []
    notes: list[str] = []
    req_id = parse_requirement(query)
    doc_cache: dict[tuple, dict] = {}

    def doc(no: str, ver: str) -> dict | None:
        if (no, ver) not in doc_cache:
            r = get("knowledge", f"/documents/{no}", {"version": ver})
            doc_cache[(no, ver)] = r.json() if r.status_code == 200 else None
        return doc_cache[(no, ver)]

    try:
        # --- requirement ID: traceability entry + the SOP sections that implement it
        if req_id:
            r = get("knowledge", f"/traceability/{req_id}")
            if r.status_code == 200:
                entry = r.json()
                sources.append(req_source(entry, score=100.0))
                by_doc: dict[tuple, set] = {}
                for i in entry["implemented_by"]:
                    if i.get("resolved") and i.get("version"):
                        by_doc.setdefault((i["sop_number"], i["version"]), set()).add(i["section"])
                    elif i.get("stale"):
                        notes.append(f"traceability entry for {req_id} points at {i['sop_number']} {i['section']}, "
                                     "which has no effective version")
                for (no, ver), secs in by_doc.items():
                    d = doc(no, ver)
                    if d:
                        sources.append(sop_source(d, only=secs, score=90.0))
            elif r.status_code == 404:
                notes.append(f"no traceability entry for requirement {req_id}")
            else:
                raise RetrievalError(f"traceability: HTTP {r.status_code}")
        q = req_id or query

        # --- library items (regulations / guidance)
        for h in _json(get("library", "/search", {"q": q, "include_text": True, "limit": rcfg["library_top_k"]}),
                       "library search"):
            sources.append(library_source({**h, "is_current": True}, score=h["score"]))

        # --- SOP sections (free-text queries)
        if not req_id:
            hits = _json(get("knowledge", "/search", {"q": q, "kind": "sop", "limit": rcfg["sop_top_k"]}),
                         "knowledge SOP search")
            secs: dict[tuple, dict] = {}
            for h in hits:
                k = (h["sop_number"], h["version"])
                secs.setdefault(k, {"sections": set(), "score": 0.0})
                secs[k]["sections"].add(h["section_id"])
                secs[k]["score"] = max(secs[k]["score"], h["score"])
            for (no, ver), v in secs.items():
                d = doc(no, ver)
                if d:
                    sources.append(sop_source(d, only=v["sections"], score=v["score"]))

            # --- similar past deviations (executed, closed records only: precedent)
            for h in _json(get("knowledge", "/search", {"q": q, "kind": "executed_record",
                                                         "limit": rcfg["record_top_k"] * 2}), "record search"):
                if h.get("status") != "closed" or len([s for s in sources if s["type"] == "record"]) >= rcfg["record_top_k"]:
                    continue
                r = get("knowledge", f"/records/{h['id']}")
                if r.status_code == 200:
                    sources.append(record_source(r.json(), score=h["score"]))

            # --- requirement IDs mapped to the SOP sections we found (suggested links, hand-authored map)
            found = {(s["source_id"].split(":", 1)[1], p["locator"]) for s in sources if s["type"] == "sop"
                     for p in s["passages"]}
            for m in _json(get("knowledge", "/traceability"), "traceability"):
                if any((i["sop_number"], i["section"]) in found for i in m["implemented_by"]):
                    sources.append(req_source(m, score=10.0))
    except SourceFetchError as e:
        raise RetrievalError(str(e)) from e
    return {"sources": sources, "notes": notes, "requirement_id": req_id}
