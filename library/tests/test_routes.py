"""Route-level tests using raw ASGI (library has no HTTP client library, so no TestClient)."""
import asyncio
import json

import pytest

from app.main import app, get_store
from app.store import Store


def call(method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    path, _, query = path.partition("?")
    hdrs = [(b"content-type", b"application/json")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
             "server": ("t", 80), "client": ("t", 1), "scheme": "http"}
    out = {"body": b"", "status": None}

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
            elif m["type"] == "http.response.body":
                out["body"] += m.get("body", b"")

        await app(scope, receive, send)

    asyncio.run(run())
    return out["status"], (json.loads(out["body"]) if out["body"] else None)


@pytest.fixture(autouse=True)
def _store(tmp_path):
    s = Store(str(tmp_path / "r.db"))
    app.dependency_overrides[get_store] = lambda: s
    yield s
    app.dependency_overrides.clear()


P = {"source_url": "https://example.test/x", "title": "X", "text": "a\nb\n", "retrieved_at": "2026-01-01T00:00:00Z",
     "document_date": "2026-01-01", "version_or_docket": "X-1", "status": "final", "legal_weight": "guidance"}


def test_ingest_queue_redline_accept_flow():
    st, r = call("POST", "/internal/ingest", P)
    assert st == 200 and r["result"] == "queued_new"
    iid = r["item_id"]
    assert call("GET", "/queue")[1][0]["id"] == iid
    st, rl = call("GET", f"/queue/{iid}/redline")
    assert st == 200 and rl["added"] == 2
    assert call("POST", f"/queue/{iid}/accept", {"note": "ok"})[0] == 403  # no X-User
    st, out = call("POST", f"/queue/{iid}/accept", {"note": "ok"}, {"X-User": "steward-a"})
    assert st == 200 and out["is_current"] is True
    st, lib = call("GET", "/library")
    assert st == 200 and lib[0]["id"] == iid and "text" not in lib[0]
    assert "text" in call("GET", "/library?include_text=true")[1][0]
    st, r2 = call("POST", "/internal/ingest", {**P, "text": "a\nb\nc\n"})
    assert r2["result"] == "queued_changed"
    assert call("POST", f"/queue/{r2['item_id']}/reject", {"note": "no"}, {"X-User": "qa-1"})[1]["queue_state"] == "rejected"
    assert len(call("GET", "/library/versions?source_url=https://example.test/x")[1]) == 2


def test_errors():
    assert call("POST", "/internal/ingest", {**P, "status": "nope"})[0] == 400
    assert call("GET", "/queue/999/redline")[0] == 404


def test_phase3_routes_search_version_and_item(_store):
    st, r = call("POST", "/internal/ingest", {**P, "text": "The quality control unit approves components.\nline 2\n"})
    iid = r["item_id"]
    assert call("GET", "/library/version")[1]["library_version"] == "lib-0-empty"
    assert call("GET", f"/library/{iid}")[0] == 404                                # pending: not citable
    call("POST", f"/queue/{iid}/accept", {}, {"X-User": "steward-a"})
    st, item = call("GET", f"/library/{iid}")
    assert st == 200 and item["text"].startswith("The quality control unit") and item["version_no"] == 1
    st, hits = call("GET", "/search?q=quality+control&include_text=true")
    assert st == 200 and hits[0]["id"] == iid and "text" in hits[0]
    assert call("GET", "/search?q=%20")[0] == 400
    assert call("GET", "/library/version")[1]["library_version"].startswith("lib-1-")
    assert call("GET", "/library/versions?source_url=https://example.test/x")[0] == 200   # static route still wins
