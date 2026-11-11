import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from app.index import DATA_DIR, IndexError_, KnowledgeIndex
from app.loader import LoadError


def make_index(tmp_path, today="2026-11-11", mutate=None) -> KnowledgeIndex:
    data = tmp_path / "data"
    if not data.exists():
        shutil.copytree(DATA_DIR, data, ignore=shutil.ignore_patterns("*.db"))
    if mutate:
        mutate(data)
    return KnowledgeIndex(data, str(tmp_path / "k.db"), today=lambda: date.fromisoformat(today))


@pytest.fixture
def idx(tmp_path):
    return make_index(tmp_path)


def eff(idx):
    return {(d["sop_number"], d["version"]) for d in idx.list_documents()}


# ---------------------------------------------------------------- data loads
def test_all_sops_load_and_are_labelled_synthetic(idx):
    docs = idx.list_documents(include_all=True)
    assert len(docs) == 10 and len({d["sop_number"] for d in docs}) == 7
    assert all(d["synthetic"] for d in docs)
    for p in (DATA_DIR / "sops").glob("*.md"):
        assert "synthetic: true" in p.read_text(encoding="utf-8") and "SYNTHETIC / FICTIONAL" in p.read_text(encoding="utf-8")


def test_sops_cover_deviation_capa_and_change_control(idx):
    nums = {d["sop_number"] for d in idx.list_documents()}
    assert {"SOP-DEV-001", "SOP-DEV-002", "SOP-INV-001", "SOP-CAPA-001", "SOP-CAPA-002", "SOP-CHG-001", "SOP-CHG-002"} == nums
    for d in idx.list_documents():
        full = idx.get_document(d["sop_number"])
        assert full["version"] and full["effective_date"] and len(full["sections"]) >= 4


def test_deviation_template_loaded(idx):
    (t,) = idx.templates()
    assert t["kind"] == "template" and t["record_kind"] == "template" and t["effective"] is True
    assert set(t["required_fields"]) == {"event_type", "site", "product", "lot_number", "equipment_id", "event_date",
                                          "detected_date", "description", "immediate_action", "severity", "reported_by"}


def test_malformed_files_fail_loudly(tmp_path):
    def break_it(data):
        (data / "sops" / "BAD.md").write_text("no header here\n## 1 X\ntext\n", encoding="utf-8")
    with pytest.raises(LoadError):
        make_index(tmp_path, mutate=break_it)


def test_unlabelled_sop_is_rejected(tmp_path):
    def unlabel(data):
        p = data / "sops" / "SOP-CHG-002_v1.0.md"
        p.write_text(p.read_text(encoding="utf-8").replace("synthetic: true", "synthetic: false"), encoding="utf-8")
    with pytest.raises(LoadError, match="synthetic"):
        make_index(tmp_path, mutate=unlabel)


# ---------------------------------------------------------------- versioning / effective flag (IK-01)
def test_default_listing_is_effective_versions_only(idx):
    assert eff(idx) == {("SOP-DEV-001", "3.0"), ("SOP-DEV-002", "2.0"), ("SOP-INV-001", "1.1"),
                        ("SOP-CAPA-001", "4.0"), ("SOP-CAPA-002", "1.0"), ("SOP-CHG-001", "2.0"), ("SOP-CHG-002", "1.0")}


def test_include_all_shows_superseded_draft_and_future_flagged_not_effective(idx):
    allv = {(d["sop_number"], d["version"]): d for d in idx.list_documents(include_all=True)}
    for key in [("SOP-DEV-001", "2.0"), ("SOP-CAPA-001", "5.0"), ("SOP-CHG-001", "3.0")]:
        assert allv[key]["effective"] is False
    assert allv[("SOP-DEV-001", "2.0")]["status"] == "superseded"
    assert allv[("SOP-CAPA-001", "5.0")]["status"] == "draft" and allv[("SOP-CAPA-001", "5.0")]["effective_date"] is None
    assert allv[("SOP-CHG-001", "3.0")]["status"] == "approved" and allv[("SOP-CHG-001", "3.0")]["effective_date"] == "2027-03-01"


def test_get_document_defaults_to_effective_but_old_version_is_reachable_explicitly(idx):
    assert idx.get_document("SOP-DEV-001")["version"] == "3.0"
    old = idx.get_document("SOP-DEV-001", "2.0")
    assert old["effective"] is False and old["status"] == "superseded"
    with pytest.raises(IndexError_) as e:
        idx.get_document("SOP-NOPE")
    assert e.value.status == 404
    with pytest.raises(IndexError_):
        idx.get_document("SOP-DEV-001", "9.9")
    assert [v["version"] for v in idx.versions("SOP-DEV-001")] == ["2.0", "3.0"]


def test_effective_date_in_future_is_not_effective_until_reached(tmp_path):
    def promote(data):  # CHG-001 v3.0 flagged 'effective' but dated 2027-03-01; v2.0 retired
        v3 = data / "sops" / "SOP-CHG-001_v3.0_FUTURE.md"
        v3.write_text(v3.read_text(encoding="utf-8").replace("status: approved", "status: effective"), encoding="utf-8")
        v2 = data / "sops" / "SOP-CHG-001_v2.0.md"
        v2.write_text(v2.read_text(encoding="utf-8").replace("status: effective", "status: superseded"), encoding="utf-8")
    before = make_index(tmp_path / "a", today="2027-02-28", mutate=promote)
    assert ("SOP-CHG-001", "3.0") not in eff(before) and ("SOP-CHG-001", "2.0") not in eff(before)
    after = make_index(tmp_path / "b", today="2027-03-01", mutate=promote)
    assert ("SOP-CHG-001", "3.0") in eff(after)


def test_two_effective_versions_of_one_sop_fail_the_load(tmp_path):
    def clash(data):
        p = data / "sops" / "SOP-DEV-001_v2.0.md"
        p.write_text(p.read_text(encoding="utf-8").replace("status: superseded", "status: effective"), encoding="utf-8")
    with pytest.raises(LoadError, match="more than one effective"):
        make_index(tmp_path, mutate=clash)


# ---------------------------------------------------------------- search
def test_search_ranks_the_relevant_section_first(idx):
    hits = idx.search("report deviation within 24 hours of detection")
    assert hits[0]["id"] == "SOP-DEV-001:5.1" and hits[0]["effective"] is True and hits[0]["version"] == "3.0"
    assert "24 hours" in hits[0]["snippet"]


def test_search_excludes_superseded_text_by_default_and_flags_it_when_included(idx):
    default = [h for h in idx.search("48 hours") if h["kind"] == "sop"]
    assert all(h["version"] != "2.0" and h["effective"] for h in default)  # superseded text never appears
    hits = idx.search("48 hours", include_non_effective=True)
    assert hits[0]["id"] == "SOP-DEV-001:5.1" and hits[0]["version"] == "2.0" and hits[0]["effective"] is False


def test_search_never_returns_draft_text_by_default(idx):
    assert all(h["version"] != "5.0" for h in idx.search("prioritised high medium low"))
    assert any(h["version"] == "5.0" and h["effective"] is False
               for h in idx.search("prioritised high medium low", include_non_effective=True))


def test_search_kind_filter_and_validation(idx):
    assert {h["kind"] for h in idx.search("deviation", kind="sop")} == {"sop"}
    assert {h["kind"] for h in idx.search("event type lot equipment", kind="template")} == {"template"}
    with pytest.raises(IndexError_):
        idx.search("   ")
    with pytest.raises(IndexError_):
        idx.search("deviation", kind="bogus")


# ---------------------------------------------------------------- executed records vs templates (IK-06)
REC = {"id": "SYN-DEV-2025-099", "version": "1.0", "record_kind": "record", "status": "closed", "event_type": "equipment_failure",
       "severity": "major", "site": "SITE-RB", "product": "Examplamol 500 mg tablets",
       "description": "Granulator bearing noise", "root_cause": "bearing wear", "capa": {"capa_id": "CAPA-X"}}


def test_executed_records_are_indexed_and_templates_are_refused(idx):
    assert idx.index_records([REC]) == {"indexed": 1, "source": "mock-eqms"}
    with pytest.raises(IndexError_, match="only executed records"):
        idx.index_records([{"id": "TPL-DEV-001", "record_kind": "template"}])
    assert [r["id"] for r in idx.list_records()] == ["SYN-DEV-2025-099"]
    assert idx.list_records()[0]["kind"] == "executed_record"


def test_search_tells_templates_from_executed_records(idx):
    idx.index_records([REC])
    kinds = {h["kind"] for h in idx.search("granulator bearing")}
    assert kinds == {"executed_record"}
    assert {h["kind"] for h in idx.search("event type", kind="template")} == {"template"}
    assert idx.list_records(status="draft") == [] and len(idx.list_records(event_type="equipment_failure")) == 1


def test_reindexing_the_same_record_is_idempotent(idx):
    idx.index_records([REC])
    idx.index_records([{**REC, "severity": "critical"}])
    (r,) = idx.list_records()
    assert r["severity"] == "critical"


# ---------------------------------------------------------------- traceability (IK-07 stub)
def test_traceability_lookup_resolves_to_effective_sop_sections(idx):
    (m,) = idx.traceability("211.192")
    assert m["citation"].startswith("21 CFR 211.192")
    got = {(i["sop_number"], i["section"], i["version"]) for i in m["implemented_by"]}
    assert ("SOP-DEV-001", "5.4", "3.0") in got and ("SOP-INV-001", "4", "1.1") in got
    assert all(i["resolved"] and not i["stale"] for i in m["implemented_by"])
    assert next(i for i in m["implemented_by"] if i["section"] == "5.4")["section_title"] == "Investigation"


def test_traceability_all_and_reverse_lookup_and_unknown(idx):
    assert {m["requirement_id"] for m in idx.traceability()} >= {"211.192", "211.100", "211.22", "211.180"}
    assert "211.100" in idx.requirements_for_section("SOP-DEV-001", "5.1")
    with pytest.raises(IndexError_) as e:
        idx.traceability("999.999")
    assert e.value.status == 404


def test_traceability_flags_mapping_when_sop_has_no_effective_version(tmp_path):
    def retire(data):
        p = data / "sops" / "SOP-CAPA-001_v4.0.md"
        p.write_text(p.read_text(encoding="utf-8").replace("status: effective", "status: superseded"), encoding="utf-8")
    i = make_index(tmp_path, mutate=retire)
    (m,) = i.traceability("211.22")
    capa = [x for x in m["implemented_by"] if x["sop_number"] == "SOP-CAPA-001"]
    assert capa and all(x["stale"] and not x["resolved"] and x["note"] for x in capa)
    assert any(x["resolved"] for x in m["implemented_by"] if x["sop_number"] == "SOP-CHG-001")


def test_traceability_pointing_at_missing_section_fails_the_load(tmp_path):
    def bad(data):
        p = data / "traceability.json"
        t = json.loads(p.read_text(encoding="utf-8"))
        t["mappings"][0]["implemented_by"].append({"sop_number": "SOP-DEV-001", "section": "99.9"})
        p.write_text(json.dumps(t), encoding="utf-8")
    with pytest.raises(LoadError, match="does not exist"):
        make_index(tmp_path, mutate=bad)


# ---------------------------------------------------------------- IK-05 / IK-01: version always shown, records cited by ID + version
def test_records_without_a_version_cannot_be_indexed(idx):
    no_version = {k: v for k, v in REC.items() if k != "version"}
    with pytest.raises(IndexError_, match="version"):
        idx.index_records([no_version])
    with pytest.raises(IndexError_):
        idx.index_records([{**REC, "version": " "}])
    assert idx.list_records() == []


def test_executed_records_are_cited_by_id_and_version(idx):
    idx.index_records([REC, {**REC, "id": "SYN-DEV-DRAFT-0001", "version": "0.1", "status": "draft"}])
    assert {r["citation"] for r in idx.list_records()} == {"SYN-DEV-2025-099 v1.0", "SYN-DEV-DRAFT-0001 v0.1"}
    hit = idx.search("granulator bearing", kind="executed_record")[0]
    assert hit["id"] == "SYN-DEV-2025-099" and hit["version"] == "1.0" and hit["citation"] == "SYN-DEV-2025-099 v1.0"


def test_every_search_hit_and_document_shows_its_version(idx):
    idx.index_records([REC])
    for h in idx.search("deviation event type record"):
        assert h["version"], h
    assert all(d["version"] for d in idx.list_documents(include_all=True))
    assert all(t["version"] for t in idx.templates())


# ---------------------------------------------------------------- Phase 3 retrieval support
def test_search_can_return_full_text_and_record_by_id(idx):
    plain = idx.search("deviation originator 24 hours", kind="sop")[0]
    full = idx.search("deviation originator 24 hours", kind="sop", include_text=True)[0]
    assert "text" not in plain and "24 hours" in full["text"]
    idx.index_records([REC])
    got = idx.get_record("SYN-DEV-2025-099")
    assert got["citation"] == "SYN-DEV-2025-099 v1.0" and got["root_cause"] == "bearing wear"
    with pytest.raises(IndexError_) as e:
        idx.get_record("SYN-NOPE")
    assert e.value.status == 404


# ---------------------------------------------------------------- word-form matching (lightweight retrieval fix)
def test_stem_groups_common_word_forms():
    from app.index import stem
    assert len({stem(w) for w in ("report", "reports", "reported", "reporting")}) == 1
    assert len({stem(w) for w in ("require", "requires", "required", "requiring")}) == 1
    assert len({stem(w) for w in ("record", "records", "recorded")}) == 1
    assert stem("24") == "24" and stem("5.1") == "5.1" and stem("sop") == "sop" and stem("class") == "class"
    assert stem("deviations") == stem("deviation") and stem("classes") == stem("class")


def test_question_in_a_different_word_form_finds_the_sop_section(idx):
    """Live-run finding: 'reported ... reporting time' did not retrieve SOP-DEV-001 5.1 ('initial report') at all."""
    q = "A deviation was reported 3 days after it was discovered. What does our SOP require for reporting time?"
    ids = [h["id"] for h in idx.search(q, kind="sop", limit=8)]
    assert "SOP-DEV-001:5.1" in ids and ids.index("SOP-DEV-001:5.1") < 5


def test_stemmed_search_still_excludes_superseded_text_and_keeps_snippets_readable(idx):
    hits = idx.search("originator reporting events", kind="sop")
    assert all(h["effective"] for h in hits) and hits[0]["snippet"]
    assert "reported" in " ".join(h["snippet"].lower() for h in hits) or "report" in " ".join(h["snippet"].lower() for h in hits)
