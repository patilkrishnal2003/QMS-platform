from datetime import date

import pytest
from asgi_helper import call

from app.index import DATA_DIR, KnowledgeIndex
from app.main import app, get_index


@pytest.fixture(autouse=True)
def _idx(tmp_path):
    i = KnowledgeIndex(DATA_DIR, str(tmp_path / "k.db"), today=lambda: date(2026, 11, 11))
    app.dependency_overrides[get_index] = lambda: i
    yield i
    app.dependency_overrides.clear()


def test_documents_default_effective_only_and_include_all():
    st, docs = call(app, "GET", "/documents")
    assert st == 200 and len(docs) == 7 and all(d["effective"] for d in docs)
    assert len(call(app, "GET", "/documents?include_all=true")[1]) == 10


def test_document_versions_and_404():
    assert call(app, "GET", "/documents/SOP-DEV-001")[1]["version"] == "3.0"
    assert call(app, "GET", "/documents/SOP-DEV-001?version=2.0")[1]["effective"] is False
    assert [v["version"] for v in call(app, "GET", "/documents/SOP-DEV-001/versions")[1]] == ["2.0", "3.0"]
    assert call(app, "GET", "/documents/SOP-NOPE")[0] == 404


def test_search_route():
    st, hits = call(app, "GET", "/search?q=root+cause+evidence")
    assert st == 200 and hits[0]["sop_number"] == "SOP-INV-001"
    assert call(app, "GET", "/search?q=%20%20")[0] == 400


def test_records_route_accepts_records_and_refuses_templates():
    rec = {"id": "SYN-DEV-2025-099", "version": "1.0", "record_kind": "record", "status": "closed", "event_type": "equipment_failure"}
    assert call(app, "POST", "/internal/index-records", {"records": [rec]})[1]["indexed"] == 1
    assert call(app, "POST", "/internal/index-records", {"records": [{"id": "T", "record_kind": "template"}]})[0] == 400
    assert [r["id"] for r in call(app, "GET", "/records")[1]] == ["SYN-DEV-2025-099"]
    assert call(app, "GET", "/records")[1][0]["citation"] == "SYN-DEV-2025-099 v1.0"
    assert call(app, "POST", "/internal/index-records", {"records": [{"id": "X", "record_kind": "record"}]})[0] == 400
    assert call(app, "GET", "/templates")[1][0]["kind"] == "template"


def test_traceability_routes():
    st, m = call(app, "GET", "/traceability/211.192")
    assert st == 200 and m["implemented_by"][0]["resolved"] is True
    assert call(app, "GET", "/traceability/0.0")[0] == 404
    assert len(call(app, "GET", "/traceability")[1]) >= 5


def test_search_include_text_and_record_by_id_routes():
    st, hits = call(app, "GET", "/search?q=originator+record+event&kind=sop&include_text=true")
    assert st == 200 and "text" in hits[0]
    rec = {"id": "SYN-DEV-2025-098", "version": "1.0", "record_kind": "record", "status": "closed", "event_type": "x"}
    call(app, "POST", "/internal/index-records", {"records": [rec]})
    assert call(app, "GET", "/records/SYN-DEV-2025-098")[1]["citation"] == "SYN-DEV-2025-098 v1.0"
    assert call(app, "GET", "/records/NOPE")[0] == 404
