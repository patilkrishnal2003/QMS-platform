"""Fetch flow + HTTP routes, with the network faked. No real request is made here."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import allowlist, fetcher
from app.main import app, get_registry
from app.registry import Registry

ECFR_XML = ('<DIV8 N="211.22" TYPE="SECTION"><HEAD>§ 211.22 Responsibilities of quality control unit.</HEAD>'
            '<P>(a) There shall be a quality control unit.</P><P>(b) Adequate <I>laboratory</I> facilities.</P></DIV8>')


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._d = status, text, data

    def json(self):
        return self._d


@pytest.fixture
def client(tmp_path, monkeypatch):
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    monkeypatch.delenv("GATEWAY_ALLOWLIST", raising=False)
    yield TestClient(app), reg
    app.dependency_overrides.clear()


def H(u):
    return {"X-User": u}


def activate(c, seed_index=0):
    created = c.post("/seed-defaults", headers=H("steward-a")).json()
    sid = created[seed_index]["id"]
    cid = created[seed_index]["pending_change"]["id"]
    assert c.post(f"/changes/{cid}/approve", headers=H("steward-b")).status_code == 200
    return sid


def test_allowlist_is_exactly_the_two_public_regulatory_hosts():
    assert allowlist.allowed_hosts() == {"www.ecfr.gov", "www.federalregister.gov"}


def test_ecfr_xml_flattened_one_paragraph_per_line():
    t = fetcher.ecfr_xml_to_text(ECFR_XML)
    assert t.splitlines() == ["§ 211.22 Responsibilities of quality control unit.",
                              "(a) There shall be a quality control unit.", "(b) Adequate laboratory facilities."]


def test_fetch_ecfr_resolves_date_then_gets_xml():
    urls = []

    def get(url):
        urls.append(url)
        if url.endswith("titles.json"):
            return Resp(data={"titles": [{"number": 21, "up_to_date_as_of": "2026-11-09"}]})
        return Resp(text=ECFR_XML)

    doc = fetcher.fetch_document({"url": "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-21.xml?part=211&section=211.22"}, get)
    assert urls[1] == "https://www.ecfr.gov/api/versioner/v1/full/2026-11-09/title-21.xml?part=211&section=211.22"
    assert doc.document_date == "2026-11-09" and doc.title == "21 CFR 211.22"


def test_fetch_federal_register_is_metadata_only_and_never_requests_full_text():
    seen = []

    def get(url):
        seen.append(url)
        return Resp(data={"title": "Data Integrity", "publication_date": "2018-12-13", "document_number": "2018-26957",
                          "docket_ids": ["Docket No. FDA-2018-D-3984"], "abstract": "FDA is announcing  the\navailability.",
                          "html_url": "https://www.federalregister.gov/documents/2018/12/13/2018-26957/x"})

    doc = fetcher.fetch_document({"url": "https://www.federalregister.gov/api/v1/documents/2018-26957.json"}, get)
    assert len(seen) == 1 and "2018-26957.json" in seen[0]
    assert not any(u.split("?")[0].endswith((".txt", ".xml", ".html")) for u in seen)
    assert doc.content_kind == "metadata_only"
    assert "Abstract: FDA is announcing the availability." in doc.text
    assert "Docket: Docket No. FDA-2018-D-3984" in doc.text and "NOT STORED" in doc.text
    assert doc.version_or_docket == "2018-26957 / Docket No. FDA-2018-D-3984" and doc.document_date == "2018-12-13"


def test_ecfr_doc_is_full_text():
    def get(url):
        if url.endswith("titles.json"):
            return Resp(data={"titles": [{"number": 21, "up_to_date_as_of": "2026-11-09"}]})
        return Resp(text=ECFR_XML)

    doc = fetcher.fetch_document({"url": "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-21.xml?part=211"}, get)
    assert doc.content_kind == "full_text"


def test_non_allowlisted_source_url_rejected_before_any_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    with pytest.raises(allowlist.UrlNotAllowed):
        fetcher.fetch_document({"url": "https://example.com/standard/1.html"})  # default getter = safe_get


def test_internal_post_only_reaches_library_internal_endpoints(monkeypatch):
    monkeypatch.setenv("LIBRARY_URL", "http://127.0.0.1:8001")

    def boom(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "post", boom)
    for bad in ["/queue/1/accept", "/internal/../health", "//evil.test/internal/x"]:
        with pytest.raises(allowlist.UrlNotAllowed):
            allowlist.internal_post(bad, {})


# ---- routes ----
def test_fetch_refused_until_source_is_active(client):
    c, _ = client
    created = c.post("/seed-defaults", headers=H("steward-a")).json()
    r = c.post(f"/sources/{created[0]['id']}/fetch", headers=H("steward-a"))
    assert r.status_code == 409


def test_two_person_rule_over_http(client):
    c, _ = client
    created = c.post("/seed-defaults", headers=H("steward-a")).json()
    cid = created[0]["pending_change"]["id"]
    assert c.post(f"/changes/{cid}/approve", headers=H("steward-a")).status_code == 403
    assert c.post(f"/changes/{cid}/approve", headers=H("viewer-1")).status_code == 403
    assert c.post(f"/changes/{cid}/approve").status_code == 401
    assert c.post(f"/changes/{cid}/approve", headers=H("qa-1")).json()["status"] == "active"
    assert [s["id"] for s in c.get("/sources?status=active", headers=H("viewer-1")).json()] == [created[0]["id"]]


def test_propose_source_with_non_allowlisted_url_is_400(client):
    c, _ = client
    body = {"name": "x", "url": "https://example.com/x", "jurisdiction": "US", "legal_weight": "guidance"}
    assert c.post("/sources", json=body, headers=H("steward-a")).status_code == 400


def test_trigger_fetch_posts_to_library_and_audits(client, monkeypatch):
    c, reg = client
    sid = activate(c, 1)
    sent = {}

    def fake_get(url, **kw):
        return Resp(data={"title": "T", "publication_date": "2018-12-13", "document_number": "2018-26957",
                          "docket_ids": [], "abstract": "abs", "html_url": "https://www.federalregister.gov/x"})

    def fake_post(path, json, **kw):
        sent.update(path=path, **json)
        return Resp(data={"result": "queued_new", "item_id": 7, "version_no": 1})

    monkeypatch.setattr(allowlist, "safe_get", fake_get)
    monkeypatch.setattr(allowlist, "internal_post", fake_post)
    r = c.post(f"/sources/{sid}/fetch", headers=H("steward-b"))
    assert r.status_code == 200 and r.json()["result"] == "queued_new"
    assert sent["path"] == "/internal/ingest" and sent["legal_weight"] == "guidance" and sent["content_kind"] == "metadata_only" and "abs" in sent["text"]
    assert sent["auto_accept"] is False and sent["retrieved_at"]
    assert reg.audit_entries()[-1]["action"] == "source.fetch"
    assert c.post(f"/sources/{sid}/fetch", headers=H("viewer-1")).status_code == 403


def test_fetch_of_source_whose_host_is_no_longer_allowed_is_refused_and_audited(client, monkeypatch):
    c, reg = client
    sid = activate(c, 1)
    monkeypatch.setattr(allowlist, "ALLOWED_HOSTS", [])  # allow-list tightened after approval
    r = c.post(f"/sources/{sid}/fetch", headers=H("steward-a"))
    assert r.status_code == 403
    assert reg.audit_entries()[-1]["action"] == "source.fetch_refused"


def test_audit_endpoint(client):
    c, _ = client
    activate(c)
    out = c.get("/audit", headers=H("viewer-1")).json()
    assert out["chain_valid"] is True and len(out["entries"]) == 3  # 2 seed proposals + 1 approval
