import pytest
from asgi_helper import call

from app.main import app, get_eqms
from app.store import DATA_DIR, Eqms


@pytest.fixture
def eqms(tmp_path):
    e = Eqms(str(tmp_path / "e.db"))
    app.dependency_overrides[get_eqms] = lambda: e
    yield e
    app.dependency_overrides.clear()


GOOD = {"event_type": "process_deviation", "site": "SITE-RB", "product": "Examplamol 500 mg tablets",
        "lot_number": "EXA-26-001", "equipment_id": "TAB-PRESS-02", "event_date": "2026-11-01",
        "detected_date": "2026-11-01", "description": "Tablet weight trending high.",
        "immediate_action": "Press stopped.", "severity": "minor", "reported_by": "U. Tester"}


def body(**kw):
    return {"record": GOOD, "human_confirmed": True, "confirmed_by": "qa-reviewer-1", **kw}


# ---------------------------------------------------------------- data
def test_twenty_synthetic_closed_records_load(eqms):
    recs = eqms.list_records(limit=1000)
    assert len(recs) == 20
    assert all(r["synthetic"] and r["record_kind"] == "record" and r["status"] == "closed" for r in recs)
    assert all(r["root_cause"] and r["capa"]["summary"] and r["closed_date"] for r in recs)


def test_records_are_varied_in_type_severity_and_site(eqms):
    recs = eqms.list_records(limit=1000)
    assert len({r["event_type"] for r in recs}) >= 9
    assert {r["severity"] for r in recs} == {"minor", "major", "critical"}
    assert len({r["site"] for r in recs}) == 3
    assert any(r["capa"]["capa_id"] == "none" for r in recs)  # some closed with correction only


def test_every_record_has_all_required_template_fields(eqms):
    for r in eqms.list_records(limit=1000):
        for f in eqms.required_fields():
            assert str(r.get(f, "")).strip(), (r["id"], f)


def test_seed_is_idempotent(tmp_path):
    db = str(tmp_path / "e.db")
    Eqms(db)
    assert len(Eqms(db).list_records(limit=1000)) == 20


def test_data_files_are_labelled_synthetic():
    import json
    for name in ("deviations_seed.json", "deviation_form.json"):
        d = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
        assert d["synthetic"] is True and "SYNTHETIC" in (d.get("notice") or "")


# ---------------------------------------------------------------- read-only connection (IK-02)
def test_read_by_type_and_filters(eqms):
    st, recs = call(app, "GET", "/read/records?type=equipment_failure")
    assert st == 200 and recs and {r["event_type"] for r in recs} == {"equipment_failure"}
    assert {r["severity"] for r in call(app, "GET", "/read/records?severity=critical")[1]} == {"critical"}
    assert call(app, "GET", "/read/records?type=nope")[1] == []
    st, one = call(app, "GET", "/read/records/" + recs[0]["id"])
    assert st == 200 and one["id"] == recs[0]["id"]
    assert call(app, "GET", "/read/records/SYN-NOPE")[0] == 404


def test_read_router_exposes_only_get(eqms):
    read_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/read")]
    assert read_routes
    for r in read_routes:
        assert r.methods <= {"GET", "HEAD"}, (r.path, r.methods)
    st, _ = call(app, "POST", "/read/records", {"x": 1})
    assert st == 405
    assert call(app, "DELETE", "/read/records/SYN-DEV-2024-001")[0] == 405


# ---------------------------------------------------------------- executed vs template (IK-06)
def test_templates_are_separate_from_executed_records(eqms):
    st, tpls = call(app, "GET", "/read/templates")
    assert st == 200 and [t["record_kind"] for t in tpls] == ["template"]
    ids = {r["id"] for r in call(app, "GET", "/read/records?limit=1000")[1]}
    assert tpls[0]["id"] not in ids
    assert all(r["record_kind"] == "record" for r in call(app, "GET", "/read/records?limit=1000")[1])
    assert call(app, "GET", "/read/records/" + tpls[0]["id"])[0] == 404  # a blank form is not a record


# ---------------------------------------------------------------- write only with human confirmation (IK-09)
def test_write_refused_without_confirmation_and_nothing_is_stored(eqms):
    for b in (body(human_confirmed=False), {"record": GOOD}, body(confirmed_by=""), body(confirmed_by="   ")):
        st, out = call(app, "POST", "/write/records", b)
        assert st == 409 and "human confirmation" in out["detail"]
    assert len(eqms.list_records(limit=1000)) == 20


def test_confirmed_write_is_stored_as_draft_never_submitted(eqms):
    st, out = call(app, "POST", "/write/records",
                   body(record={**GOOD, "status": "closed", "closed_date": "2026-11-02", "capa": {"capa_id": "X"}}))
    assert st == 201
    assert out["id"] == "SYN-DEV-DRAFT-0001" and out["status"] == "draft" and out["record_kind"] == "record"
    assert out["confirmed_by"] == "qa-reviewer-1" and out["confirmed_at"] and out["synthetic"] is True
    assert "closed_date" not in out and "capa" not in out  # caller cannot close or add outcomes
    drafts = call(app, "GET", "/read/records?status=draft")[1]
    assert [d["id"] for d in drafts] == ["SYN-DEV-DRAFT-0001"]
    assert len(eqms.list_records(limit=1000)) == 21
    assert call(app, "POST", "/write/records", body())[1]["id"] == "SYN-DEV-DRAFT-0002"


def test_write_validates_required_fields_enums_and_kind(eqms):
    missing = {k: v for k, v in GOOD.items() if k != "lot_number"}
    st, out = call(app, "POST", "/write/records", body(record=missing))
    assert st == 422 and "lot_number" in out["detail"]
    assert call(app, "POST", "/write/records", body(record={**GOOD, "severity": "catastrophic"}))[0] == 422
    assert call(app, "POST", "/write/records", body(record={**GOOD, "site": "SITE-ZZ"}))[0] == 422
    st, out = call(app, "POST", "/write/records", body(record={**GOOD, "record_kind": "template"}))
    assert st == 400 and "templates" in out["detail"]
    assert len(eqms.list_records(limit=1000)) == 20


# ---------------------------------------------------------------- IK-05: records carry a version for citation
def test_every_record_has_a_version_and_drafts_start_at_0_1(eqms):
    assert all(r["version"] == "1.0" for r in eqms.list_records(limit=1000))
    st, out = call(app, "POST", "/write/records", body())
    assert st == 201 and out["version"] == "0.1"
    ok = {(r["id"], r["version"]) for r in call(app, "GET", "/read/records?limit=1000")[1]}
    assert ("SYN-DEV-DRAFT-0001", "0.1") in ok and ("SYN-DEV-2024-001", "1.0") in ok
    # a caller cannot choose its own version
    assert call(app, "POST", "/write/records", body(record={**GOOD, "version": "9.9"}))[1]["version"] == "0.1"
