"""Citation verifier (AI-01, AI-02).  Pure stdlib, no network, no imports from the rest of the app.

For every statement an LLM makes it checks, per citation:
  1. the cited source exists (in the library / knowledge store) at the cited version,
  2. the cited passage/locator exists inside that source,
  3. the quoted text really appears in that passage,
  4. the passage plausibly supports the statement (lexical coverage + number check).
Statements are never dropped and never silently passed: each gets a verdict and reasons.

Verdicts: verified | low_confidence | mismatch | invalid_citation | unsupported
Only `verified` is usable. Everything else is FLAGGED for the user.

This is a lexical check, not a semantic one. It cannot reliably catch a paraphrase that reverses
meaning; a human still confirms. Thresholds are version-controlled configuration (AI-06)."""
import re

DEFAULT_THRESHOLDS = {"verified": 0.60, "low": 0.35}

# modal verbs carry little content and are paraphrased freely (shall/must/should)
STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "is", "are", "for", "be", "by", "on", "with", "as", "at",
        "that", "this", "it", "from", "was", "were", "its", "their", "which", "who", "whose", "than", "then",
        "shall", "must", "should", "will", "would", "may", "can", "could", "also", "any", "all", "each", "every",
        "per", "has", "have", "had", "been", "being", "so", "if", "when", "where", "into", "such", "these",
        "those", "there", "do", "does", "did", "other", "only"}
NEGATIONS = {"not", "no", "never", "without", "cannot", "neither", "nor", "n't"}
NUMWORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
            "nine": "9", "ten": "10", "twelve": "12", "fifteen": "15", "twenty": "20", "thirty": "30",
            "forty": "40", "sixty": "60", "ninety": "90"}
_TOKEN = re.compile(r"\d+(?:\.\d+)*|[a-z]+(?:'[a-z]+)?")


def _stem(w: str) -> str:
    if w.isdigit() or "." in w or len(w) <= 4:
        return w
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and not w.endswith("ss") and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    return w


def tokenize(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall((text or "").lower().replace("’", "'")):
        t = NUMWORDS.get(t, t)
        out.append(t)
    return out


def content_tokens(text: str) -> list[str]:
    return [_stem(t) for t in tokenize(text) if t not in STOP and t not in NEGATIONS]


def _numbers(text: str) -> set[str]:
    return {t for t in tokenize(text) if t[0].isdigit()}


def _negation_count(text: str) -> int:
    return sum(1 for t in tokenize(text) if t in NEGATIONS or t.endswith("n't"))


def normalize_quote_text(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\"'.,;:")


def urlquote(text: str, safe: str = "") -> str:
    """Percent-encode for a URL fragment (hand-written: urllib is banned outside the gateway)."""
    return "".join(c if (c.isascii() and (c.isalnum() or c in "-._~" or c in safe)) else
                   "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in text)


def source_key(source_id: str, version) -> str:
    v = str(version).strip().lstrip("vV") if version not in (None, "") else ""
    return f"{source_id}@{v}" if v else source_id


# ----------------------------------------------------------------------------- passages
def resolve_passage(source: dict, locator: str) -> str | None:
    """Return the text of `locator` inside `source`, or None if it does not exist."""
    locator = (locator or "").strip()
    m = re.fullmatch(r"lines:(\d+)(?:-(\d+))?", locator)
    if m:
        lines = source.get("lines") or []
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if 1 <= a <= b <= len(lines):
            return " ".join(lines[a - 1:b])
        return None
    for p in source.get("passages") or []:
        if p.get("locator") == locator:
            return p.get("text", "")
    return None


def quote_in_passage(quote: str, passage: str) -> bool:
    """The quote (or each '...'-separated fragment of it) must appear verbatim, after
    whitespace/case/quote-mark normalisation."""
    hay = normalize_quote_text(passage)
    frags = [normalize_quote_text(f) for f in re.split(r"\.\.\.|…", quote or "")]
    frags = [f for f in frags if f]
    return bool(frags) and all(f in hay for f in frags)


def strip_self_references(text: str, citations: list[dict]) -> str:
    """Remove the statement's mentions of its OWN cited source (its id and version, e.g. 'SOP-DEV-001',
    'version 3.0') before scoring: naming the source is not a claim the passage has to contain, and its digits
    must not count as numbers to find in the passage. Only exact id/version references are removed, so a real
    quantity like '24 hours' is never exempted. Bare numeric ids (library:7) are not stripped."""
    out = text or ""
    for c in citations:
        ident = (c.get("source_id") or "").partition(":")[2]
        if ident and not ident.isdigit():
            pat = r"[\s_\-]+".join(re.escape(p) for p in re.split(r"[\s_\-]+", ident) if p)
            out = re.sub(r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])", " ", out, flags=re.I)
        ver = str(c.get("version") or "").strip().lstrip("vV")
        if ver:
            out = re.sub(r"\b(?:version|ver\.?|v)\s*\.?\s*" + re.escape(ver) + r"(?![\d.]*\d)", " ", out, flags=re.I)
            if "." in ver:  # '3.0' by itself is a version reference, not a quantity
                out = re.sub(r"(?<![\d.])" + re.escape(ver) + r"(?![\d])", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def support_score(statement: str, passage_text: str, quote_text: str = "") -> tuple[float, list[str]]:
    """Share of the statement's content words found in the passage, plus any hard problems.
    Negation is compared against the cited QUOTE (a whole section often has unrelated negations)."""
    stoks = content_tokens(statement)
    if not stoks:
        return 0.0, ["statement_has_no_content_words"]
    ptoks = set(content_tokens(passage_text))
    score = sum(1 for t in stoks if t in ptoks) / len(stoks)
    problems = []
    missing_numbers = sorted(n for n in _numbers(statement) if n not in _numbers(passage_text))
    if missing_numbers:
        problems.append("number_not_in_passage:" + ",".join(missing_numbers))
    if quote_text and _negation_count(statement) != _negation_count(quote_text):
        problems.append("negation_differs")
    return round(score, 3), problems


# ----------------------------------------------------------------------------- citations
def check_citation(c: dict, sources: dict) -> dict:
    """Structural checks 1-3. Returns the citation annotated with status ok|invalid, reason, passage."""
    out = {"source_id": c.get("source_id") or "", "version": c.get("version") or None,
           "locator": c.get("locator") or "", "quote": c.get("quote") or "",
           "status": "invalid", "reason": None, "link": None, "_passage": None, "_flags": []}
    sid, loc, quote = out["source_id"], out["locator"], out["quote"]
    if not sid or not loc or not quote.strip():
        out["reason"] = "incomplete_citation"
        return out
    is_req = sid.startswith("req:")
    if not is_req and not out["version"]:
        out["reason"] = "version_missing"                    # AI-01: document, version AND section
        return out
    src = sources.get(source_key(sid, out["version"]))
    if src is None:
        out["reason"] = "source_not_found"
        return out
    passage = resolve_passage(src, loc)
    if passage is None:
        out["reason"] = "locator_not_found"
        return out
    out["link"] = (src.get("link") or "") + "#" + urlquote(loc, safe="")
    if not quote_in_passage(quote, passage):
        out["reason"] = "quote_not_in_passage"
        return out
    out["status"], out["_passage"] = "ok", passage
    if src.get("effective") is False or src.get("status") in ("superseded", "withdrawn", "draft", "approved"):
        out["_flags"].append("source_not_effective")
    if src.get("content_kind") == "metadata_only":
        out["_flags"].append("source_is_metadata_only")
    return out


def verify_statement(stmt: dict, sources: dict, thresholds: dict | None = None) -> dict:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    text = (stmt.get("text") or "").strip()
    res = {"text": text, "kind": stmt.get("kind") or "other", "verdict": None, "score": None,
           "reasons": [], "citations": []}
    cites = [c for c in (stmt.get("citations") or []) if isinstance(c, dict)]
    if not cites:
        res.update(verdict="unsupported", reasons=["no_citation"])        # AI-01/AI-02: never invented, never passed
        return res
    checked = [check_citation(c, sources) for c in cites]
    bad = [c for c in checked if c["status"] != "ok"]
    passages = [c["_passage"] for c in checked if c["status"] == "ok"]
    flags = sorted({f for c in checked for f in c["_flags"]})
    if bad:  # a fabricated/broken citation is never silently passed, even next to a good one
        res["verdict"] = "invalid_citation"
        res["reasons"] = sorted({c["reason"] for c in bad})
    else:
        claim = strip_self_references(text, checked)
        score, problems = support_score(claim, " ".join(passages), " ".join(c["quote"] for c in checked))
        if not content_tokens(claim):
            problems.append("statement_only_names_its_source")
        res["score"] = score
        res["reasons"] = problems + flags
        if any(p.startswith("number_not_in_passage") for p in problems) or score < th["low"]:
            res["verdict"] = "mismatch"
            if score < th["low"]:
                res["reasons"].insert(0, "passage_does_not_support_statement")
        elif score < th["verified"] or "negation_differs" in problems or flags:
            res["verdict"] = "low_confidence"
        else:
            res["verdict"] = "verified"
    for c in checked:
        res["citations"].append({k: v for k, v in c.items() if not k.startswith("_")})
    return res


def verify(statements: list, sources: dict, thresholds: dict | None = None) -> dict:
    results = [verify_statement(s if isinstance(s, dict) else {"text": str(s), "citations": []}, sources, thresholds)
               for s in statements]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"results": results,
            "summary": {"total": len(results), "verified": counts.get("verified", 0),
                        "flagged": len(results) - counts.get("verified", 0), "by_verdict": counts,
                        "thresholds": {**DEFAULT_THRESHOLDS, **(thresholds or {})}}}
