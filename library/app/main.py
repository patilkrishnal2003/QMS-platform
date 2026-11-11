from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .store import LibraryError, Store

app = FastAPI(title="library")
_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _err(e: LibraryError) -> HTTPException:
    return HTTPException(e.status, str(e))


class Decision(BaseModel):
    note: str = ""


@app.get("/health")
def health() -> dict:
    return {"service": "library", "status": "ok"}


@app.post("/internal/ingest")
def ingest(payload: dict, store: Store = Depends(get_store)):
    """Called only by the gateway (the library itself never makes outbound calls)."""
    try:
        return store.ingest(payload)
    except LibraryError as e:
        raise _err(e)


@app.get("/queue")
def list_queue(state: str = "pending", store: Store = Depends(get_store)):
    return store.queue(state)


@app.get("/queue/{item_id}")
def queue_item(item_id: int, store: Store = Depends(get_store)):
    try:
        return store.item(item_id)
    except LibraryError as e:
        raise _err(e)


@app.get("/queue/{item_id}/redline")
def view_redline(item_id: int, store: Store = Depends(get_store)):
    try:
        return store.redline_for(item_id)
    except LibraryError as e:
        raise _err(e)


@app.post("/queue/{item_id}/accept")
def accept(item_id: int, body: Decision | None = None, x_user: str = Header(default=""),
           store: Store = Depends(get_store)):
    try:
        return store.accept(item_id, x_user, body.note if body else "")
    except LibraryError as e:
        raise _err(e)


@app.post("/queue/{item_id}/reject")
def reject(item_id: int, body: Decision | None = None, x_user: str = Header(default=""),
           store: Store = Depends(get_store)):
    try:
        return store.reject(item_id, x_user, body.note if body else "")
    except LibraryError as e:
        raise _err(e)


@app.get("/library")
def library_state(include_text: bool = False, store: Store = Depends(get_store)):
    """Current accepted version of every source."""
    return store.library(include_text)


@app.get("/search")
def search(q: str, include_text: bool = False, limit: int = 10, store: Store = Depends(get_store)):
    """Keyword search over current accepted library items."""
    try:
        return store.search(q, include_text, limit)
    except LibraryError as e:
        raise _err(e)


@app.get("/library/version")
def library_version(store: Store = Depends(get_store)):
    return store.library_version()


@app.get("/library/versions")
def versions(source_url: str, store: Store = Depends(get_store)):
    return store.versions(source_url)


@app.get("/library/{item_id}")
def get_item(item_id: int, store: Store = Depends(get_store)):
    """One accepted item with its full text (what a citation links to)."""
    try:
        return store.accepted_item(item_id)
    except LibraryError as e:
        raise _err(e)
