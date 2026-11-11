"""Gateway internal-service routing + eqms->knowledge sync (network faked)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import allowlist, sync
from app.allowlist import UrlNotAllowed, check_internal_url
from app.main import app, get_registry
from app.registry import Registry


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data, text

    def json(self):
        return self._d


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for v in ("LIBRARY_URL", "KNOWLEDGE_URL", "MOCK_EQMS_URL", "GATEWAY_ALLOWLIST"):
        monkeypatch.delenv(v, raising=False)


# ---- internal routing table
def test_allowed_internal_routes():
    check_internal_url("http://127.0.0.1:8001/internal/ingest", "library", "POST")
    check_internal_url("http://127.0.0.1:8002/internal/index-records", "knowledge", "POST")
    check_internal_url("http://127.0.0.1:8003/read/records", "mock-eqms", "GET")
    check_internal_url("http://127.0.0.1:8003/read/records/SYN-DEV-2024-001")  # service/method not pinned
    # Phase 3 read routes + workflow + local LLM
    check_internal_url("http://127.0.0.1:8001/search?q=x", "library", "GET")
    check_internal_url("http://127.0.0.1:8001/library/12", "library", "GET")
    check_internal_url("http://127.0.0.1:8002/search?q=x", "knowledge", "GET")
    check_internal_url("http://127.0.0.1:8002/documents/SOP-DEV-001", "knowledge", "GET")
    check_internal_url("http://127.0.0.1:8004/internal/verify", "workflow", "POST")
    check_internal_url("http://127.0.0.1:11434/api/chat", "ollama", "POST")
    check_internal_url("http://127.0.0.1:11434/api/tags", "ollama", "GET")


@pytest.mark.parametrize("url,service,method", [
    ("http://127.0.0.1:8003/write/records", "mock-eqms", "POST"),         # write side of the eQMS is never reachable
    ("http://127.0.0.1:8003/read/records", "mock-eqms", "POST"),          # read-only means GET only
    ("http://127.0.0.1:8002/internal/index-records", "knowledge", "GET"),
    ("http://127.0.0.1:8002/openapi.json", "knowledge", "GET"),           # not a listed read route
    ("http://127.0.0.1:8002/search?q=x", "knowledge", "POST"),            # read routes are GET only
    ("http://127.0.0.1:8001/libraryfoo", "library", "GET"),               # prefix must end at a boundary
    ("http://127.0.0.1:11434/api/pull", "ollama", "POST"),                # only /api/chat and /api/tags
    ("http://127.0.0.1:11434/api/chat", "ollama", "GET"),
    ("http://127.0.0.1:8004/health", "workflow", "GET"),                  # workflow: POST /internal/ only
    ("http://127.0.0.1:8001/queue/1/accept", "library", "POST"),
    ("http://127.0.0.1:8003/read/../write/records", "mock-eqms", "GET"),
    ("http://127.0.0.1:9999/internal/x", "knowledge", "POST"),            # wrong port
    ("http://evil.example.com/internal/x", None, None),
])
def test_disallowed_internal_routes(url, service, method):
    with pytest.raises(UrlNotAllowed):
        check_internal_url(url, service, method)


def test_no_request_is_made_for_a_disallowed_internal_route(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(UrlNotAllowed):
        allowlist.service_post("mock-eqms", "/write/records", {})
    with pytest.raises(UrlNotAllowed):
        allowlist.service_get("knowledge", "/internal/index-records")  # POST-only route
    with pytest.raises(UrlNotAllowed):
        allowlist.service_get("nonexistent", "/x")


def test_localhost_is_still_not_on_the_external_allowlist():
    assert "127.0.0.1" not in allowlist.allowed_hosts()
    with pytest.raises(UrlNotAllowed):
        allowlist.check_url("http://127.0.0.1:8003/read/records")


# ---- sync function
def test_sync_copies_records_to_knowledge_and_drops_templates():
    sent = {}

    def get(service, path, params=None):
        assert (service, path) == ("mock-eqms", "/read/records")
        return Resp(data=[{"id": "R1", "record_kind": "record"}, {"id": "T1", "record_kind": "template"}])

    def post(service, path, json):
        sent.update(service=service, path=path, **json)
        return Resp(data={"indexed": len(json["records"]), "source": "mock-eqms"})

    out = sync.sync_eqms_records("steward-a", get, post)
    assert out == {"read": 1, "indexed": 1, "source": "mock-eqms"}
    assert [r["id"] for r in sent["records"]] == ["R1"] and sent["service"] == "knowledge"
    assert sent["path"] == "/internal/index-records" and sent["synced_by"] == "steward-a"


def test_sync_reports_upstream_failures():
    with pytest.raises(sync.SyncError):
        sync.sync_eqms_records("a", lambda *a, **k: Resp(500), lambda *a, **k: Resp())
    with pytest.raises(sync.SyncError):
        sync.sync_eqms_records("a", lambda *a, **k: Resp(data=[]), lambda *a, **k: Resp(400, text="nope"))


# ---- route
@pytest.fixture
def client(tmp_path):
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    yield TestClient(app), reg
    app.dependency_overrides.clear()


def test_sync_route_roles_audit_and_failure(client, monkeypatch):
    c, reg = client
    monkeypatch.setattr(allowlist, "service_get", lambda *a, **k: Resp(data=[{"id": "R1", "record_kind": "record"}]))
    monkeypatch.setattr(allowlist, "service_post", lambda *a, **k: Resp(data={"indexed": 1, "source": "mock-eqms"}))
    assert c.post("/sync/eqms-records").status_code == 401
    assert c.post("/sync/eqms-records", headers={"X-User": "viewer-1"}).status_code == 403
    r = c.post("/sync/eqms-records", headers={"X-User": "steward-a"})
    assert r.status_code == 200 and r.json()["indexed"] == 1
    assert reg.audit_entries()[-1]["action"] == "sync.eqms"
    monkeypatch.setattr(allowlist, "service_get", lambda *a, **k: Resp(503))
    assert c.post("/sync/eqms-records", headers={"X-User": "qa-1"}).status_code == 502
    assert reg.audit_entries()[-1]["action"] == "sync.eqms_failed" and reg.verify_audit()


def test_ollama_is_internal_only_never_on_the_external_allowlist():
    assert "127.0.0.1" not in allowlist.allowed_hosts() and "localhost" not in allowlist.allowed_hosts()
    for url in ("http://127.0.0.1:11434/api/chat", "http://localhost:11434/api/tags"):
        with pytest.raises(UrlNotAllowed):
            allowlist.check_url(url)          # the external path refuses it...
        with pytest.raises(UrlNotAllowed):
            allowlist.safe_get(url)           # ...before any request
    assert allowlist.INTERNAL["ollama"][1] == "http://127.0.0.1:11434"
