"""The deviation drafting loop: state machine + every human action. Pure logic over a JSON state document
persisted in SQLite. No network. The gateway orchestrates AI calls and audit logging; this module decides what is
ALLOWED and refuses everything else (nothing advances on its own).

status: in_progress | ready_for_approval | cancelled
step:   proposals -> questions -> checklist -> draft -> ready     (GW-01 intake happens at creation)
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import rules, style
from .errors import WorkflowError

STEPS = ["proposals", "questions", "checklist", "draft", "ready"]
PROPOSAL_ITEMS = ["event_type", "regulations", "sops", "classification"]
PROPOSAL_ELEMENT = {"event_type": "event_type", "regulations": "applicable_regulations", "sops": "applicable_sops",
                    "classification": "classification"}
HUMAN_DECISIONS = ["root_cause", "product_impact", "reportability"]          # AI-05: explicit recorded human decisions
AI_SECTIONS = ["event_summary", "regulatory_and_procedural_context", "precedent_review"]
SECTION_ORDER = ["identification", "event_description", "immediate_action", "classification",
                 "regulatory_and_procedural_context", "precedent_review", "investigation_and_root_cause",
                 "product_impact", "reportability", "other_open_items"]
SECTION_TITLES = {"identification": "Identification", "event_description": "Description of event",
                  "immediate_action": "Immediate action / containment", "classification": "Classification",
                  "regulatory_and_procedural_context": "Regulatory and procedural context",
                  "precedent_review": "Review of similar past records", "investigation_and_root_cause": "Investigation and root cause",
                  "product_impact": "Product and batch impact", "reportability": "Reportability",
                  "other_open_items": "CAPA, recurrence and other items"}
ROLES = {"initiator", "sme", "steward", "qa", "viewer"}
OVERSIGHT = {"steward", "qa"}
VERDICT_OK = "verified"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum(key: str) -> list | None:
    return rules.decision_values(key)


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        try:
            with c:
                c.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, created_by TEXT NOT NULL, "
                          "status TEXT NOT NULL, step TEXT NOT NULL, state TEXT NOT NULL)")
                c.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, "
                          "ts TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)")
                c.execute("CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events "
                          "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
                c.execute("CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
                          "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
        finally:
            c.close()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def transact(self, record_id: str | None, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                events: list = []
                if record_id is None:
                    n = c.execute("SELECT COUNT(*) FROM records").fetchone()[0] + 1
                    state, rid = None, f"WF-{n:04d}"
                else:
                    r = c.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
                    if not r:
                        raise WorkflowError(f"deviation {record_id} not found", 404, code="not_found")
                    state, rid = json.loads(r["state"]), record_id
                result, state = fn(rid, state, events)
                c.execute("INSERT OR REPLACE INTO records(id, created_by, status, step, state) VALUES (?,?,?,?,?)",
                          (rid, state["created_by"], state["status"], state["step"], json.dumps(state)))
                for ev in events:
                    c.execute("INSERT INTO events(record_id, ts, actor, role, action, detail) VALUES (?,?,?,?,?,?)",
                              (rid, ev["ts"], ev["actor"], ev["role"], ev["action"], json.dumps(ev["detail"])))
                c.execute("COMMIT")
                return result
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    def load(self, record_id: str) -> dict:
        c = self._conn()
        try:
            r = c.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
        finally:
            c.close()
        if not r:
            raise WorkflowError(f"deviation {record_id} not found", 404, code="not_found")
        return json.loads(r["state"])

    def list_records(self) -> "list[dict]":
        c = self._conn()
        try:
            return [{"id": r["id"], "created_by": r["created_by"], "status": r["status"], "step": r["step"]}
                    for r in c.execute("SELECT id, created_by, status, step FROM records ORDER BY id")]
        finally:
            c.close()

    def events(self, record_id: str) -> list[dict]:
        c = self._conn()
        try:
            return [{**dict(r), "detail": json.loads(r["detail"])} for r in
                    c.execute("SELECT * FROM events WHERE record_id=? ORDER BY seq", (record_id,))]
        finally:
            c.close()


class Engine:
    def __init__(self, store: Store, clock=utcnow):
        self.store, self.now = store, clock

    # ------------------------------------------------------------------ helpers
    def _ev(self, events, actor, role, action, **detail):
        events.append({"ts": self.now(), "actor": actor, "role": role, "action": action, "detail": detail})

    @staticmethod
    def _open(state: dict):
        if state["status"] == "ready_for_approval":
            raise WorkflowError("this record is marked ready for approval and is frozen (formal review is a later phase)",
                                409, code="frozen")
        if state["status"] == "cancelled":
            raise WorkflowError("this record is cancelled", 409, code="cancelled")

    @staticmethod
    def _owner(state, actor, what):
        if actor != state["created_by"]:
            raise WorkflowError(f"only the initiator ({state['created_by']}) may {what}", 403, code="not_owner")

    @staticmethod
    def _owner_or_oversight(state, actor, role, what):
        if actor != state["created_by"] and role not in OVERSIGHT:
            raise WorkflowError(f"only the initiator or a steward/QA may {what}", 403, code="not_permitted")

    @staticmethod
    def _step_at_least(state, step, what):
        if STEPS.index(state["step"]) < STEPS.index(step):
            raise WorkflowError(f"cannot {what} yet: the record is at step '{state['step']}', this needs step '{step}' "
                                f"or later", 409, required=Engine.next_required(state), code="step_order")

    @staticmethod
    def _touch(state: dict):
        """Any change to decisions or answers after a draft exists makes that draft stale."""
        d = state.get("draft")
        if d and not d.get("stale"):
            d["stale"] = True

    # ------------------------------------------------------------------ GW-01 intake
    def create(self, actor: str, role: str, description: str, fields: dict) -> dict:
        if role not in {"initiator", "steward", "qa"}:
            raise WorkflowError(f"role {role!r} may not start a record", 403, code="role")
        if not (description or "").strip():
            raise WorkflowError("a free-text description is required", 422, code="validation")
        allowed = {"event_type", "site", "product", "lot_number", "equipment_id", "detected_date"}
        extra = set(fields) - allowed
        if extra:
            raise WorkflowError(f"unknown intake fields: {sorted(extra)}", 422, code="validation")
        et = fields.get("event_type")
        if et and et not in rules.bank()["event_types"]:
            raise WorkflowError(f"event_type must be one of {rules.bank()['event_types']}", 422, code="validation")

        def fn(rid, state, events):
            now = self.now()
            state = {"id": rid, "created_by": actor, "created_at": now, "status": "in_progress", "step": "proposals",
                     "answers": {}, "decisions": {}, "proposals": {"generated": False, "items": {}, "decided": {}},
                     "gap": {"generated": False, "waived": None}, "gap_questions": [], "assignments": {}, "evidence": [],
                     "na": {}, "tbd_accepted": {}, "checklist_shown": None, "draft": None, "draft_history": [], "ready": None}
            given = {"description": description.strip(), **{k: v for k, v in fields.items() if v not in (None, "")}}
            for k, v in given.items():
                if k == "event_type":
                    state["intake_event_type"] = v      # the initiator's own guess; confirmed or changed at step 2
                    continue
                state["answers"][k] = {"value": str(v).strip(), "kind": "answer", "revision": 1, "by": actor, "at": now,
                                       "note": "entered at intake", "history": []}
            self._ev(events, actor, role, "create", fields=sorted(given))
            return self.view(state), state

        return self.store.transact(None, fn)

    # ------------------------------------------------------------------ GW-02 proposals
    def store_proposals(self, rid: str, actor: str, role: str, items: dict, ai_event: dict) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "request AI proposals")
            if state["step"] != "proposals":
                raise WorkflowError("AI proposals are generated at step 'proposals'", 409, code="step_order")
            if state["proposals"]["decided"]:
                raise WorkflowError("proposals cannot be regenerated after a decision was recorded on them", 409,
                                    code="already_decided")
            missing = [i for i in PROPOSAL_ITEMS if i not in items]
            if missing:
                raise WorkflowError(f"proposals missing items: {missing}", 422, code="validation")
            state["proposals"].update({"generated": True, "items": items, "ai_event": ai_event, "shown_at": self.now()})
            self._ev(events, actor, role, "proposals_stored", call_id=ai_event.get("call_id"))
            return self.view(state), state

        return self.store.transact(rid, fn)

    @staticmethod
    def _entry_flagged(entry: dict) -> bool:
        v = (entry.get("verification") or {}).get("verdict")
        return v != VERDICT_OK or not (entry.get("validity") or {}).get("ok", True)

    def decide_proposal(self, rid, actor, role, item, action, value=None, rationale="", ack_flag=False) -> dict:
        """GW-02/GW-09: the initiator explicitly confirms or changes EACH proposal. Nothing is auto-confirmed."""
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "confirm or change a proposal")
            if item not in PROPOSAL_ITEMS:
                raise WorkflowError(f"item must be one of {PROPOSAL_ITEMS}", 422, code="validation")
            prop = state["proposals"]
            if not prop["generated"]:
                raise WorkflowError("generate the AI proposals first", 409, required=self.next_required(state), code="step_order")
            if action not in ("confirm", "change"):
                raise WorkflowError("action must be 'confirm' or 'change'", 422, code="validation")
            entries = prop["items"][item].get("entries", [])
            multi = item in ("regulations", "sops")
            if action == "confirm":
                if not entries:
                    raise WorkflowError(f"the AI proposed nothing for {item}; use action 'change' and give a value", 409, code="nothing_to_confirm")
                flagged = [e for e in entries if self._entry_flagged(e)]
                if flagged and not ack_flag:
                    raise WorkflowError("this proposal is flagged (unverified citation or failed check): confirming needs "
                                        "ack_flag=true, or change it", 409, code="flag_needs_ack",
                                        required=[f"acknowledge flagged proposal '{item}' or change it"])
                val = [e["value"] for e in entries] if multi else entries[0]["value"]
                via, ack = "ai_proposal_confirmed", bool(flagged)
            else:
                if value in (None, "", []):
                    raise WorkflowError("action 'change' needs a value", 422, code="validation")
                val, via, ack = value, "ai_proposal_changed", False
                if multi:
                    if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
                        raise WorkflowError(f"{item} must be a list of strings", 422, code="validation")
                else:
                    allowed = _enum(item) or rules.bank().get("event_types")
                    if allowed and val not in allowed:
                        raise WorkflowError(f"{item} must be one of {allowed}", 422, code="validation")
            if action == "change" and not (rationale or "").strip():
                raise WorkflowError("changing an AI proposal needs a rationale", 422, code="validation")
            key = PROPOSAL_ELEMENT[item]
            prev = state["decisions"].get(key)
            state["decisions"][key] = {"value": val, "rationale": (rationale or "").strip(), "tbd": False, "by": actor,
                                       "at": self.now(), "via": via, "proposal_item": item, "ack_flag": ack,
                                       "ai_call_id": (prop.get("ai_event") or {}).get("call_id"),
                                       "revision": (prev["revision"] + 1) if prev else 1,
                                       "history": ([*prev.get("history", []), {k: prev[k] for k in ("value", "by", "at")}] if prev else [])}
            prop["decided"][item] = {"action": action, "by": actor, "at": self.now(), "ack_flag": ack}
            self._touch(state)
            self._ev(events, actor, role, "proposal_decision", item=item, decision=action, value=val, ack_flag=ack,
                     ai_call_id=prop.get("ai_event", {}).get("call_id"))
            return self.view(state), state

        return self.store.transact(rid, fn)

    # ------------------------------------------------------------------ steps (explicit advance only)
    def advance(self, rid, actor, role, to) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "advance the record")
            if to not in STEPS[1:-1]:
                raise WorkflowError(f"'advance' can move to {STEPS[1:-1]}; 'ready' needs the explicit mark-ready action", 422,
                                    code="validation")
            cur = state["step"]
            if STEPS.index(to) != STEPS.index(cur) + 1:
                raise WorkflowError(f"cannot skip: the record is at '{cur}', the next step is "
                                    f"'{STEPS[STEPS.index(cur) + 1] if cur != 'ready' else None}', not '{to}'", 409,
                                    required=self.next_required(state), code="skip")
            problems = self._guard(state, to)
            if problems:
                raise WorkflowError(f"cannot move to '{to}' yet", 409, required=problems, code="guard")
            state["step"] = to
            if to == "checklist":
                cl = rules.checklist(state)
                state["checklist_shown"] = {"at": self.now(), "by": actor, "counts": cl["counts"], "missing": cl["missing"]}
            self._ev(events, actor, role, "advance", frm=cur, to=to)
            return self.view(state), state

        return self.store.transact(rid, fn)

    def _guard(self, state: dict, to: str) -> list[str]:
        p = []
        if to == "questions":
            if not state["proposals"]["generated"]:
                p.append("generate the AI proposals (GW-02)")
            p += [f"confirm or change the proposed {i}" for i in PROPOSAL_ITEMS if i not in state["proposals"]["decided"]]
        elif to == "checklist":
            g = state["gap"]
            if not g["generated"] and not g["waived"]:
                p.append("generate the gap questions, or waive them with a justification (GW-03)")
        elif to == "draft":
            if not state.get("checklist_shown"):
                p.append("review the completeness checklist (GW-05)")
            cl = rules.checklist(state)
            p += [f"answer '{k}' (missing)" for k in cl["missing"]]
        return p

    # ------------------------------------------------------------------ GW-03/04 questions
    def store_gap_questions(self, rid, actor, role, items: list, ai_event: dict) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "request AI gap questions")
            self._step_at_least(state, "questions", "generate gap questions")
            if state["step"] != "questions":
                raise WorkflowError("gap questions are generated at step 'questions'", 409, code="step_order")
            if state["gap"]["generated"]:
                raise WorkflowError("gap questions were already generated for this record", 409, code="already_generated")
            state["gap_questions"] = [{**q, "id": f"g{i}"} for i, q in enumerate(items, 1)]
            state["gap"].update({"generated": True, "ai_event": ai_event, "at": self.now()})
            self._ev(events, actor, role, "gap_questions_stored", n=len(items), call_id=ai_event.get("call_id"))
            return self.view(state), state

        return self.store.transact(rid, fn)

    def waive_gap(self, rid, actor, role, justification) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "waive gap questions")
            self._step_at_least(state, "questions", "waive gap questions")
            if not (justification or "").strip():
                raise WorkflowError("waiving the AI gap questions needs a justification", 422, code="validation")
            state["gap"]["waived"] = {"by": actor, "at": self.now(), "justification": justification.strip()}
            self._ev(events, actor, role, "gap_questions_waived", justification=justification.strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    def dismiss_gap_question(self, rid, actor, role, qid, justification) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "dismiss a question")
            q = next((g for g in state["gap_questions"] if g["id"] == qid), None)
            if not q:
                raise WorkflowError(f"no gap question {qid}", 404, code="not_found")
            if not (justification or "").strip():
                raise WorkflowError("dismissing a question needs a justification", 422, code="validation")
            q["dismissed"] = {"by": actor, "at": self.now(), "justification": justification.strip()}
            self._touch(state)
            self._ev(events, actor, role, "gap_question_dismissed", question=qid, justification=justification.strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    def answer(self, rid, actor, role, key, kind, value="", note="") -> dict:
        """GW-04: an answer, 'unknown' or 'to be determined'. Who may answer: initiator, the assignee, steward/QA."""
        def fn(_id, state, events):
            self._open(state)
            if key in {e["key"] for e in rules.elements() if e["kind"] in ("proposal", "decision")}:
                raise WorkflowError(f"'{key}' is a decision, not a question: use the decision/proposal action", 409, code="is_decision")
            if key not in rules.valid_keys(state):
                raise WorkflowError(f"'{key}' is not a question for this record right now", 404, code="not_found")
            a = state["assignments"].get(key)
            if actor != state["created_by"] and role not in OVERSIGHT and not (a and a["to"] == actor):
                raise WorkflowError("only the initiator, the person the question is assigned to, or a steward/QA may answer", 403,
                                    code="not_permitted")
            if kind not in ("answer", "unknown", "tbd"):
                raise WorkflowError("kind must be answer | unknown | tbd", 422, code="validation")
            if kind == "answer" and not (value or "").strip():
                raise WorkflowError("an answer needs a value (or answer 'unknown' / 'tbd')", 422, code="validation")
            prev = state["answers"].get(key)
            state["answers"][key] = {"value": (value or "").strip() or {"unknown": "unknown", "tbd": "to be determined"}[kind],
                                     "kind": kind, "revision": (prev["revision"] + 1) if prev else 1, "by": actor,
                                     "at": self.now(), "note": note,
                                     "history": ([*prev["history"], {k: prev[k] for k in ("value", "kind", "by", "at")}] if prev else [])}
            state["tbd_accepted"].pop(key, None)         # a new answer voids an earlier 'accepted as TBD'
            self._touch(state)
            self._ev(events, actor, role, "answer", key=key, kind=kind, revision=state["answers"][key]["revision"])
            return self.view(state), state

        return self.store.transact(rid, fn)

    def assign(self, rid, actor, role, key, to_user, to_role) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "assign a question")
            if key not in rules.valid_keys(state):
                raise WorkflowError(f"'{key}' is not a question for this record right now", 404, code="not_found")
            if to_role not in {"initiator", "sme", "steward", "qa"}:
                raise WorkflowError("a question can be assigned to an initiator, SME, steward or QA user", 422, code="validation")
            state["assignments"][key] = {"to": to_user, "by": actor, "at": self.now()}
            self._ev(events, actor, role, "assign", key=key, to=to_user)
            return self.view(state), state

        return self.store.transact(rid, fn)

    def attach_evidence(self, rid, actor, role, key, name, description="", ref="") -> dict:
        """Evidence is a REFERENCE only for now (no real file handling): name, description, a placeholder ref."""
        def fn(_id, state, events):
            self._open(state)
            if key not in rules.valid_keys(state):
                raise WorkflowError(f"'{key}' is not a question for this record right now", 404, code="not_found")
            a = state["assignments"].get(key)
            if actor != state["created_by"] and role not in OVERSIGHT and not (a and a["to"] == actor):
                raise WorkflowError("only the initiator, the assignee or a steward/QA may attach evidence", 403, code="not_permitted")
            if not (name or "").strip():
                raise WorkflowError("evidence needs a name", 422, code="validation")
            ev = {"id": f"ev{len(state['evidence']) + 1}", "key": key, "name": name.strip(), "description": description,
                  "ref": ref or f"placeholder://{rid}/{key}/{name.strip()}", "by": actor, "at": self.now(),
                  "placeholder": True}
            state["evidence"].append(ev)
            self._ev(events, actor, role, "evidence", key=key, name=ev["name"], ref=ev["ref"])
            return self.view(state), state

        return self.store.transact(rid, fn)

    # ------------------------------------------------------------------ GW-05 / AI-05
    def mark_not_applicable(self, rid, actor, role, key, justification) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "mark an element not applicable")
            if key not in {i["key"] for i in rules.checklist(state)["items"]}:
                raise WorkflowError(f"'{key}' is not a checklist element for this record right now", 404, code="not_found")
            if key in ("event_type", "classification"):
                raise WorkflowError(f"'{key}' cannot be not applicable", 409, code="validation")
            if not (justification or "").strip():
                raise WorkflowError("'not applicable' needs a justification", 422, code="validation")
            state["na"][key] = {"justification": justification.strip(), "by": actor, "at": self.now()}
            self._touch(state)
            self._ev(events, actor, role, "not_applicable", key=key, justification=justification.strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    def decide(self, rid, actor, role, key, value="", rationale="", tbd=False) -> dict:
        """AI-05: root cause, product impact and reportability are explicit, recorded human decisions."""
        def fn(_id, state, events):
            self._open(state)
            self._owner_or_oversight(state, actor, role, "record this decision")
            if key not in HUMAN_DECISIONS:
                raise WorkflowError(f"decision must be one of {HUMAN_DECISIONS}", 422, code="validation")
            self._step_at_least(state, "questions", f"record the {key} decision")
            if not tbd:
                if not (value or "").strip():
                    raise WorkflowError("a decision needs a value (or tbd=true)", 422, code="validation")
                allowed = _enum(key)
                if allowed and value not in allowed:
                    raise WorkflowError(f"{key} must be one of {allowed}", 422, code="validation")
                if not (rationale or "").strip():
                    raise WorkflowError("a decision needs a recorded rationale", 422, code="validation")
            prev = state["decisions"].get(key)
            state["decisions"][key] = {"value": value.strip() if value else "to be determined", "rationale": (rationale or "").strip(),
                                       "tbd": bool(tbd), "by": actor, "at": self.now(), "via": "human",
                                       "revision": (prev["revision"] + 1) if prev else 1,
                                       "history": ([*prev.get("history", []), {k: prev[k] for k in ("value", "by", "at")}] if prev else [])}
            state["tbd_accepted"].pop(key, None)
            self._touch(state)
            self._ev(events, actor, role, "decision", key=key, value=state["decisions"][key]["value"], tbd=bool(tbd),
                     rationale=(rationale or "").strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    def accept_tbd(self, rid, actor, role, key, justification) -> dict:
        """A person accepts 'to be determined at investigation' for an element. Logged like a flagged-content resolution."""
        def fn(_id, state, events):
            self._open(state)
            self._owner_or_oversight(state, actor, role, "accept a to-be-determined item")
            st = rules.element_state(state, key)
            if st["status"] != "tbd":
                raise WorkflowError(f"'{key}' is not a to-be-determined item (status: {st['status']})", 409, code="not_tbd")
            if not (justification or "").strip():
                raise WorkflowError("accepting 'to be determined at investigation' needs a justification", 422, code="validation")
            state["tbd_accepted"][key] = {"by": actor, "at": self.now(), "justification": justification.strip()}
            self._touch(state)
            self._ev(events, actor, role, "tbd_accepted", key=key, justification=justification.strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    # ------------------------------------------------------------------ draft
    def sources_for(self, rid: str) -> list[dict]:
        """Human-entered information as citable sources (answers + decisions), for AI prompts and citation checks."""
        state = self.store.load(rid)
        out = []
        for key in sorted(set(state["answers"]) | set(state["decisions"])):
            s = self.answer_source(state, key)
            if s:
                out.append(s)
        return out

    def answer_source(self, state: dict, key: str) -> dict | None:
        d = state["decisions"].get(key)
        a = state["answers"].get(key)
        src = d or a
        if not src:
            return None
        text = src["value"] if not d else (f"{src['value']}. Basis: {src['rationale']}" if src.get("rationale") else str(src["value"]))
        if isinstance(src["value"], list):
            text = ", ".join(src["value"]) + (f". Basis: {src['rationale']}" if src.get("rationale") else "")
        return {"type": "answer", "source_id": f"answer:{state['id']}:{key}", "version": str(src["revision"]),
                "title": f"{rules.element(key)['label'] if rules.element(key) else key} (entered by {src['by']})",
                "effective": None, "status": "human-entered", "link": f"/deviations/{state['id']}/answers/{key}",
                "passages": [{"locator": "value", "text": text}], "score": 5.0,
                "authored_by": src["by"], "at": src["at"], "kind": "decision" if d else a["kind"]}

    def _human_statements(self, state: dict, version: int) -> list[dict]:
        out = []
        n = 0
        cl = rules.checklist(state)
        labels = {i["key"]: i for i in cl["items"]}
        order = {e["key"]: i for i, e in enumerate(rules.elements())}
        sec_of = {e["key"]: e["section"] for e in rules.elements()}
        for key, item in sorted(labels.items(), key=lambda kv: (order.get(kv[0], 999), kv[0])):
            st = item["status"]
            if st == "missing":
                continue
            if st == "not_applicable":
                text, by = f"{item['label']}: not applicable. {item['justification']}", item["by"]
            else:
                val = item.get("value")
                val = ", ".join(val) if isinstance(val, list) else val
                if st in ("tbd", "tbd_accepted"):
                    text = f"{item['label']}: to be determined"
                    if st == "tbd_accepted":
                        text += f" (accepted as to be determined at investigation by {item['accepted_by']}: {item['justification']})"
                    else:
                        text += " (not yet accepted)"
                else:
                    dec = state["decisions"].get(key)
                    text = f"{item['label']}: {val}" + (f". Basis: {dec['rationale']}" if dec and dec.get("rationale") else "")
                by = item.get("by")
            src = self.answer_source(state, key)
            n += 1
            out.append({"id": f"h{version}-{n}", "section": sec_of.get(key, "other_open_items"), "text": text, "element": key,
                        "author": {"type": "human", "user": by}, "status": "human_entered",
                        "sources": ([{"type": "answer", "ref": src["source_id"], "version": src["version"], "locator": "value",
                                      "link": src["link"]}] if src else []),
                        "verification": None, "style_flags": style.check(text)})
        return out

    def store_draft(self, rid, actor, role, ai_sections: dict, ai_event: dict) -> dict:
        """The AI-written sections arrive already citation-verified by the gateway. Everything the AI wrote starts
        PENDING and visibly marked (DI-05); it never becomes part of the record until a person accepts it."""
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "generate the draft")
            if state["step"] != "draft":
                raise WorkflowError("the draft is generated at step 'draft'", 409, required=self.next_required(state), code="step_order")
            cur = state["draft"]
            if cur and not cur.get("stale"):
                raise WorkflowError("a current draft already exists; change an answer or decision to make it stale before "
                                    "regenerating", 409, code="draft_current")
            version = (cur["version"] + 1) if cur else 1
            if cur:
                state["draft_history"].append({**cur, "superseded_at": self.now(), "superseded_by_version": version})
            stmts = self._human_statements(state, version)
            n = 0
            for sec in AI_SECTIONS:
                for s in ai_sections.get(sec, []):
                    n += 1
                    v = s.get("verification") or {}
                    flags = style.check(s["text"])
                    stmts.append({"id": f"a{version}-{n}", "section": "event_description" if sec == "event_summary" else sec,
                                  "ai_section": sec, "text": s["text"], "element": None,
                                  "author": {"type": "ai", "model": ai_event.get("model"), "digest": ai_event.get("digest"),
                                             "config_version": ai_event.get("config_version"), "call_id": ai_event.get("call_id")},
                                  "status": "pending", "sources": s.get("citations", []), "verification": {
                                      "verdict": v.get("verdict"), "score": v.get("score"), "reasons": v.get("reasons", [])},
                                  "style_flags": flags, "needs_ack": v.get("verdict") != VERDICT_OK or bool(flags)})
            state["draft"] = {"version": version, "generated_at": self.now(), "generated_by": actor, "ai_event": ai_event,
                              "statements": stmts, "stale": False, "ai_notice": ai_event.get("notice")}
            self._ev(events, actor, role, "draft_stored", version=version, ai_statements=n, call_id=ai_event.get("call_id"))
            return self.view(state), state

        return self.store.transact(rid, fn)

    def decide_statement(self, rid, actor, role, sid, action, text="", ack_flag=False) -> dict:
        """A person accepts, edits or rejects each AI-written statement. Flagged ones need an explicit acknowledgement."""
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "accept, edit or reject AI-written text")
            d = state["draft"]
            if not d:
                raise WorkflowError("there is no draft yet", 409, required=self.next_required(state), code="step_order")
            s = next((x for x in d["statements"] if x["id"] == sid), None)
            if not s:
                raise WorkflowError(f"no statement {sid}", 404, code="not_found")
            if s["author"]["type"] != "ai" or s["status"] != "pending":
                raise WorkflowError("only pending AI-written statements need a decision", 409, code="not_pending")
            if d.get("stale"):
                raise WorkflowError("this draft is stale (an answer or decision changed): regenerate it first", 409,
                                    required=["regenerate the draft"], code="stale")
            if action == "accept":
                if s["needs_ack"] and not ack_flag:
                    raise WorkflowError("this statement is flagged (" + ", ".join(
                        [s["verification"]["verdict"] or "unverified", *[f["term"] for f in s["style_flags"]]]) +
                        "): accepting needs ack_flag=true, or edit/reject it", 409, code="flag_needs_ack",
                        required=[f"acknowledge, edit or reject flagged statement {sid}"])
                s["status"], s["acknowledged"] = "accepted", bool(s["needs_ack"])
            elif action == "edit":
                if not (text or "").strip():
                    raise WorkflowError("an edit needs the new text", 422, code="validation")
                s["ai_original"] = {"text": s["text"], "author": s["author"], "verification": s["verification"]}
                s["text"], s["status"] = text.strip(), "edited"
                s["author"] = {"type": "human", "user": actor, "derived_from_ai": True}   # now attributable to the person
                s["style_flags"] = style.check(s["text"])
            elif action == "reject":
                s["status"] = "rejected"
            else:
                raise WorkflowError("action must be accept | edit | reject", 422, code="validation")
            s["decided_by"], s["decided_at"] = actor, self.now()
            self._ev(events, actor, role, "statement_decision", statement=sid, decision=action,
                     ack_flag=bool(ack_flag), verdict=(s.get("verification") or {}).get("verdict") if action != "edit"
                     else (s["ai_original"]["verification"] or {}).get("verdict"), ai_call_id=d["ai_event"].get("call_id"))
            return self.view(state), state

        return self.store.transact(rid, fn)

    # ------------------------------------------------------------------ GW-09 mark ready / cancel
    def ready_blockers(self, state: dict) -> list[str]:
        p = []
        if state["step"] != "draft":
            p.append(f"the record must be at step 'draft' (it is at '{state['step']}')")
        d = state["draft"]
        if not d:
            p.append("generate the draft")
        elif d.get("stale"):
            p.append("the draft is stale (an answer or decision changed): regenerate it")
        else:
            p += [f"decide AI-written statement {s['id']} (accept / edit / reject)" for s in d["statements"]
                  if s["author"]["type"] == "ai" and s["status"] == "pending"]
        cl = rules.checklist(state)
        p += [f"answer '{k}' (missing)" for k in cl["missing"]]
        p += [f"answer '{k}' or accept it as 'to be determined at investigation'" for k in cl["tbd_unaccepted"]]
        for k in HUMAN_DECISIONS:
            if rules.element_state(state, k)["status"] == "missing":
                p.append(f"record the {k} decision (AI-05)")
        for k in ("event_type", "classification"):
            if k not in state["decisions"]:
                p.append(f"confirm the {k}")
        return list(dict.fromkeys(p))

    def mark_ready(self, rid, actor, role) -> dict:
        def fn(_id, state, events):
            self._open(state)
            self._owner(state, actor, "mark the draft ready for approval")
            problems = self.ready_blockers(state)
            if problems:
                raise WorkflowError("cannot mark ready for approval yet", 409, required=problems, code="not_ready")
            state["status"], state["step"] = "ready_for_approval", "ready"
            state["ready"] = {"by": actor, "at": self.now(), "draft_version": state["draft"]["version"]}
            self._ev(events, actor, role, "mark_ready", draft_version=state["draft"]["version"])
            return self.view(state), state

        return self.store.transact(rid, fn)

    def cancel(self, rid, actor, role, reason) -> dict:
        def fn(_id, state, events):
            if state["status"] == "cancelled":
                raise WorkflowError("already cancelled", 409, code="cancelled")
            self._owner_or_oversight(state, actor, role, "cancel this record")
            if state["status"] == "ready_for_approval" and role != "qa":
                raise WorkflowError("a record that is ready for approval can only be cancelled by QA", 403, code="not_permitted")
            if not (reason or "").strip():
                raise WorkflowError("cancelling needs a reason", 422, code="validation")
            state["status"] = "cancelled"
            state["cancelled"] = {"by": actor, "at": self.now(), "reason": reason.strip()}
            self._ev(events, actor, role, "cancel", reason=reason.strip())
            return self.view(state), state

        return self.store.transact(rid, fn)

    # ------------------------------------------------------------------ views
    @staticmethod
    def next_required(state: dict) -> list[str]:
        """What a human must do next (never done automatically)."""
        if state["status"] != "in_progress":
            return []
        step, out = state["step"], []
        if step == "proposals":
            if not state["proposals"]["generated"]:
                return ["request the AI proposals (GW-02)"]
            out = [f"confirm or change the proposed {i}" for i in PROPOSAL_ITEMS if i not in state["proposals"]["decided"]]
            return out or ["advance to step 'questions'"]
        if step == "questions":
            g = state["gap"]
            if not g["generated"] and not g["waived"]:
                out.append("generate the gap questions (or waive them with a justification)")
            cl = rules.checklist(state)
            out += [f"answer '{k}'" for k in cl["missing"]]
            return out + ["advance to step 'checklist'"]
        if step == "checklist":
            cl = rules.checklist(state)
            return [f"answer '{k}' (missing)" for k in cl["missing"]] or ["advance to step 'draft'"]
        if step == "draft":
            d = state["draft"]
            if not d or d.get("stale"):
                return ["generate the draft" if not d else "regenerate the stale draft"]
            return _ready_blockers_static(state) or ["mark the draft ready for approval"]
        return out

    def view(self, state: dict) -> dict:
        cl = rules.checklist(state)
        d = state.get("draft")
        return {"id": state["id"], "status": state["status"], "step": state["step"], "created_by": state["created_by"],
                "created_at": state["created_at"], "intake_event_type": state.get("intake_event_type"),
                "answers": {k: {f: v[f] for f in ("value", "kind", "revision", "by", "at")} for k, v in state["answers"].items()},
                "decisions": {k: {f: v.get(f) for f in ("value", "rationale", "tbd", "by", "at", "via", "revision", "ack_flag")}
                              for k, v in state["decisions"].items()},
                "proposals": {"generated": state["proposals"]["generated"], "items": state["proposals"].get("items", {}),
                              "decided": state["proposals"]["decided"], "ai_event": state["proposals"].get("ai_event")},
                "gap": state["gap"], "questions": rules.questions(state), "checklist_summary": {
                    "counts": cl["counts"], "missing": cl["missing"], "tbd_unaccepted": cl["tbd_unaccepted"]},
                "tbd_accepted": state["tbd_accepted"], "na": state["na"], "checklist_shown": state.get("checklist_shown"),
                "draft": None if not d else {"version": d["version"], "stale": d["stale"], "generated_at": d["generated_at"],
                                             "pending_ai_statements": sum(1 for s in d["statements"] if s["author"]["type"] == "ai" and s["status"] == "pending")},
                "ready": state.get("ready"), "cancelled": state.get("cancelled"), "next_required": self.next_required(state)}

    def get(self, rid: str) -> dict:
        return self.view(self.store.load(rid))

    def checklist_view(self, rid: str) -> dict:
        return rules.checklist(self.store.load(rid))

    def draft_view(self, rid: str) -> dict:
        state = self.store.load(rid)
        d = state.get("draft")
        if not d:
            raise WorkflowError("no draft has been generated yet", 404, code="no_draft")
        by_sec: dict[str, list] = {}
        for s in d["statements"]:
            by_sec.setdefault(s["section"], []).append(s)
        return {"id": rid, "version": d["version"], "stale": d["stale"], "status": state["status"],
                "ai_notice": d.get("ai_notice"), "legend": "Statements marked AI are written by the AI and are NOT part of "
                "the record until a person accepts them; edited statements are attributed to the editor.",
                "sections": [{"section": k, "title": SECTION_TITLES[k], "statements": by_sec.get(k, [])}
                             for k in SECTION_ORDER if by_sec.get(k)],
                "rejected": [s for s in d["statements"] if s["status"] == "rejected"],
                "ready_blockers": self.ready_blockers(state) if state["status"] == "in_progress" else []}

    def source(self, rid: str, key: str) -> dict:
        state = self.store.load(rid)
        s = self.answer_source(state, key)
        if not s:
            raise WorkflowError(f"no answer or decision '{key}'", 404, code="not_found")
        return s


def _ready_blockers_static(state: dict) -> list[str]:
    """ready_blockers without needing an Engine instance (used by next_required)."""
    return Engine.ready_blockers(Engine.__new__(Engine), state)
