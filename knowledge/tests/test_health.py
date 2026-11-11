"""Health test via raw ASGI (stdlib only): TestClient would need httpx,
which is forbidden outside the gateway (closed-loop rule)."""
import asyncio
import json

from app.main import app

SERVICE = "knowledge"


def _get(path: str):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": [], "server": ("test", 80),
             "client": ("test", 1), "scheme": "http", "root_path": ""}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_health():
    status, body = _get("/health")
    assert status == 200
    assert body == {"service": SERVICE, "status": "ok"}
