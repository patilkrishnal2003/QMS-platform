"""Orchestrates one assistant call: retrieve -> build prompt (workflow) -> local LLM (gateway only) ->
parse + verify citations (workflow) -> log (DI-03).

URS: AI-01 every statement cites document+version+section with a link; AI-02 when no support is found the
system says so and never invents a citation; AI-06 model/prompt/retrieval/generation settings are the approved
config; DI-03 every call is logged (input, retrieved source refs, model + config version, library version,
output, user, time) and the user's later action (accepted / edited / rejected) is appended."""
import time
import uuid

from . import allowlist, retrieval
from .ai_config import AiConfig, ConfigNotApproved, ModelNotApproved, load_approved
from .llm import DigestMismatch, LlmError, ModelNotInstalled, OllamaClient
from .registry import Registry
from .sources import SourceFetchError, fetch_source

NO_SUPPORT_NOTICE = ("No supporting source was found for this question in the library or the controlled "
                     "documents. Nothing has been drafted from outside knowledge.")
DISPOSITIONS = {"accepted", "edited", "rejected"}


class AssistError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _wf(path: str, payload: dict, post) -> dict:
    r = post("workflow", path, payload)
    if r.status_code != 200:
        raise AssistError(f"workflow {path} failed: HTTP {r.status_code} {str(r.text)[:200]}", 502)
    return r.json()


def ask(query: str, user: str, reg: Registry, get=None, post=None, client: OllamaClient | None = None,
        cfg: AiConfig | None = None, model: str | None = None) -> dict:
    get = get or allowlist.service_get
    post = post or allowlist.service_post
    call_id = uuid.uuid4().hex
    subject = f"ai:{call_id}"
    started = time.time()
    log: dict = {"call_id": call_id, "user": user, "input": {"query": query}}

    def fail(action: str, msg: str, status: int):
        reg.log(user, action, subject, {**log, "error": msg})
        raise AssistError(msg, status)

    if not (query or "").strip():
        raise AssistError("query is empty", 400)
    try:
        cfg = cfg or load_approved()
    except ConfigNotApproved as e:
        fail("ai.call_refused", str(e), 503)
    log["config"] = cfg.identity()
    try:
        model_name, chosen_by = cfg.resolve_model(model)
    except ModelNotApproved as e:
        log["model_selection"] = {"requested": model, "used": None}
        fail("ai.call_refused", str(e), 400)
    log["model_selection"] = {"requested": model, "used": model_name, "chosen_by": chosen_by}
    log["settings"] = {"retrieval": cfg.retrieval, "generation": cfg.generation_for(model_name),
                       "verifier": cfg.verifier, "system_prompt": cfg.system_prompt, "prompt": cfg.prompt}
    client = client or OllamaClient(cfg, get=get, post=post, model=model_name)

    try:
        log["library_version"] = retrieval.library_version(get)
        log["knowledge_snapshot"] = retrieval.knowledge_snapshot(get)
        found = retrieval.retrieve(query, cfg.retrieval, get)
    except retrieval.RetrievalError as e:
        fail("ai.call_failed", f"retrieval failed: {e}", 502)
    srcs = found["sources"]
    log["retrieval_notes"] = found["notes"]

    # ---- nothing retrieved: say so, do NOT call the model (AI-02)
    if not srcs:
        out = {"call_id": call_id, "status": "no_support_found", "no_support_found": True, "notice": NO_SUPPORT_NOTICE,
               "statements": [], "gaps": [], "summary": {"total": 0, "verified": 0, "flagged": 0},
               "retrieved_source_ids": [], "model": None, "model_chosen_by": chosen_by, "config": cfg.identity(),
               "library_version": log["library_version"], "knowledge_snapshot": log["knowledge_snapshot"]}
        reg.log(user, "ai.call", subject, {**log, "retrieved_source_ids": [], "model_called": False,
                                            "output": out, "duration_s": round(time.time() - started, 3)})
        return out

    # ---- prompt (pure, in workflow)
    prompt = _wf("/internal/build-prompt", {
        "query": query, "sources": srcs, "system_prompt": cfg.system_prompt,
        "max_sources": cfg.retrieval["max_sources"], "max_context_chars": cfg.retrieval["max_context_chars"],
        "passage_template": cfg.prompt["passage_template"], "instructions": cfg.prompt["instructions"]}, post)
    retrieved_ids = prompt["context_source_ids"]
    log["retrieved_source_ids"] = retrieved_ids
    log["prompt_messages"] = prompt["messages"]

    # ---- local model (only the gateway can reach it)
    try:
        info = client.model_info()
        log["model"] = info
        gen = client.chat(prompt["messages"], prompt["schema"])
    except (ModelNotInstalled, DigestMismatch) as e:
        fail("ai.call_failed", str(e), 503)
    except LlmError as e:
        fail("ai.call_failed", str(e), 502)
    log["model_call"] = {k: gen.get(k) for k in ("model", "eval_count", "prompt_eval_count", "total_duration",
                                                 "load_duration", "eval_duration", "prompt_eval_duration")}
    log["raw_output"] = gen["content"]

    # ---- parse, then fetch every CITED source from the real stores and verify
    parsed = _wf("/internal/parse", {"raw": gen["content"]}, post)
    if not parsed["ok"]:
        out = {"call_id": call_id, "status": "model_output_unparseable", "no_support_found": False,
               "notice": f"The model's output could not be used ({parsed['error']}). Nothing is presented as supported.",
               "statements": [], "gaps": [], "summary": {"total": 0, "verified": 0, "flagged": 0},
               "retrieved_source_ids": retrieved_ids, "model": log["model"], "model_chosen_by": chosen_by, "config": cfg.identity(),
               "library_version": log["library_version"], "knowledge_snapshot": log["knowledge_snapshot"]}
        reg.log(user, "ai.call", subject, {**log, "model_called": True, "output": out,
                                            "duration_s": round(time.time() - started, 3)})
        return out
    sources_map: dict = {}
    try:
        for c in parsed["cited"]:
            key = c["source_id"] + ("@" + c["version"].lstrip("vV") if c["version"] and not c["source_id"].startswith("req:") else "")
            sources_map[key] = fetch_source(c["source_id"], c["version"], get)
    except SourceFetchError as e:
        fail("ai.call_failed", f"could not check cited sources: {e}", 502)
    ver = _wf("/internal/verify", {"statements": parsed["statements"], "sources": sources_map,
                                   "thresholds": cfg.verifier}, post)
    results, summary = ver["results"], ver["summary"]
    for r in results:
        r["usable"] = r["verdict"] == "verified"
    none_verified = summary["verified"] == 0
    no_support = parsed["no_support_found"] or none_verified
    notice = NO_SUPPORT_NOTICE if (no_support and not results) else (
        "None of the model's statements could be verified against a source. Treat all of it as unsupported."
        if none_verified else
        f"{summary['flagged']} of {summary['total']} statements are flagged and must be checked by a person."
        if summary["flagged"] else "All statements passed the automatic citation check. A person still confirms.")
    out = {"call_id": call_id, "status": "ok", "no_support_found": no_support, "notice": notice,
           "statements": results, "gaps": parsed["gaps"], "summary": summary, "retrieved_source_ids": retrieved_ids,
           "model": log["model"], "model_chosen_by": chosen_by, "config": cfg.identity(), "library_version": log["library_version"],
           "knowledge_snapshot": log["knowledge_snapshot"]}
    reg.log(user, "ai.call", subject, {**log, "model_called": True, "parsed": parsed, "cited_sources_resolved":
                                       {k: (v is not None) for k, v in sources_map.items()}, "output": out,
                                       "duration_s": round(time.time() - started, 3)})
    return out


def record_disposition(call_id: str, action: str, user: str, reg: Registry, edited_text: str | None = None) -> dict:
    """DI-03: what the user did with an AI output. Appended to the audit log; the original call is untouched."""
    if action not in DISPOSITIONS:
        raise AssistError(f"action must be one of {sorted(DISPOSITIONS)}", 400)
    if action == "edited" and not (edited_text or "").strip():
        raise AssistError("edited_text is required when action is 'edited'", 400)
    calls = reg.audit_find(f"ai:{call_id}", "ai.call")
    if not calls:
        raise AssistError(f"no AI call {call_id}", 404)
    import json

    out = json.loads(calls[0]["detail"]).get("output", {})
    flagged = out.get("summary", {}).get("flagged", 0)
    detail = {"call_id": call_id, "action": action, "edited_text": edited_text if action == "edited" else None,
              "statements_flagged_at_decision": flagged}
    reg.log(user, "ai.disposition", f"ai:{call_id}", detail)
    return {**detail, "user": user, "note": "recorded" + (f"; {flagged} statement(s) were still flagged" if flagged and action != "rejected" else "")}
