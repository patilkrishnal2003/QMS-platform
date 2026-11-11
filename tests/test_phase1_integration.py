"""Phase 1 end-to-end + repo-wide guards. Runs with the gateway venv (httpx available).

The library runs as a real uvicorn subprocess from its own venv, so gateway and library
never share a Python process (their `app` packages would collide)."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))


# ---------------------------------------------------------------- ISO guard
def test_no_iso_or_licensed_standard_content():
    pat = re.compile(r"iso\.org|ISO[ /-]?(?:IEC )?\d{3,5}|\bIEC \d{4,5}\b|ASTM [A-Z]?\d{2,5}|ANSI/")
    bad = []
    skip = {".venv", "node_modules", "__pycache__", ".pytest_cache", "data", ".git", ".run", "dist"}
    for p in ROOT.rglob("*"):
        if (not p.is_file() or set(p.parts) & skip or p.suffix not in {".py", ".json", ".md", ".txt", ".ps1", ".js", ".jsx", ".html", ".yaml", ".yml"}
                or p.name in {"test_phase1_integration.py", "CLAUDE.md"}):
            continue
        m = pat.search(p.read_text(encoding="utf-8", errors="ignore"))
        if m:
            bad.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, bad


def test_allowlist_has_no_standards_publisher():
    from app import allowlist
    assert allowlist.ALLOWED_HOSTS == ["www.ecfr.gov", "www.federalregister.gov"]


# ---------------------------------------------------------------- real network
def _online() -> bool:
    try:
        socket.create_connection(("www.ecfr.gov", 443), timeout=5).close()
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_library(tmp_path, monkeypatch):
    py = ROOT / "library" / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / "library" / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("library venv not created")
    port = _free_port()
    env = {**os.environ, "LIBRARY_DB": str(tmp_path / "lib.db")}
    proc = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT / "library"), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("library did not start")
    monkeypatch.setenv("LIBRARY_URL", f"http://127.0.0.1:{port}")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(10)


@pytest.mark.network
def test_real_fetch_of_21_cfr_211_22_through_gateway_into_library(live_library, tmp_path):
    if not _online():
        pytest.skip("offline: www.ecfr.gov unreachable")
    import httpx
    from fastapi.testclient import TestClient

    from app.main import app, get_registry
    from app.registry import Registry

    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    try:
        c = TestClient(app)
        a, b = {"X-User": "steward-a"}, {"X-User": "steward-b"}
        src = c.post("/sources", headers=a, json={
            "name": "21 CFR 211.22", "jurisdiction": "US-FDA", "product_types": ["drug"],
            "document_types": ["regulation"], "check_frequency": "manual", "legal_weight": "regulation",
            "url": "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-21.xml?part=211&section=211.22"}).json()
        assert c.post(f"/sources/{src['id']}/fetch", headers=b).status_code == 409  # not yet approved
        assert c.post(f"/changes/{src['pending_change']['id']}/approve", headers=b).status_code == 200

        r = c.post(f"/sources/{src['id']}/fetch", headers=b)
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "queued_new"

        q = httpx.get(f"{live_library}/queue").json()
        assert len(q) == 1
        item = httpx.get(f"{live_library}/queue/{q[0]['id']}").json()
        assert "quality control unit" in item["text"].lower()
        assert item["title"] == "21 CFR 211.22" and item["legal_weight"] == "regulation"
        assert item["content_kind"] == "full_text"  # contrast with the metadata-only FR item
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["document_date"]) and item["retrieved_at"]
        import hashlib
        assert item["checksum"] == hashlib.sha256(item["text"].encode()).hexdigest()

        # accept it, then a second explicit fetch of unchanged text is a no-op
        assert httpx.post(f"{live_library}/queue/{item['id']}/accept", headers=a, json={}).status_code == 200
        cur = httpx.get(f"{live_library}/library").json()
        assert [i["id"] for i in cur] == [item["id"]] and cur[0]["is_current"]
        again = c.post(f"/sources/{src['id']}/fetch", headers=b).json()
        assert again["result"] == "unchanged"
        assert reg.verify_audit() and [e["action"] for e in reg.audit_entries()].count("source.fetch") == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.network
def test_real_fetch_of_federal_register_2018_26957_through_gateway_into_library(live_library, tmp_path):
    try:
        socket.create_connection(("www.federalregister.gov", 443), timeout=5).close()
    except OSError:
        pytest.skip("offline: www.federalregister.gov unreachable")
    import hashlib

    import httpx
    from fastapi.testclient import TestClient

    from app.fetcher import SEED_SOURCES
    from app.main import app, get_registry
    from app.registry import Registry

    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    try:
        c = TestClient(app)
        a, b = {"X-User": "steward-a"}, {"X-User": "steward-b"}
        body = next(s for s in SEED_SOURCES if "2018-26957" in s["url"])
        src = c.post("/sources", headers=a, json=body).json()
        assert c.post(f"/changes/{src['pending_change']['id']}/approve", headers=b).status_code == 200

        r = c.post(f"/sources/{src['id']}/fetch", headers=b)
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "queued_new"

        item = httpx.get(f"{live_library}/queue/{r.json()['item_id']}").json()
        print("\nSTORED:", item["title"], "|", item["document_date"], "|", item["checksum"],
              "|", item["version_or_docket"], "|", item["content_kind"], "|", len(item["text"]), "chars")
        # Metadata-only by design: federalregister.gov full-text pages are bot-protected.
        assert item["content_kind"] == "metadata_only"
        assert item["title"].startswith("Data Integrity and Compliance With Drug CGMP")
        assert item["document_date"] == "2018-12-13"
        assert "2018-26957" in item["version_or_docket"] and "FDA-2018-D-3984" in item["version_or_docket"]
        assert item["legal_weight"] == "guidance" and item["queue_state"] == "pending"
        assert item["fetched_url"].endswith("/documents/2018-26957.json")
        assert "Docket: Docket No. FDA-2018-D-3984" in item["text"]
        assert "Abstract: " in item["text"] and "data integrity" in item["text"].lower()
        assert "federalregister.gov/documents/2018/12/13/2018-26957" in item["text"]  # source URL
        assert "NOT STORED" in item["text"]
        assert item["checksum"] == hashlib.sha256(item["text"].encode()).hexdigest()
        assert reg.verify_audit()
    finally:
        app.dependency_overrides.clear()
