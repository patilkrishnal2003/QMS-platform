"""Prompt building, retrieval merging and structured-output parsing. Pure functions, no network.
The system prompt and retrieval settings arrive from the gateway's version-controlled AI config (AI-06)."""
import json
import re

CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string"},
        "version": {"type": "string"},
        "locator": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["source_id", "version", "locator", "quote"],
}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["requirement", "procedure", "precedent", "other"]},
                    "citations": {"type": "array", "items": CITATION_SCHEMA},
                },
                "required": ["text", "kind", "citations"],
            },
        },
        "no_support_found": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["statements", "no_support_found", "gaps"],
}
TYPE_ORDER = {"answer": -1, "req": 0, "library": 1, "sop": 2, "record": 3}
# Defaults only for direct use/tests. In the running system both come from the approved AI config (AI-06).
DEFAULT_PASSAGE_TEMPLATE = "[locator: {locator}] {text}"
DEFAULT_INSTRUCTIONS = (
    "Reply with JSON matching the schema. Every statement about a requirement or procedure must carry at least "
    "one citation: source_id and version copied exactly from the source header, the locator copied exactly from "
    "the value after 'locator:' (for example 5.1 or lines:2, without brackets or the section title), and a quote "
    "copied word for word from that passage. If the sources do not support an answer, set no_support_found to "
    "true and return no statements. Do not use knowledge outside the sources.")


class PromptConfigError(ValueError):
    pass


def _check_template(t: str) -> str:
    if "{locator}" not in t or "{text}" not in t:
        raise PromptConfigError("passage_template must contain {locator} and {text}")
    return t


def merge_sources(sources: list[dict], max_sources: int) -> list[dict]:
    """De-duplicate by (source_id, version), keep the best score, order by type then score."""
    best: dict[tuple, dict] = {}
    for s in sources:
        k = (s["source_id"], s.get("version"))
        if k not in best or (s.get("score") or 0) > (best[k].get("score") or 0):
            best[k] = s
    ordered = sorted(best.values(), key=lambda s: (TYPE_ORDER.get(s.get("type"), 9), -(s.get("score") or 0), s["source_id"]))
    return ordered[:max_sources]


def format_context(sources: list[dict], max_chars: int,
                   passage_template: str = DEFAULT_PASSAGE_TEMPLATE) -> tuple[str, list[str]]:
    tpl = _check_template(passage_template)
    lines, used, ids = [], 0, []
    for i, s in enumerate(sources, 1):
        head = f"[S{i}] source_id={s['source_id']} version={s.get('version') or '-'} :: {s.get('title', '')}"
        if s.get("effective") is False or s.get("status") in ("superseded", "draft", "approved"):
            head += "  (NOT EFFECTIVE)"
        block = [head]
        if s.get("lines"):
            block += ["  " + tpl.replace("{locator}", f"lines:{n}").replace("{text}", t)
                      for n, t in enumerate(s["lines"], 1) if t.strip()]
        for p in s.get("passages") or []:
            block.append("  " + tpl.replace("{locator}", p["locator"]).replace("{text}", p["text"]))
        text = "\n".join(block)
        if used + len(text) > max_chars:
            text = text[: max(0, max_chars - used)] + "\n  [truncated]"
        lines.append(text)
        ids.append(s["source_id"] + ("@" + str(s["version"]) if s.get("version") else ""))
        used += len(text)
        if used >= max_chars:
            break
    return "\n\n".join(lines), ids


def build_prompt(query: str, sources: list[dict], system_prompt: str, max_sources: int = 10,
                 max_context_chars: int = 12000, passage_template: str = DEFAULT_PASSAGE_TEMPLATE,
                 instructions: str = DEFAULT_INSTRUCTIONS) -> dict:
    merged = merge_sources(sources, max_sources)
    context, ids = format_context(merged, max_context_chars, passage_template)
    user = (f"QUESTION / EVENT:\n{query.strip()}\n\nSOURCES (the only material you may rely on):\n{context}\n\n"
            + instructions.strip())
    return {"messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}],
            "schema": RESPONSE_SCHEMA, "context_source_ids": ids, "sources_in_context": len(ids)}


# ----------------------------------------------------------------------------- parsing
def parse_output(raw: str) -> dict:
    """Strict JSON parse with light clean-up. Never raises; malformed items are kept as unsupported."""
    res = {"ok": False, "error": None, "statements": [], "no_support_found": False, "gaps": [], "cited": []}
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    if not text.startswith("{") and "{" in text:
        text = text[text.index("{"): text.rindex("}") + 1] if "}" in text else text
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as e:
        res["error"] = f"output is not valid JSON: {e}"
        return res
    if not isinstance(obj, dict) or not isinstance(obj.get("statements"), list):
        res["error"] = "JSON lacks a 'statements' list"
        return res
    for s in obj["statements"]:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            res["statements"].append({"text": str(s), "kind": "other", "citations": [], "malformed": True})
            continue
        cites = [{"source_id": str(c.get("source_id") or ""), "version": str(c.get("version") or ""),
                  "locator": str(c.get("locator") or ""), "quote": str(c.get("quote") or "")}
                 for c in (s.get("citations") or []) if isinstance(c, dict)]
        kind = s.get("kind") if s.get("kind") in ("requirement", "procedure", "precedent", "other") else "other"
        res["statements"].append({"text": s["text"], "kind": kind, "citations": cites})
    res["no_support_found"] = bool(obj.get("no_support_found", False))
    res["gaps"] = [str(g) for g in (obj.get("gaps") or [])]
    seen = []
    for s in res["statements"]:
        for c in s["citations"]:
            k = (c["source_id"], c["version"])
            if c["source_id"] and k not in seen:
                seen.append(k)
    res["cited"] = [{"source_id": a, "version": b} for a, b in seen]
    res["ok"] = True
    return res
