"""Cross-service consistency of the SYNTHETIC Phase 2 data (flat files only, stdlib)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
K = ROOT / "knowledge" / "data"
E = ROOT / "mock-eqms" / "data"


def _j(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_template_fields_match_between_dms_and_eqms():
    a, b = _j(K / "templates" / "deviation_template.json"), _j(E / "deviation_form.json")
    assert a["fields"] == b["fields"] and a["enums"] == b["enums"]
    assert a["template_id"] == b["template_id"] and a["record_kind"] == b["record_kind"] == "template"


def test_every_data_file_is_labelled_synthetic():
    for p in list(K.rglob("*.json")) + list(E.glob("*.json")):
        d = _j(p)
        assert d["synthetic"] is True and "SYNTHETIC" in d["notice"].upper(), p
    for p in (K / "sops").glob("*.md"):
        t = p.read_text(encoding="utf-8")
        assert "synthetic: true" in t and "SYNTHETIC / FICTIONAL" in t, p


def test_past_records_cite_real_sop_numbers_and_template_enums():
    tpl = _j(K / "templates" / "deviation_template.json")
    sop_numbers = set()
    for p in (K / "sops").glob("*.md"):
        sop_numbers.add(re.search(r"^sop_number:\s*(\S+)", p.read_text(encoding="utf-8"), re.M).group(1))
    recs = _j(E / "deviations_seed.json")["records"]
    assert len(recs) == 20
    for r in recs:
        assert set(r["related_sops"]) <= sop_numbers, r["id"]
        assert r["event_type"] in tpl["enums"]["event_type"] and r["site"] in tpl["enums"]["site"]
        assert r["severity"] in tpl["enums"]["severity"]
    assert len({r["id"] for r in recs}) == 20


def test_no_real_company_names_in_synthetic_data():
    text = " ".join(p.read_text(encoding="utf-8") for p in list(K.rglob("*.*")) + list(E.glob("*.json")) if p.suffix in {".md", ".json"})
    for real in ("Pfizer", "Novartis", "Merck", "Roche", "Sanofi", "GlaxoSmithKline", "AstraZeneca", "Johnson & Johnson"):
        assert real.lower() not in text.lower()
    assert "Examplon" in text


def test_template_fields_are_tagged_with_the_real_urs_ids():
    """Aligned to the real URS (GW-01..GW-03, IK-06); guards against drifting back to guesses."""
    t = _j(K / "templates" / "deviation_template.json")
    assert t["urs_status"] == "aligned" and "ASSUMED" not in json.dumps(t)
    by = {f["name"]: f for f in t["fields"]}
    # GW-01 minimal initiator fields: record type, description, site, product or material, lot, equipment, date discovered
    assert set(t["gw01_minimal_fields"]) == {"event_type", "description", "site", "product", "lot_number",
                                              "equipment_id", "detected_date"}
    assert {n for n, f in by.items() if "GW-01" in f["urs"]} == set(t["gw01_minimal_fields"])
    assert by["detected_date"]["label"] == "Date discovered" and by["product"]["label"] == "Product or material"
    # GW-02: proposed by the system, confirmed by the initiator
    assert {n for n, f in by.items() if "GW-02" in f["urs"]} == {"event_type", "severity"}
    # GW-03: required by the SOP/form but not part of the initiator's minimal entry
    assert {n for n, f in by.items() if "GW-03" in f["urs"]} == {"event_date", "immediate_action", "reported_by"}
    assert all(f["urs"] and set(f["urs"]) <= {"GW-01", "GW-02", "GW-03"} for f in t["fields"])
    assert t["fields"] == _j(E / "deviation_form.json")["fields"]


def test_no_assumed_urs_labels_remain_in_data_files():
    for p in list(K.rglob("*.json")) + list(E.glob("*.json")):
        assert "ASSUMED" not in p.read_text(encoding="utf-8"), p


def test_every_seed_record_is_versioned_for_citation():
    for r in _j(E / "deviations_seed.json")["records"]:
        assert r["version"] == "1.0", r["id"]
