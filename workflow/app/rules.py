"""Question bank, adaptation rules, and completeness checklist logic. Pure functions over a record state dict."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "elements.json"
_BANK: dict | None = None

TBD_KINDS = ("unknown", "tbd")


def bank() -> dict:
    global _BANK
    if _BANK is None:
        b = json.loads(DATA.read_text(encoding="utf-8"))
        if b.get("synthetic") is not True:
            raise ValueError("elements.json must be labelled synthetic")
        _BANK = b
    return _BANK


def elements() -> list[dict]:
    return bank()["elements"]


def element(key: str) -> dict | None:
    return next((e for e in elements() if e["key"] == key), None)


def decision_values(key: str) -> list | None:
    return bank()["decision_values"].get(key)


# ------------------------------------------------------------------ current values used by adaptation rules
def current_value(state: dict, key: str):
    """The confirmed value of `key` (from a decision or an answer), or None."""
    d = state.get("decisions", {}).get(key)
    if d and not d.get("tbd"):
        return d["value"]
    a = state.get("answers", {}).get(key)
    if a and a["kind"] == "answer":
        return a["value"]
    return None


def applies(el: dict, state: dict) -> bool:
    """GW-04: a question/element appears only when earlier answers make it relevant."""
    for key, allowed in (el.get("when") or {}).items():
        if current_value(state, key) not in allowed:
            return False
    return True


def applicable_elements(state: dict) -> list[dict]:
    return [e for e in elements() if applies(e, state)]


def adapted_out(state: dict) -> list[dict]:
    """Elements hidden by the current answers, with the reason (shown for transparency, never silently)."""
    out = []
    for e in elements():
        if not applies(e, state):
            out.append({"key": e["key"], "label": e["label"],
                        "reason": "; ".join(f"{k} is {current_value(state, k) or 'not set'}, needs one of {v}"
                                            for k, v in e["when"].items())})
    return out


# ------------------------------------------------------------------ per-element status
def element_state(state: dict, key: str) -> dict:
    """status: answered | missing | tbd | tbd_accepted | not_applicable, plus the value and who/when."""
    na = state.get("na", {}).get(key)
    if na:
        return {"status": "not_applicable", "justification": na["justification"], "by": na["by"], "at": na["at"]}
    accepted = state.get("tbd_accepted", {}).get(key)
    src = state.get("decisions", {}).get(key)
    kind = "decision"
    if not src:
        src, kind = state.get("answers", {}).get(key), "answer"
    if not src:
        return {"status": "missing"}
    is_tbd = src.get("tbd") if kind == "decision" else src["kind"] in TBD_KINDS
    base = {"value": src.get("value"), "by": src.get("by"), "at": src.get("at"), "via": kind,
            "answer_kind": "tbd" if is_tbd else "answer", "revision": src.get("revision")}
    if is_tbd:
        if accepted:
            return {**base, "status": "tbd_accepted", "accepted_by": accepted["by"], "accepted_at": accepted["at"],
                    "justification": accepted["justification"]}
        return {**base, "status": "tbd"}
    return {**base, "status": "answered"}


def checklist(state: dict) -> dict:
    """GW-05: every required element with its status. Hidden (adapted-out) elements are listed separately."""
    items = []
    for e in applicable_elements(state):
        st = element_state(state, e["key"])
        items.append({"key": e["key"], "label": e["label"], "kind": e["kind"], "satisfies": e["satisfies"],
                      "why": e["why"], **st})
    for g in state.get("gap_questions", []):          # AI-generated gap questions are required too, unless dismissed
        if g.get("dismissed"):
            continue
        items.append({"key": g["id"], "label": g["text"], "kind": "gap", "satisfies": g["satisfies"], "why": g["why"],
                      **element_state(state, g["id"])})
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    missing = [i["key"] for i in items if i["status"] == "missing"]
    tbd = [i["key"] for i in items if i["status"] == "tbd"]
    return {"items": items, "counts": counts, "missing": missing, "tbd_unaccepted": tbd,
            "complete_for_draft": not missing, "adapted_out": adapted_out(state)}


# ------------------------------------------------------------------ questions (GW-03/GW-04)
def questions(state: dict) -> list[dict]:
    """Guided questions: regulatory requirements + SOP/form fields (the bank) + gaps (AI, stored in state)."""
    out = []
    for e in applicable_elements(state):
        if e["kind"] == "proposal":
            continue  # answered by confirming the step-2 proposals, not by a question
        st = element_state(state, e["key"])
        reg = any(s["type"] == "requirement" for s in e["satisfies"])
        a = state.get("assignments", {}).get(e["key"])
        out.append({"id": e["key"], "text": e["question"], "why": e["why"], "satisfies": e["satisfies"],
                    "source": "regulatory" if reg else "sop_form", "kind": e["kind"], "status": st["status"],
                    "answer": {k: st.get(k) for k in ("value", "answer_kind", "by", "at", "revision")} if st["status"] != "missing" else None,
                    "assigned_to": a["to"] if a else None,
                    "evidence": [ev for ev in state.get("evidence", []) if ev["key"] == e["key"]]})
    for g in state.get("gap_questions", []):
        if g.get("dismissed"):
            continue
        st = element_state(state, g["id"])
        a = state.get("assignments", {}).get(g["id"])
        out.append({"id": g["id"], "text": g["text"], "why": g["why"], "satisfies": g["satisfies"], "source": "gap",
                    "kind": "gap", "status": st["status"],
                    "answer": {k: st.get(k) for k in ("value", "answer_kind", "by", "at", "revision")} if st["status"] != "missing" else None,
                    "assigned_to": a["to"] if a else None, "verification": g.get("verification"),
                    "evidence": [ev for ev in state.get("evidence", []) if ev["key"] == g["id"]]})
    return out


def valid_keys(state: dict) -> set:
    return {q["id"] for q in questions(state)} | {e["key"] for e in applicable_elements(state)}
