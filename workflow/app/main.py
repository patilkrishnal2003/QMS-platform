"""workflow: PASSIVE service (no network, no HTTP client). The gateway orchestrates retrieval, the LLM call and audit
logging, and calls these endpoints, which only compute and store: Phase 3 prompt/parse/verify, plus the Phase 4
deviation drafting loop (state machine and every human action)."""
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import prompt, tasks, verifier
from .engine import Engine, Store
from .errors import WorkflowError

app = FastAPI(title="workflow")
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        from pathlib import Path
        _engine = Engine(Store(os.environ.get("WORKFLOW_DB", str(Path(__file__).resolve().parent.parent / "data" / "workflow.db"))))
    return _engine


@app.exception_handler(WorkflowError)
async def _wf_error(_request, exc: WorkflowError):
    return JSONResponse(status_code=exc.status, content=exc.as_dict())


@app.get("/health")
def health() -> dict:
    return {"service": "workflow", "status": "ok"}


# ------------------------------------------------------------------ Phase 3: prompt / parse / verify (unchanged)
@app.post("/internal/build-prompt")
def build_prompt(payload: dict):
    try:
        return prompt.build_prompt(payload["query"], payload.get("sources", []), payload["system_prompt"],
                                   int(payload.get("max_sources", 10)), int(payload.get("max_context_chars", 12000)),
                                   payload.get("passage_template", prompt.DEFAULT_PASSAGE_TEMPLATE),
                                   payload.get("instructions", prompt.DEFAULT_INSTRUCTIONS))
    except KeyError as e:
        raise HTTPException(400, f"missing field: {e}")
    except prompt.PromptConfigError as e:
        raise HTTPException(400, str(e))


@app.post("/internal/parse")
def parse(payload: dict):
    return prompt.parse_output(payload.get("raw", ""))


@app.post("/internal/verify")
def verify(payload: dict):
    """sources: {"<source_id>@<version>": source-or-null}. A null/missing source means 'does not exist'."""
    return verifier.verify(payload.get("statements", []), payload.get("sources", {}), payload.get("thresholds"))


# ------------------------------------------------------------------ Phase 4: AI task prompts/parsers (pure)
@app.post("/internal/tasks/proposals/prompt")
def proposals_prompt(p: dict):
    return tasks.build_proposals_prompt(p)


@app.post("/internal/tasks/proposals/parse")
def proposals_parse(p: dict):
    return tasks.parse_proposals(p.get("raw", ""), p["event_types"], p["classes"], p.get("known_requirements", []),
                                 p.get("effective_sops", []))


@app.post("/internal/tasks/gap/prompt")
def gap_prompt(p: dict):
    return tasks.build_gap_prompt(p)


@app.post("/internal/tasks/gap/parse")
def gap_parse(p: dict):
    return tasks.parse_gap(p.get("raw", ""), int(p.get("max_questions", 6)))


@app.post("/internal/tasks/draft/prompt")
def draft_prompt(p: dict):
    return tasks.build_draft_prompt(p)


@app.post("/internal/tasks/draft/parse")
def draft_parse(p: dict):
    return tasks.parse_draft(p.get("raw", ""))


# ------------------------------------------------------------------ Phase 4: the deviation record + its state machine
# `who` = {"actor": "<user>", "role": "<role>"} is supplied by the gateway, which authenticates the (simulated) user.
def _who(p: dict) -> tuple[str, str]:
    return p["actor"], p["role"]


@app.post("/internal/deviations")
def create(p: dict):
    a, r = _who(p)
    return get_engine().create(a, r, p.get("description", ""), p.get("fields", {}))


@app.get("/internal/deviations")
def list_deviations():
    return get_engine().store.list_records()


@app.get("/internal/deviations/{rid}")
def get_deviation(rid: str):
    return get_engine().get(rid)


@app.get("/internal/deviations/{rid}/checklist")
def get_checklist(rid: str):
    return get_engine().checklist_view(rid)


@app.get("/internal/deviations/{rid}/draft")
def get_draft(rid: str):
    return get_engine().draft_view(rid)


@app.get("/internal/deviations/{rid}/events")
def get_events(rid: str):
    return get_engine().store.events(rid)


@app.get("/internal/deviations/{rid}/sources")
def get_sources(rid: str):
    return get_engine().sources_for(rid)


@app.get("/internal/deviations/{rid}/sources/{key}")
def get_source(rid: str, key: str):
    return get_engine().source(rid, key)


@app.post("/internal/deviations/{rid}/proposals")
def store_proposals(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().store_proposals(rid, a, r, p["items"], p["ai_event"])


@app.post("/internal/deviations/{rid}/proposals/{item}/decide")
def decide_proposal(rid: str, item: str, p: dict):
    a, r = _who(p)
    return get_engine().decide_proposal(rid, a, r, item, p.get("action", ""), p.get("value"), p.get("rationale", ""),
                                        bool(p.get("ack_flag", False)))


@app.post("/internal/deviations/{rid}/advance")
def advance(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().advance(rid, a, r, p.get("to", ""))


@app.post("/internal/deviations/{rid}/gap-questions")
def store_gap(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().store_gap_questions(rid, a, r, p["items"], p["ai_event"])


@app.post("/internal/deviations/{rid}/gap-questions/waive")
def waive_gap(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().waive_gap(rid, a, r, p.get("justification", ""))


@app.post("/internal/deviations/{rid}/gap-questions/{qid}/dismiss")
def dismiss_gap(rid: str, qid: str, p: dict):
    a, r = _who(p)
    return get_engine().dismiss_gap_question(rid, a, r, qid, p.get("justification", ""))


@app.post("/internal/deviations/{rid}/answers/{key}")
def answer(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().answer(rid, a, r, key, p.get("kind", "answer"), p.get("value", ""), p.get("note", ""))


@app.post("/internal/deviations/{rid}/assign/{key}")
def assign(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().assign(rid, a, r, key, p.get("to_user", ""), p.get("to_role", ""))


@app.post("/internal/deviations/{rid}/evidence/{key}")
def evidence(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().attach_evidence(rid, a, r, key, p.get("name", ""), p.get("description", ""), p.get("ref", ""))


@app.post("/internal/deviations/{rid}/not-applicable/{key}")
def not_applicable(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().mark_not_applicable(rid, a, r, key, p.get("justification", ""))


@app.post("/internal/deviations/{rid}/decisions/{key}")
def decision(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().decide(rid, a, r, key, p.get("value", ""), p.get("rationale", ""), bool(p.get("tbd", False)))


@app.post("/internal/deviations/{rid}/tbd-accept/{key}")
def tbd_accept(rid: str, key: str, p: dict):
    a, r = _who(p)
    return get_engine().accept_tbd(rid, a, r, key, p.get("justification", ""))


@app.post("/internal/deviations/{rid}/draft")
def store_draft(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().store_draft(rid, a, r, p["ai_sections"], p["ai_event"])


@app.post("/internal/deviations/{rid}/draft/statements/{sid}")
def decide_statement(rid: str, sid: str, p: dict):
    a, r = _who(p)
    return get_engine().decide_statement(rid, a, r, sid, p.get("action", ""), p.get("text", ""), bool(p.get("ack_flag", False)))


@app.post("/internal/deviations/{rid}/mark-ready")
def mark_ready(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().mark_ready(rid, a, r)


@app.post("/internal/deviations/{rid}/cancel")
def cancel(rid: str, p: dict):
    a, r = _who(p)
    return get_engine().cancel(rid, a, r, p.get("reason", ""))
