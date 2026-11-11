"""The guided deviation drafting loop (Phase 4), gateway side.

The gateway is the front door: it authenticates the (simulated) user, runs retrieval and the local model for the three
AI steps (proposals GW-02, gap questions GW-03, draft GW-06), has the citation verifier check every AI claim (AI-01/02),
forwards every human action to the `workflow` service - which owns the record and the state machine and REFUSES any
skipped step (GW-09) - and writes every step to the hash-chained audit log (DI-03): what the AI produced, what was
shown, and what the person did with it.

Nothing here advances a record by itself: AI output is stored as a proposal/draft, and each step needs an explicit
human action (AI-05)."""
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import allowlist, retrieval
from .ai_config import ConfigNotApproved, ModelNotApproved, load_approved
from .auth import USERS, User, get_user
from .llm import DigestMismatch, LlmError, ModelNotInstalled, OllamaClient
from .registry import Registry
from .sources import SourceFetchError, fetch_source, sop_source

router = APIRouter(prefix="/deviations", tags=["deviations"])


class AiTaskError(Exception):
    def __init__(self, message: str, status: int, detail: dict | None = None):
        super().__init__(message)
        self.status, self.detail = status, detail or {"detail": message}


# ------------------------------------------------------------------------------------------------ workflow calls
def _wf(method: str, path: str, payload: dict | None = None, get=None, post=None):
    get = get or allowlist.service_get
    post = post or allowlist.service_post
    r = get("workflow", path) if method == "GET" else post("workflow", path, payload or {})
    if r.status_code == 200:
        return r.json()
    try:
        body = r.json()
    except Exception:
        body = {"detail": f"workflow returned HTTP {r.status_code}"}
    raise AiTaskError(body.get("detail", "refused"), r.status_code, body)


def _who(user: User, **extra) -> dict:
    return {"actor": user.name, "role": user.role, **extra}


def _audit(reg: Registry, user: User, action: str, rid: str, /, **detail):
    reg.log(user.name, f"deviation.{action}", f"deviation:{rid}", {"role": user.role, **detail})


def _human(reg: Registry, user: User, rid: str, action: str, path: str, payload: dict, log: dict | None = None, method="POST"):
    """Forward one explicit human action to the workflow; audit the outcome (accepted or refused)."""
    try:
        out = _wf(method, path, {**_who(user), **payload})
    except AiTaskError as e:
        _audit(reg, user, "refused", rid, attempted=action, status=e.status, code=e.detail.get("code"),
               reason=str(e), required=e.detail.get("required"), request=log if log is not None else payload)
        raise HTTPException(e.status, e.detail)
    _audit(reg, user, action, rid, request=log if log is not None else payload,
           result={"status": out.get("status"), "step": out.get("step")})
    return out


# ------------------------------------------------------------------------------------------------ the AI task runner
def run_ai_task(task: str, user: User, rid: str, query: str, extra_sources: list, prompt_path: str, prompt_payload: dict,
                parse_path: str, parse_payload: dict, model: str | None, get=None, post=None, client=None, cfg=None) -> dict:
    """retrieve -> prompt (workflow) -> local model (gateway only) -> parse (workflow) -> fetch cited sources ->
    verify (workflow). Returns everything the caller needs to store the result and to write the DI-03 log entry."""
    get = get or allowlist.service_get
    post = post or allowlist.service_post
    started = time.time()
    call_id = uuid.uuid4().hex
    try:
        cfg = cfg or load_approved()
        model_name, chosen_by = cfg.resolve_model(model)
    except ConfigNotApproved as e:
        raise AiTaskError(str(e), 503)
    except ModelNotApproved as e:
        raise AiTaskError(str(e), 400)
    tcfg = cfg.tasks[task]
    rcfg = {**cfg.retrieval, **{k: v for k, v in tcfg.items() if k in ("max_sources", "max_context_chars")}}
    try:
        library_version = retrieval.library_version(get)
        knowledge_snapshot = retrieval.knowledge_snapshot(get)
        found = retrieval.retrieve(query, rcfg, get) if query else {"sources": [], "notes": []}
    except retrieval.RetrievalError as e:
        raise AiTaskError(f"retrieval failed: {e}", 502)
    sources = [*extra_sources, *found["sources"]]

    base = {"sources": sources, "system_prompt": cfg.system_prompt, "max_sources": rcfg["max_sources"],
            "max_context_chars": rcfg["max_context_chars"], "passage_template": cfg.prompt["passage_template"],
            "instructions": tcfg["instructions"]}
    prompt = _wf("POST", prompt_path, {**base, **prompt_payload}, get, post)
    client = client or OllamaClient(cfg, get=get, post=post, model=model_name)
    try:
        info = client.model_info()
        gen = client.chat(prompt["messages"], prompt["schema"])
    except (ModelNotInstalled, DigestMismatch) as e:
        raise AiTaskError(str(e), 503)
    except LlmError as e:
        raise AiTaskError(str(e), 502)

    parsed = _wf("POST", parse_path, {"raw": gen["content"], **parse_payload}, get, post)
    ai_event = {"call_id": call_id, "task": task, "model": info["name"], "digest": info.get("digest"),
                "model_chosen_by": chosen_by, "config_version": cfg.version, "config_sha256": cfg.sha256,
                "library_version": library_version, "knowledge_snapshot": knowledge_snapshot,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    log = {"call_id": call_id, "task": task, "user": user.name, "record": rid, "input": {"query": query},
           "prompt_messages": prompt["messages"], "retrieved_source_ids": prompt["context_source_ids"],
           "retrieval_notes": found.get("notes", []), "model": info, "model_selection": {"requested": model, "used": model_name,
                                                                                         "chosen_by": chosen_by},
           "config": cfg.identity(), "settings": {"retrieval": rcfg, "generation": cfg.generation_for(model_name),
                                                  "verifier": cfg.verifier, "task_instructions": tcfg["instructions"]},
           "library_version": library_version, "knowledge_snapshot": knowledge_snapshot, "raw_output": gen["content"],
           "model_call": {k: gen.get(k) for k in ("eval_count", "prompt_eval_count", "total_duration", "load_duration")}}
    out = {"ai_event": ai_event, "log": log, "parsed": parsed, "results": [], "retrieved": prompt["context_source_ids"],
           "started": started}
    if not parsed.get("ok"):
        return out
    resolved: dict = {}
    try:
        for c in parsed.get("cited", []):
            key = c["source_id"] + ("@" + c["version"].lstrip("vV") if c["version"] and not c["source_id"].startswith("req:") else "")
            resolved[key] = fetch_source(c["source_id"], c["version"], get)
    except SourceFetchError as e:
        raise AiTaskError(f"could not check cited sources: {e}", 502)
    ver = _wf("POST", "/internal/verify", {"statements": parsed["statements"], "sources": resolved,
                                           "thresholds": cfg.verifier}, get, post)
    out["results"] = ver["results"]
    log["cited_sources_resolved"] = {k: v is not None for k, v in resolved.items()}
    return out


def _ver(r: dict) -> dict:
    return {"verdict": r["verdict"], "score": r["score"], "reasons": r["reasons"]}


def _cits(r: dict) -> list[dict]:
    """Citations as shown to the person: the document, version, section, a quote and a link (AI-01)."""
    return [{k: c.get(k) for k in ("source_id", "version", "locator", "quote", "status", "reason", "link")} for c in r["citations"]]


def _ai_fail(reg, user, rid, task, e: AiTaskError):
    _audit(reg, user, f"{task}_failed", rid, error=str(e), status=e.status)
    raise HTTPException(e.status, e.detail)


def _precheck(reg, user, rid, what: str, step: str):
    """Fail fast (before spending minutes of CPU on a model call) if the human step is not currently allowed."""
    try:
        rec = _wf("GET", f"/internal/deviations/{rid}")
    except AiTaskError as e:
        raise HTTPException(e.status, e.detail)
    problems = []
    if rec["status"] != "in_progress":
        problems.append(f"record is {rec['status']}")
    if rec["created_by"] != user.name:
        problems.append(f"only the initiator ({rec['created_by']}) may {what}")
    if rec["step"] != step:
        problems.append(f"{what} happens at step '{step}'; the record is at '{rec['step']}'")
    if problems:
        status = 409 if rec["created_by"] == user.name else 403
        _audit(reg, user, "refused", rid, attempted=what, status=status, code="precheck", reason="; ".join(problems),
               required=rec["next_required"])
        raise HTTPException(status, {"detail": "; ".join(problems), "code": "precheck", "required": rec["next_required"]})
    return rec


def _intake(rec: dict) -> dict:
    return {**({"event_type (initiator's own guess)": rec.get("intake_event_type")} if rec.get("intake_event_type") else {}),
            **{k: v["value"] for k, v in rec["answers"].items()}}


# ------------------------------------------------------------------------------------------------ request bodies
class CreateIn(BaseModel):
    description: str
    event_type: str | None = None
    site: str | None = None
    product: str | None = None
    lot_number: str | None = None
    equipment_id: str | None = None
    detected_date: str | None = None


class GenerateIn(BaseModel):
    model: str | None = None


class DecideProposalIn(BaseModel):
    action: str
    value: list[str] | str | None = None
    rationale: str = ""
    ack_flag: bool = False


class AdvanceIn(BaseModel):
    to: str


class JustificationIn(BaseModel):
    justification: str = ""


class AnswerIn(BaseModel):
    kind: str = "answer"
    value: str = ""
    note: str = ""


class AssignIn(BaseModel):
    to_user: str


class EvidenceIn(BaseModel):
    name: str
    description: str = ""
    ref: str = ""


class DecisionIn(BaseModel):
    value: str = ""
    rationale: str = ""
    tbd: bool = False


class StatementIn(BaseModel):
    action: str
    text: str = ""
    ack_flag: bool = False


class CancelIn(BaseModel):
    reason: str = ""


# ------------------------------------------------------------------------------------------------ routes
def register(get_registry):
    """Attach the routes (called from main.py with the app's Registry dependency)."""

    @router.post("", status_code=201)
    def create(body: CreateIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        """GW-01: free-text description + minimal fields. Nothing is inferred or filled in for the initiator."""
        fields = {k: v for k, v in body.model_dump().items() if k != "description" and v}
        try:
            out = _wf("POST", "/internal/deviations", {**_who(user), "description": body.description, "fields": fields})
        except AiTaskError as e:
            reg.log(user.name, "deviation.refused", "deviation:new", {"attempted": "create", "reason": str(e), "role": user.role,
                                                                       "status": e.status, "code": e.detail.get("code")})
            raise HTTPException(e.status, e.detail)
        _audit(reg, user, "create", out["id"], request=body.model_dump(), result={"status": out["status"], "step": out["step"]})
        return out

    @router.get("")
    def list_all(_: User = Depends(get_user)):
        try:
            return _wf("GET", "/internal/deviations")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    @router.get("/{rid}")
    def get_one(rid: str, _: User = Depends(get_user)):
        try:
            return _wf("GET", f"/internal/deviations/{rid}")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    @router.get("/{rid}/events")
    def events(rid: str, _: User = Depends(get_user)):
        try:
            return _wf("GET", f"/internal/deviations/{rid}/events")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    @router.get("/{rid}/answers/{key}")
    def answer_source(rid: str, key: str, _: User = Depends(get_user)):
        """The link target for a statement that cites what a person entered (GW-06)."""
        try:
            return _wf("GET", f"/internal/deviations/{rid}/sources/{key}")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    # ---- GW-02: AI proposals, then explicit confirm/change of EACH
    @router.post("/{rid}/proposals/generate")
    def generate_proposals(rid: str, body: GenerateIn | None = None, reg: Registry = Depends(get_registry),
                           user: User = Depends(get_user)):
        rec = _precheck(reg, user, rid, "request AI proposals", "proposals")
        if rec["proposals"]["generated"] and rec["proposals"]["decided"]:
            raise HTTPException(409, {"detail": "proposals already have recorded decisions", "code": "already_decided"})
        try:
            cfg = load_approved()
            known = [m["requirement_id"] for m in retrieval._json(allowlist.service_get("knowledge", "/traceability"), "traceability")]
            eff = [d["sop_number"] for d in retrieval._json(allowlist.service_get("knowledge", "/documents"), "documents")]
            enums = _bank_enums()
            intake = _intake(rec)
            answers = _wf("GET", f"/internal/deviations/{rid}/sources")
            query = " ".join(str(v) for v in intake.values())
            res = run_ai_task("proposals", user, rid, query, answers, "/internal/tasks/proposals/prompt",
                              {"intake": intake, "event_types": enums["event_types"], "classes": enums["classification"]},
                              "/internal/tasks/proposals/parse",
                              {"event_types": enums["event_types"], "classes": enums["classification"],
                               "known_requirements": known, "effective_sops": eff}, body.model if body else None, cfg=cfg)
        except AiTaskError as e:
            _ai_fail(reg, user, rid, "proposals", e)
        except ConfigNotApproved as e:
            _ai_fail(reg, user, rid, "proposals", AiTaskError(str(e), 503))
        parsed = res["parsed"]
        if not parsed.get("ok"):
            _audit(reg, user, "proposals_unusable", rid, **res["log"], error=parsed.get("error"))
            raise HTTPException(502, {"detail": f"the model's output could not be used: {parsed.get('error')}", "code": "unusable",
                                      "required": ["retry the AI proposals, or enter each proposal yourself with 'change'"],
                                      "call_id": res["ai_event"]["call_id"]})
        items = parsed["items"]
        for st, r in zip(parsed["statements"], res["results"]):
            e = items[st["item"]]["entries"][st["index"]]
            e["verification"], e["citations"] = _ver(r), _cits(r)
        for it in items.values():
            for e in it["entries"]:
                e.setdefault("verification", {"verdict": "unsupported", "score": None, "reasons": ["no_citation"]})
        out = _wf("POST", f"/internal/deviations/{rid}/proposals", {**_who(user), "items": items, "ai_event": res["ai_event"]})
        _audit(reg, user, "proposals_generated", rid, **res["log"], output={"items": items},
               duration_s=round(time.time() - res["started"], 3))
        return out

    @router.post("/{rid}/proposals/{item}/decide")
    def decide_proposal(rid: str, item: str, body: DecideProposalIn, reg: Registry = Depends(get_registry),
                        user: User = Depends(get_user)):
        try:
            rec = _wf("GET", f"/internal/deviations/{rid}")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)
        call_id = (rec["proposals"].get("ai_event") or {}).get("call_id")
        return _human(reg, user, rid, "proposal_decision", f"/internal/deviations/{rid}/proposals/{item}/decide",
                      body.model_dump(), {"item": item, **body.model_dump(), "ai_call_id": call_id})

    @router.post("/{rid}/advance")
    def advance(rid: str, body: AdvanceIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        out = _human(reg, user, rid, "advance", f"/internal/deviations/{rid}/advance", body.model_dump(), body.model_dump())
        if body.to == "checklist":   # GW-05: the checklist the person was shown is part of the record of the step
            _audit(reg, user, "checklist_shown", rid, summary=out["checklist_summary"])
        return out

    # ---- GW-03/04: guided questions
    @router.post("/{rid}/questions/generate")
    def generate_gap_questions(rid: str, body: GenerateIn | None = None, reg: Registry = Depends(get_registry),
                               user: User = Depends(get_user)):
        rec = _precheck(reg, user, rid, "request AI gap questions", "questions")
        if rec["gap"]["generated"]:
            raise HTTPException(409, {"detail": "gap questions were already generated", "code": "already_generated"})
        try:
            intake = {**_intake(rec), **{k: str(v["value"]) for k, v in rec["decisions"].items() if not v.get("tbd")}}
            answers = _wf("GET", f"/internal/deviations/{rid}/sources")
            req_ids = [d for d in rec["decisions"].get("applicable_regulations", {}).get("value", []) or []]
            query = " ".join(str(v) for v in intake.values())
            extra = list(answers)
            for rq in req_ids:
                extra += retrieval.retrieve(rq, load_approved().retrieval)["sources"]
            res = run_ai_task("gap_questions", user, rid, query, extra, "/internal/tasks/gap/prompt",
                              {"intake": intake, "existing_questions": [q["text"] for q in rec["questions"]]},
                              "/internal/tasks/gap/parse", {"max_questions": load_approved().tasks["gap_questions"].get("max_questions", 6)},
                              body.model if body else None)
        except AiTaskError as e:
            _ai_fail(reg, user, rid, "gap_questions", e)
        parsed = res["parsed"]
        if not parsed.get("ok"):
            _audit(reg, user, "gap_questions_unusable", rid, **res["log"], error=parsed.get("error"))
            raise HTTPException(502, {"detail": f"the model's output could not be used: {parsed.get('error')}", "code": "unusable",
                                      "required": ["retry, or waive the AI gap questions with a justification"]})
        items = []
        for q, r in zip(parsed["questions"], res["results"]):
            items.append({"text": q["text"], "why": q["why"], "satisfies": _cits(r), "verification": _ver(r)})
        out = _wf("POST", f"/internal/deviations/{rid}/gap-questions", {**_who(user), "items": items, "ai_event": res["ai_event"]})
        _audit(reg, user, "gap_questions_generated", rid, **res["log"], output={"questions": items, "no_gaps": parsed.get("no_gaps")},
               duration_s=round(time.time() - res["started"], 3))
        return out

    @router.post("/{rid}/questions/waive")
    def waive(rid: str, body: JustificationIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "gap_questions_waived", f"/internal/deviations/{rid}/gap-questions/waive", body.model_dump())

    @router.post("/{rid}/questions/{qid}/dismiss")
    def dismiss(rid: str, qid: str, body: JustificationIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "gap_question_dismissed", f"/internal/deviations/{rid}/gap-questions/{qid}/dismiss",
                      body.model_dump(), {"question": qid, **body.model_dump()})

    @router.post("/{rid}/answers/{key}")
    def answer(rid: str, key: str, body: AnswerIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "answer", f"/internal/deviations/{rid}/answers/{key}", body.model_dump(),
                      {"key": key, **body.model_dump()})

    @router.post("/{rid}/assign/{key}")
    def assign(rid: str, key: str, body: AssignIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        to_role = USERS.get(body.to_user)
        if not to_role:
            raise HTTPException(422, {"detail": f"unknown user {body.to_user!r}", "code": "validation"})
        return _human(reg, user, rid, "assign", f"/internal/deviations/{rid}/assign/{key}",
                      {"to_user": body.to_user, "to_role": to_role}, {"key": key, "to": body.to_user})

    @router.post("/{rid}/evidence/{key}")
    def evidence(rid: str, key: str, body: EvidenceIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "evidence", f"/internal/deviations/{rid}/evidence/{key}", body.model_dump(),
                      {"key": key, **body.model_dump()})

    @router.post("/{rid}/not-applicable/{key}")
    def not_applicable(rid: str, key: str, body: JustificationIn, reg: Registry = Depends(get_registry),
                       user: User = Depends(get_user)):
        return _human(reg, user, rid, "not_applicable", f"/internal/deviations/{rid}/not-applicable/{key}", body.model_dump(),
                      {"key": key, **body.model_dump()})

    # ---- AI-05: explicit, recorded human decisions
    @router.post("/{rid}/decisions/{key}")
    def decision(rid: str, key: str, body: DecisionIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "decision", f"/internal/deviations/{rid}/decisions/{key}", body.model_dump(),
                      {"key": key, **body.model_dump()})

    @router.post("/{rid}/tbd-accept/{key}")
    def tbd_accept(rid: str, key: str, body: JustificationIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "tbd_accepted", f"/internal/deviations/{rid}/tbd-accept/{key}", body.model_dump(),
                      {"key": key, **body.model_dump()})

    @router.get("/{rid}/checklist")
    def checklist(rid: str, _: User = Depends(get_user)):
        try:
            return _wf("GET", f"/internal/deviations/{rid}/checklist")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    # ---- GW-06: the draft. AI-written text is marked until a person accepts it (DI-05)
    @router.post("/{rid}/draft/generate")
    def generate_draft(rid: str, body: GenerateIn | None = None, reg: Registry = Depends(get_registry),
                       user: User = Depends(get_user)):
        rec = _precheck(reg, user, rid, "generate the draft", "draft")
        if rec["draft"] and not rec["draft"]["stale"]:
            raise HTTPException(409, {"detail": "a current draft already exists", "code": "draft_current",
                                      "required": ["change an answer or decision to make it stale, or decide the pending statements"]})
        try:
            cfg = load_approved()
            answers = _wf("GET", f"/internal/deviations/{rid}/sources")
            extra = list(answers)
            desc = rec["answers"].get("description", {}).get("value", "")
            for rq in (rec["decisions"].get("applicable_regulations", {}).get("value") or []):
                extra += retrieval.retrieve(rq, cfg.retrieval)["sources"]
            for sn in (rec["decisions"].get("applicable_sops", {}).get("value") or []):
                r = allowlist.service_get("knowledge", f"/documents/{sn}")
                if r.status_code == 200:
                    extra.append(sop_source(r.json(), score=50.0))
            res = run_ai_task("draft", user, rid, desc, extra, "/internal/tasks/draft/prompt", {}, "/internal/tasks/draft/parse", {},
                              body.model if body else None, cfg=cfg)
        except AiTaskError as e:
            _ai_fail(reg, user, rid, "draft", e)
        parsed = res["parsed"]
        if not parsed.get("ok"):
            _audit(reg, user, "draft_unusable", rid, **res["log"], error=parsed.get("error"))
            raise HTTPException(502, {"detail": f"the model's output could not be used: {parsed.get('error')}", "code": "unusable",
                                      "required": ["retry the draft generation"]})
        sections: dict = {s: [] for s in ("event_summary", "regulatory_and_procedural_context", "precedent_review")}
        for st, r in zip(parsed["statements"], res["results"]):
            sections[st["section"]].append({"text": st["text"], "citations": _cits(r), "verification": _ver(r)})
        none_verified = parsed["statements"] and not any(r["verdict"] == "verified" for r in res["results"])
        notice = ("The AI wrote nothing that could be supported by the sources; the draft contains only what people entered."
                  if not parsed["statements"] else
                  "None of the AI-written statements could be verified against a source." if none_verified else None)
        ev = {**res["ai_event"], "notice": notice}
        out = _wf("POST", f"/internal/deviations/{rid}/draft", {**_who(user), "ai_sections": sections, "ai_event": ev})
        _audit(reg, user, "draft_generated", rid, **res["log"], output={"sections": sections, "gaps": parsed.get("gaps")},
               duration_s=round(time.time() - res["started"], 3))
        return out

    @router.get("/{rid}/draft")
    def get_draft(rid: str, _: User = Depends(get_user)):
        try:
            return _wf("GET", f"/internal/deviations/{rid}/draft")
        except AiTaskError as e:
            raise HTTPException(e.status, e.detail)

    @router.post("/{rid}/draft/statements/{sid}")
    def decide_statement(rid: str, sid: str, body: StatementIn, reg: Registry = Depends(get_registry),
                         user: User = Depends(get_user)):
        return _human(reg, user, rid, "statement_decision", f"/internal/deviations/{rid}/draft/statements/{sid}",
                      body.model_dump(), {"statement": sid, **body.model_dump()})

    # ---- GW-09: explicit human actions only
    @router.post("/{rid}/mark-ready")
    def mark_ready(rid: str, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "mark_ready", f"/internal/deviations/{rid}/mark-ready", {})

    @router.post("/{rid}/cancel")
    def cancel(rid: str, body: CancelIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
        return _human(reg, user, rid, "cancel", f"/internal/deviations/{rid}/cancel", body.model_dump())

    return router


def _bank_enums() -> dict:
    """Event types and classifications come from the form template (knowledge), so the model can only pick real ones."""
    r = allowlist.service_get("knowledge", "/templates")
    t = r.json()[0] if r.status_code == 200 else {}
    enums = t.get("enums") or {}
    return {"event_types": enums.get("event_type") or [], "classification": enums.get("severity") or ["minor", "major", "critical"]}
