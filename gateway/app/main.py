import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import fetcher, sync
from .allowlist import UrlNotAllowed
from .auth import User, get_user
from .registry import Registry, RegistryError

app = FastAPI(title="gateway")
_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def _err(e: RegistryError) -> HTTPException:
    return HTTPException(e.status, str(e))


class SourceIn(BaseModel):
    name: str
    url: str
    jurisdiction: str
    product_types: list[str] = []
    document_types: list[str] = []
    check_frequency: str = "manual"
    legal_weight: str
    auto_accept: bool = False


class DecisionIn(BaseModel):
    note: str = ""


@app.get("/health")
def health() -> dict:
    return {"service": "gateway", "status": "ok"}


@app.get("/sources")
def list_sources(status: str | None = None, reg: Registry = Depends(get_registry), _: User = Depends(get_user)):
    return reg.list_sources(status)


@app.get("/sources/{sid}")
def get_source(sid: int, reg: Registry = Depends(get_registry), _: User = Depends(get_user)):
    try:
        return reg.get_source(sid)
    except RegistryError as e:
        raise _err(e)


@app.post("/sources", status_code=201)
def propose_source(body: SourceIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    try:
        return reg.propose_add(body.model_dump(), user)
    except RegistryError as e:
        raise _err(e)


@app.patch("/sources/{sid}")
def propose_edit(sid: int, patch: dict, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    try:
        return reg.propose_edit(sid, patch, user)
    except RegistryError as e:
        raise _err(e)


@app.delete("/sources/{sid}")
def propose_retire(sid: int, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    try:
        return reg.propose_retire(sid, user)
    except RegistryError as e:
        raise _err(e)


@app.post("/seed-defaults", status_code=201)
def seed_defaults(reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    """Propose the two fixed Phase 1 sources (still need a second person's approval)."""
    out = []
    for s in fetcher.SEED_SOURCES:
        try:
            out.append(reg.propose_add(s, user))
        except RegistryError as e:
            if e.status != 409:
                raise _err(e)
    return out


@app.get("/changes")
def list_changes(state: str | None = None, reg: Registry = Depends(get_registry), _: User = Depends(get_user)):
    return reg.list_changes(state)


@app.post("/changes/{cid}/approve")
def approve(cid: int, body: DecisionIn | None = None, reg: Registry = Depends(get_registry),
            user: User = Depends(get_user)):
    try:
        return reg.decide(cid, True, user, body.note if body else "")
    except RegistryError as e:
        raise _err(e)


@app.post("/changes/{cid}/reject")
def reject(cid: int, body: DecisionIn | None = None, reg: Registry = Depends(get_registry),
           user: User = Depends(get_user)):
    try:
        return reg.decide(cid, False, user, body.note if body else "")
    except RegistryError as e:
        raise _err(e)


@app.post("/sources/{sid}/fetch")
def trigger_fetch(sid: int, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    """Explicit, one-shot fetch. No scheduler exists in Phase 1."""
    if user.role not in ("steward", "qa"):
        raise HTTPException(403, "only steward/qa may trigger a fetch")
    try:
        src = reg.get_source(sid)
    except RegistryError as e:
        raise _err(e)
    if src["status"] != "active":
        raise HTTPException(409, f"source is {src['status']}, not active (needs second approval)")
    try:
        doc = fetcher.fetch_document(src)
        result = fetcher.ingest(src, doc, user.name)
    except UrlNotAllowed as e:
        reg.log(user.name, "source.fetch_refused", f"source:{sid}", {"reason": str(e)})
        raise HTTPException(403, str(e))
    except (fetcher.FetchError, httpx.HTTPError) as e:
        reg.log(user.name, "source.fetch_failed", f"source:{sid}", {"error": str(e)})
        raise HTTPException(502, str(e))
    reg.log(user.name, "source.fetch", f"source:{sid}",
            {"fetched_url": doc.fetched_url, "result": result.get("result"), "item_id": result.get("item_id")})
    return result


@app.post("/sync/eqms-records")
def sync_eqms(reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    """Explicit one-shot: copy executed records from mock-eqms (read-only) into the knowledge index."""
    if user.role not in ("steward", "qa"):
        raise HTTPException(403, "only steward/qa may trigger a sync")
    try:
        result = sync.sync_eqms_records(user.name)
    except UrlNotAllowed as e:
        reg.log(user.name, "sync.eqms_refused", "mock-eqms->knowledge", {"reason": str(e)})
        raise HTTPException(403, str(e))
    except (sync.SyncError, httpx.HTTPError) as e:
        reg.log(user.name, "sync.eqms_failed", "mock-eqms->knowledge", {"error": str(e)})
        raise HTTPException(502, str(e))
    reg.log(user.name, "sync.eqms", "mock-eqms->knowledge", result)
    return result


@app.get("/audit")
def audit_log(reg: Registry = Depends(get_registry), _: User = Depends(get_user)):
    return {"chain_valid": reg.verify_audit(), "entries": reg.audit_entries()}


# ------------------------------------------------------------------ Phase 3: assistant (retrieval + local LLM + citation check)
from . import ai_config, allowlist, assist  # noqa: E402


class AskIn(BaseModel):
    query: str
    model: str | None = None   # must be an approved model; omitted -> the config's default_model


class DispositionIn(BaseModel):
    action: str
    edited_text: str | None = None


@app.post("/assist/ask")
def assist_ask(body: AskIn, reg: Registry = Depends(get_registry), user: User = Depends(get_user)):
    """Explicit, one-shot. Retrieval -> local model -> citation verification. Nothing runs on a schedule."""
    if user.role not in ("steward", "qa"):
        raise HTTPException(403, "only steward/qa may use the assistant")
    try:
        return assist.ask(body.query, user.name, reg, model=body.model)
    except assist.AssistError as e:
        raise HTTPException(e.status, str(e))
    except UrlNotAllowed as e:
        raise HTTPException(403, str(e))


@app.post("/assist/{call_id}/disposition")
def assist_disposition(call_id: str, body: DispositionIn, reg: Registry = Depends(get_registry),
                       user: User = Depends(get_user)):
    """Record what the user did with an AI output: accepted | edited | rejected (DI-03)."""
    if user.role not in ("steward", "qa"):
        raise HTTPException(403, "only steward/qa may record a disposition")
    try:
        return assist.record_disposition(call_id, body.action, user.name, reg, body.edited_text)
    except assist.AssistError as e:
        raise HTTPException(e.status, str(e))


@app.get("/assist/config")
def assist_config(_: User = Depends(get_user)):
    """The approved AI configuration item currently in force (AI-06)."""
    try:
        cfg = ai_config.load_approved()
    except ai_config.ConfigNotApproved as e:
        raise HTTPException(503, str(e))
    out = cfg.identity()
    installed: dict = {}
    try:  # best effort, read-only: which approved models are installed and does the digest match the pin?
        r = allowlist.service_get("ollama", "/api/tags")
        have = {(m.get("name") or "").removesuffix(":latest"): (m.get("digest") or "").lower() for m in r.json().get("models", [])}
        for m in cfg.models:
            d = have.get(m["name"])
            installed[m["name"]] = {"installed": d is not None,
                                    "digest_matches_pin": None if (d is None or not m["digest"]) else d.startswith(m["digest"])}
    except Exception:
        installed = {}
    out["models"] = [{"name": m["name"], "pinned_digest": m["digest"], **installed.get(m["name"], {})} for m in cfg.models]
    return out
