from fastapi import Depends, FastAPI, HTTPException

from .index import IndexError_, KnowledgeIndex

app = FastAPI(title="knowledge")
_index: KnowledgeIndex | None = None


def get_index() -> KnowledgeIndex:
    global _index
    if _index is None:
        _index = KnowledgeIndex()
    return _index


def _err(e: IndexError_) -> HTTPException:
    return HTTPException(e.status, str(e))


@app.get("/health")
def health() -> dict:
    return {"service": "knowledge", "status": "ok"}


@app.get("/documents")
def list_documents(include_all: bool = False, idx: KnowledgeIndex = Depends(get_index)):
    """Effective SOP versions only, unless include_all=true (IK-01)."""
    return idx.list_documents(include_all)


@app.get("/documents/{sop_number}")
def get_document(sop_number: str, version: str | None = None, idx: KnowledgeIndex = Depends(get_index)):
    try:
        return idx.get_document(sop_number, version)
    except IndexError_ as e:
        raise _err(e)


@app.get("/documents/{sop_number}/versions")
def versions(sop_number: str, idx: KnowledgeIndex = Depends(get_index)):
    try:
        return idx.versions(sop_number)
    except IndexError_ as e:
        raise _err(e)


@app.get("/templates")
def templates(idx: KnowledgeIndex = Depends(get_index)):
    return idx.templates()


@app.get("/records")
def records(status: str | None = None, event_type: str | None = None, idx: KnowledgeIndex = Depends(get_index)):
    """Executed records indexed from the eQMS (via the gateway). Never templates."""
    return idx.list_records(status, event_type)


@app.get("/records/{record_id}")
def record_by_id(record_id: str, idx: KnowledgeIndex = Depends(get_index)):
    try:
        return idx.get_record(record_id)
    except IndexError_ as e:
        raise _err(e)


@app.get("/search")
def search(q: str, kind: str | None = None, include_non_effective: bool = False, limit: int = 10,
           include_text: bool = False, idx: KnowledgeIndex = Depends(get_index)):
    try:
        return idx.search(q, kind, include_non_effective, limit, include_text)
    except IndexError_ as e:
        raise _err(e)


@app.get("/traceability")
def traceability(idx: KnowledgeIndex = Depends(get_index)):
    return idx.traceability()


@app.get("/traceability/{requirement_id}")
def traceability_one(requirement_id: str, idx: KnowledgeIndex = Depends(get_index)):
    try:
        return idx.traceability(requirement_id)[0]
    except IndexError_ as e:
        raise _err(e)


@app.post("/internal/index-records")
def index_records(payload: dict, idx: KnowledgeIndex = Depends(get_index)):
    """Called only by the gateway (knowledge makes no outbound calls)."""
    try:
        return idx.index_records(payload.get("records", []), payload.get("source", "mock-eqms"))
    except IndexError_ as e:
        raise _err(e)
