"""Raw-ASGI test client (stdlib only: TestClient needs httpx, which is forbidden outside the gateway)."""
import asyncio
import json


def call(app, method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    path, _, query = path.partition("?")
    hdrs = [(b"content-type", b"application/json")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
             "server": ("t", 80), "client": ("t", 1), "scheme": "http", "root_path": ""}
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
