"""Phase 2 end to end with REAL services over localhost (no internet needed):
mock-eqms -> gateway sync -> knowledge. Each service runs from its own venv as a subprocess."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(service: str, env: dict):
    py = ROOT / service / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / service / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip(f"{service} venv not created")
    import httpx
    port = _port()
    proc = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT / service), env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc, f"http://127.0.0.1:{port}"
        except httpx.HTTPError:
            time.sleep(0.25)
    proc.kill()
    pytest.fail(f"{service} did not start")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    eq_proc, eq = _spawn("mock-eqms", {"EQMS_DB": str(tmp_path / "e.db")})
    kn_proc, kn = _spawn("knowledge", {"KNOWLEDGE_DB": str(tmp_path / "k.db")})
    monkeypatch.setenv("MOCK_EQMS_URL", eq)
    monkeypatch.setenv("KNOWLEDGE_URL", kn)
    yield eq, kn
    for p in (eq_proc, kn_proc):
        p.terminate()
        p.wait(10)


def test_sync_moves_executed_records_from_eqms_into_knowledge(stack, tmp_path):
    import httpx
    from fastapi.testclient import TestClient

    from app.main import app, get_registry
    from app.registry import Registry

    eq, kn = stack
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    try:
        c = TestClient(app)
        assert httpx.get(f"{kn}/records").json() == []          # nothing indexed before the sync
        assert c.post("/sync/eqms-records", headers={"X-User": "viewer-1"}).status_code == 403
        r = c.post("/sync/eqms-records", headers={"X-User": "steward-a"})
        assert r.status_code == 200, r.text
        assert r.json() == {"read": 20, "indexed": 20, "source": "mock-eqms"}

        recs = httpx.get(f"{kn}/records").json()
        assert len(recs) == 20 and {x["kind"] for x in recs} == {"executed_record"}
        assert all(x["record_kind"] == "record" for x in recs)     # never a blank template
        assert all(x["citation"] == f"{x['id']} v{x['version']}" for x in recs)   # IK-05: cited by ID + version

        hits = httpx.get(f"{kn}/search", params={"q": "metal detector sensitivity"}).json()
        assert hits[0]["kind"] == "executed_record" and hits[0]["id"].startswith("SYN-DEV-")
        assert hits[0]["version"] == "1.0" and hits[0]["citation"].endswith(" v1.0")
        sop = httpx.get(f"{kn}/search", params={"q": "classification critical major minor", "kind": "sop"}).json()
        assert sop[0]["sop_number"] == "SOP-DEV-002" and sop[0]["effective"] is True
        assert [t["kind"] for t in httpx.get(f"{kn}/templates").json()] == ["template"]

        # a confirmed human write to the eQMS shows up only after the next explicit sync
        draft = {"event_type": "process_deviation", "site": "SITE-RB", "product": "Examplamol 500 mg tablets",
                 "lot_number": "EXA-26-001", "equipment_id": "TAB-PRESS-02", "event_date": "2026-11-01",
                 "detected_date": "2026-11-01", "description": "Tablet weight trending high.",
                 "immediate_action": "Press stopped.", "severity": "minor", "reported_by": "U. Tester"}
        assert httpx.post(f"{eq}/write/records", json={"record": draft}).status_code == 409
        ok = httpx.post(f"{eq}/write/records", json={"record": draft, "human_confirmed": True, "confirmed_by": "qa-1"})
        assert ok.status_code == 201 and ok.json()["status"] == "draft"
        assert len(httpx.get(f"{kn}/records").json()) == 20
        assert c.post("/sync/eqms-records", headers={"X-User": "qa-1"}).json()["indexed"] == 21
        assert len(httpx.get(f"{kn}/records", params={"status": "draft"}).json()) == 1
        assert reg.verify_audit() and [e["action"] for e in reg.audit_entries()].count("sync.eqms") == 2
    finally:
        app.dependency_overrides.clear()
