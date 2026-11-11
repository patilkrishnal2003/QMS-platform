"""Prompt building and output parsing for the three AI tasks in the drafting loop: proposals (GW-02), gap questions
(GW-03) and the draft (GW-06). Pure functions: the gateway runs the model; instructions come from the version-controlled
AI config (AI-06). Every AI claim carries citations that the Phase 3 verifier checks."""
import json
import re

from . import prompt

CITATION = prompt.CITATION_SCHEMA
CITES = {"type": "array", "items": CITATION}
STATEMENT = {"type": "object", "properties": {"text": {"type": "string"}, "citations": CITES},
             "required": ["text", "citations"]}


def _loads(raw: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    if not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    return json.loads(text)


def _cites(items) -> list[dict]:
    return [{"source_id": str(c.get("source_id") or ""), "version": str(c.get("version") or ""),
             "locator": str(c.get("locator") or ""), "quote": str(c.get("quote") or "")}
            for c in (items or []) if isinstance(c, dict)]


def _context(sources, system_prompt, max_sources, max_chars, passage_template):
    merged = prompt.merge_sources(sources, max_sources)
    return prompt.format_context(merged, max_chars, passage_template)


def _intake_block(intake: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in intake.items() if v)


# ----------------------------------------------------------------------------- proposals (GW-02)
def proposals_schema(event_types: list, classes: list) -> dict:
    prop = lambda extra: {"type": "object", "properties": {**extra, "reasoning": {"type": "string"}, "citations": CITES},
                          "required": [*extra, "reasoning", "citations"]}
    return {"type": "object", "properties": {
        "event_type": prop({"value": {"type": "string", "enum": event_types}}),
        "regulations": {"type": "array", "items": prop({"requirement_id": {"type": "string"}})},
        "sops": {"type": "array", "items": prop({"sop_number": {"type": "string"}})},
        "classification": prop({"value": {"type": "string", "enum": classes}})},
        "required": ["event_type", "regulations", "sops", "classification"]}


def build_proposals_prompt(p: dict) -> dict:
    ctx, ids = _context(p["sources"], p["system_prompt"], p["max_sources"], p["max_context_chars"], p["passage_template"])
    user = (f"NEW DEVIATION (as described by the initiator):\n{_intake_block(p['intake'])}\n\n"
            f"SOURCES (the only material you may rely on):\n{ctx}\n\n{p['instructions'].strip()}\n"
            f"Allowed event types: {', '.join(p['event_types'])}. Allowed classifications: {', '.join(p['classes'])}.")
    return {"messages": [{"role": "system", "content": p["system_prompt"]}, {"role": "user", "content": user}],
            "schema": proposals_schema(p["event_types"], p["classes"]), "context_source_ids": ids}


def parse_proposals(raw: str, event_types: list, classes: list, known_requirements: list, effective_sops: list) -> dict:
    """Items for the four proposals + the flattened statements the verifier must check. Never raises."""
    res = {"ok": False, "error": None, "items": {}, "statements": []}
    try:
        obj = _loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError) as e:
        res["error"] = f"output is not valid JSON: {e}"
        return res
    reqs = {r.strip() for r in known_requirements}
    sops = set(effective_sops)

    def entry(d, kind):
        d = d if isinstance(d, dict) else {}
        reasoning = str(d.get("reasoning") or "").strip()
        problems = []
        if kind == "event_type":
            value = str(d.get("value") or "").strip()
            if value not in event_types:
                problems.append(f"event type {value!r} is not an allowed type")
        elif kind == "classification":
            value = str(d.get("value") or "").strip()
            if value not in classes:
                problems.append(f"classification {value!r} is not minor/major/critical")
        elif kind == "regulations":
            value = re.sub(r"(?i)^\s*(21\s*cfr\s*)?§?\s*", "", str(d.get("requirement_id") or "")).strip()
            if value not in reqs:
                problems.append(f"requirement {value!r} is not in the traceability map")
        else:
            value = str(d.get("sop_number") or "").strip()
            if value not in sops:
                problems.append(f"SOP {value!r} has no effective version")
        return {"value": value, "reasoning": reasoning, "citations": _cites(d.get("citations")),
                "validity": {"ok": not problems, "problems": problems}}

    items = {"event_type": {"entries": [entry(obj.get("event_type"), "event_type")]},
             "classification": {"entries": [entry(obj.get("classification"), "classification")]},
             "regulations": {"entries": [entry(d, "regulations") for d in (obj.get("regulations") or [])]},
             "sops": {"entries": [entry(d, "sops") for d in (obj.get("sops") or [])]}}
    if not items["event_type"]["entries"][0]["value"]:
        items["event_type"]["entries"] = []
    if not items["classification"]["entries"][0]["value"]:
        items["classification"]["entries"] = []
    res["items"] = items
    for name, it in items.items():
        for i, e in enumerate(it["entries"]):
            res["statements"].append({"item": name, "index": i, "text": e["reasoning"], "kind": "other",
                                      "citations": e["citations"]})
    cited = []
    for s in res["statements"]:
        for c in s["citations"]:
            k = (c["source_id"], c["version"])
            if c["source_id"] and k not in cited:
                cited.append(k)
    res["cited"] = [{"source_id": a, "version": b} for a, b in cited]
    res["ok"] = True
    return res


# ----------------------------------------------------------------------------- gap questions (GW-03)
GAP_SCHEMA = {"type": "object", "properties": {
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "why": {"type": "string"}, "citations": CITES}, "required": ["text", "why", "citations"]}},
    "no_gaps": {"type": "boolean"}}, "required": ["questions", "no_gaps"]}


def build_gap_prompt(p: dict) -> dict:
    ctx, ids = _context(p["sources"], p["system_prompt"], p["max_sources"], p["max_context_chars"], p["passage_template"])
    asked = "\n".join(f"- {q}" for q in p["existing_questions"])
    user = (f"WHAT THE INITIATOR HAS SAID SO FAR:\n{_intake_block(p['intake'])}\n\nQUESTIONS ALREADY ASKED (do not repeat):\n{asked}\n\n"
            f"SOURCES (the only material you may rely on):\n{ctx}\n\n{p['instructions'].strip()}")
    return {"messages": [{"role": "system", "content": p["system_prompt"]}, {"role": "user", "content": user}],
            "schema": GAP_SCHEMA, "context_source_ids": ids}


def parse_gap(raw: str, max_questions: int = 6) -> dict:
    res = {"ok": False, "error": None, "questions": [], "statements": [], "cited": [], "no_gaps": False}
    try:
        obj = _loads(raw)
        qs = obj["questions"]
        if not isinstance(qs, list):
            raise ValueError("questions is not a list")
    except (ValueError, TypeError, KeyError) as e:
        res["error"] = f"output is not usable: {e}"
        return res
    for q in qs[:max_questions]:
        if not isinstance(q, dict) or not str(q.get("text") or "").strip():
            continue
        cites = _cites(q.get("citations"))
        res["questions"].append({"text": str(q["text"]).strip(), "why": str(q.get("why") or "").strip(), "citations": cites})
        res["statements"].append({"text": str(q.get("why") or "").strip(), "kind": "requirement", "citations": cites})
    seen = []
    for s in res["statements"]:
        for c in s["citations"]:
            k = (c["source_id"], c["version"])
            if c["source_id"] and k not in seen:
                seen.append(k)
    res["cited"] = [{"source_id": a, "version": b} for a, b in seen]
    res["no_gaps"] = bool(obj.get("no_gaps", False)) if isinstance(obj, dict) else False
    res["ok"] = True
    return res


# ----------------------------------------------------------------------------- draft (GW-06, AI-13, DI-05)
DRAFT_SECTIONS = ["event_summary", "regulatory_and_procedural_context", "precedent_review"]
DRAFT_SCHEMA = {"type": "object", "properties": {
    "sections": {"type": "object", "properties": {s: {"type": "array", "items": STATEMENT} for s in DRAFT_SECTIONS},
                 "required": DRAFT_SECTIONS},
    "no_support_found": {"type": "boolean"}, "gaps": {"type": "array", "items": {"type": "string"}}},
    "required": ["sections", "no_support_found", "gaps"]}


def build_draft_prompt(p: dict) -> dict:
    ctx, ids = _context(p["sources"], p["system_prompt"], p["max_sources"], p["max_context_chars"], p["passage_template"])
    user = (f"DEVIATION DRAFT REQUEST. Sections to write: {', '.join(DRAFT_SECTIONS)}.\n"
            f"SOURCES (the only material you may rely on; 'answer:' sources are what the people on this record entered):\n{ctx}\n\n"
            f"{p['instructions'].strip()}")
    return {"messages": [{"role": "system", "content": p["system_prompt"]}, {"role": "user", "content": user}],
            "schema": DRAFT_SCHEMA, "context_source_ids": ids}


def parse_draft(raw: str) -> dict:
    res = {"ok": False, "error": None, "sections": {s: [] for s in DRAFT_SECTIONS}, "statements": [], "cited": [],
           "no_support_found": False, "gaps": []}
    try:
        obj = _loads(raw)
        secs = obj["sections"]
        if not isinstance(secs, dict):
            raise ValueError("sections is not an object")
    except (ValueError, TypeError, KeyError) as e:
        res["error"] = f"output is not usable: {e}"
        return res
    for name in DRAFT_SECTIONS:
        for s in (secs.get(name) or []):
            if isinstance(s, dict) and isinstance(s.get("text"), str) and s["text"].strip():
                item = {"text": s["text"].strip(), "kind": "other", "citations": _cites(s.get("citations")), "section": name}
            else:   # malformed entries are kept as citation-less statements (flagged), never dropped
                item = {"text": str(s), "kind": "other", "citations": [], "section": name, "malformed": True}
            res["sections"][name].append(item)
            res["statements"].append(item)
    seen = []
    for s in res["statements"]:
        for c in s["citations"]:
            k = (c["source_id"], c["version"])
            if c["source_id"] and k not in seen:
                seen.append(k)
    res["cited"] = [{"source_id": a, "version": b} for a, b in seen]
    res["no_support_found"] = bool(obj.get("no_support_found", False))
    res["gaps"] = [str(g) for g in (obj.get("gaps") or [])]
    res["ok"] = True
    return res
