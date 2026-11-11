import json

from asgi_helper import call

from app import rules, tasks
from app.main import app, get_engine
from app.engine import Engine, Store

ET = ["equipment_failure", "process_deviation", "laboratory_oos"]
CL = ["minor", "major", "critical"]
CITE = {"source_id": "sop:SOP-DEV-002", "version": "2.0", "locator": "3", "quote": "Critical: confirmed"}


def src(sid, ver="1", typ="sop"):
    return {"type": typ, "source_id": sid, "version": ver, "title": sid, "score": 1,
            "passages": [{"locator": "1", "text": f"text of {sid}"}]}


BASE = {"sources": [src("answer:WF-0001:description", "1", "answer"), src("sop:SOP-DEV-002", "2.0")], "system_prompt": "SYS",
        "max_sources": 10, "max_context_chars": 6000, "passage_template": "[locator: {locator}] {text}", "instructions": "DO THE TASK."}


# ------------------------------------------------------------------ prompts
def test_proposals_prompt_lists_intake_sources_enums_and_the_schema():
    p = tasks.build_proposals_prompt({**BASE, "intake": {"description": "Force drifted.", "site": "SITE-RB"}, "event_types": ET, "classes": CL})
    user = p["messages"][1]["content"]
    assert "- description: Force drifted." in user and "[locator: 1] text of sop:SOP-DEV-002" in user and "DO THE TASK." in user
    assert "Allowed event types: equipment_failure" in user and p["messages"][0]["content"] == "SYS"
    props = p["schema"]["properties"]
    assert props["event_type"]["properties"]["value"]["enum"] == ET and props["classification"]["properties"]["value"]["enum"] == CL
    assert p["context_source_ids"][0] == "answer:WF-0001:description@1"                  # what people entered comes first


def test_gap_and_draft_prompts():
    g = tasks.build_gap_prompt({**BASE, "intake": {"description": "x"}, "existing_questions": ["Who reported it?"]})
    assert "- Who reported it?" in g["messages"][1]["content"] and g["schema"]["required"] == ["questions", "no_gaps"]
    d = tasks.build_draft_prompt({**BASE})
    assert all(s in d["messages"][1]["content"] for s in tasks.DRAFT_SECTIONS) and "'answer:' sources" in d["messages"][1]["content"]
    assert set(d["schema"]["properties"]["sections"]["properties"]) == set(tasks.DRAFT_SECTIONS)


# ------------------------------------------------------------------ proposals parsing + deterministic validity
GOOD = {"event_type": {"value": "equipment_failure", "reasoning": "A press sensor drifted.", "citations": [CITE]},
        "regulations": [{"requirement_id": "21 CFR 211.192", "reasoning": "Records review.", "citations": [CITE]},
                        {"requirement_id": "999.1", "reasoning": "Made up.", "citations": []}],
        "sops": [{"sop_number": "SOP-DEV-001", "reasoning": "Governs deviations.", "citations": [CITE]},
                 {"sop_number": "SOP-FAKE-001", "reasoning": "Made up.", "citations": []}],
        "classification": {"value": "major", "reasoning": "Potential quality impact.", "citations": [CITE]}}


def parse(obj, **kw):
    return tasks.parse_proposals(json.dumps(obj), ET, CL, ["211.192", "211.68"], ["SOP-DEV-001", "SOP-DEV-002"], **kw)


def test_parse_proposals_normalises_values_and_checks_validity_deterministically():
    r = parse(GOOD)
    assert r["ok"] and r["items"]["event_type"]["entries"][0]["validity"]["ok"]
    regs = r["items"]["regulations"]["entries"]
    assert regs[0]["value"] == "211.192" and regs[0]["validity"]["ok"]                    # '21 CFR 211.192' normalised
    assert not regs[1]["validity"]["ok"] and "not in the traceability map" in regs[1]["validity"]["problems"][0]
    sops = r["items"]["sops"]["entries"]
    assert sops[0]["validity"]["ok"] and not sops[1]["validity"]["ok"] and "no effective version" in sops[1]["validity"]["problems"][0]
    assert len(r["statements"]) == 6 and r["statements"][0]["item"] == "event_type"
    assert r["cited"] == [{"source_id": "sop:SOP-DEV-002", "version": "2.0"}]


def test_parse_proposals_flags_invalid_enum_values_and_never_raises():
    bad = {**GOOD, "event_type": {"value": "alien", "reasoning": "x", "citations": []}, "classification": {"value": "huge", "reasoning": "x", "citations": []}}
    r = parse(bad)
    assert not r["items"]["event_type"]["entries"][0]["validity"]["ok"] and not r["items"]["classification"]["entries"][0]["validity"]["ok"]
    for raw in ("", "not json", "[]", None):
        out = tasks.parse_proposals(raw, ET, CL, [], [])
        assert out["ok"] is False and out["error"]
    assert parse({})["items"]["event_type"]["entries"] == []                                # nothing proposed -> empty, not invented
    assert tasks.parse_proposals("```json\n" + json.dumps(GOOD) + "\n```", ET, CL, ["211.192"], ["SOP-DEV-001"])["ok"]


# ------------------------------------------------------------------ gap + draft parsing
def test_parse_gap_questions_keeps_citations_caps_the_count_and_drops_blank_ones():
    raw = json.dumps({"questions": [{"text": f"Q{i}?", "why": "Because 211.68.", "citations": [CITE]} for i in range(9)] +
                      [{"text": "  ", "why": "blank"}], "no_gaps": False})
    r = tasks.parse_gap(raw, max_questions=6)
    assert r["ok"] and len(r["questions"]) == 6 and len(r["statements"]) == 6 and r["questions"][0]["citations"][0]["locator"] == "3"
    assert tasks.parse_gap("nope")["ok"] is False and tasks.parse_gap(json.dumps({"questions": "x"}))["ok"] is False
    assert tasks.parse_gap(json.dumps({"questions": [], "no_gaps": True}))["no_gaps"] is True


def test_parse_draft_sections_and_malformed_entries_are_kept_not_dropped():
    raw = json.dumps({"sections": {"event_summary": [{"text": "Force rose.", "citations": [CITE]}, "just a string", {"text": "ok", "citations": "bad"}],
                                   "regulatory_and_procedural_context": [], "precedent_review": [{"text": "Past case.", "citations": []}]},
                      "no_support_found": False, "gaps": ["g"]})
    r = tasks.parse_draft(raw)
    assert r["ok"] and [len(r["sections"][s]) for s in tasks.DRAFT_SECTIONS] == [3, 0, 1] and r["gaps"] == ["g"]
    assert r["sections"]["event_summary"][1]["malformed"] is True and r["sections"]["event_summary"][1]["citations"] == []
    assert [s["section"] for s in r["statements"]] == ["event_summary"] * 3 + ["precedent_review"]
    assert tasks.parse_draft("")["ok"] is False and tasks.parse_draft(json.dumps({"sections": 1}))["ok"] is False


# ------------------------------------------------------------------ rules (pure)
def test_bank_is_synthetic_complete_and_every_element_says_what_it_satisfies():
    b = rules.bank()
    assert b["synthetic"] is True and len(rules.elements()) >= 25
    assert all(e["why"] and e["satisfies"] and e["question"] for e in rules.elements())
    keys = [e["key"] for e in rules.elements()]
    assert len(keys) == len(set(keys)) and {"root_cause", "product_impact", "reportability", "classification"} <= set(keys)
    assert set(b["decision_values"]) == {"classification", "product_impact", "reportability"}
    assert all(c["type"] in ("requirement", "sop", "field", "urs") for e in rules.elements() for c in e["satisfies"])


def test_condition_logic():
    st = {"decisions": {"event_type": {"value": "laboratory_oos", "tbd": False}}, "answers": {}}
    assert rules.applies(rules.element("oos_phase1_review"), st) and not rules.applies(rules.element("equipment_status"), st)
    assert not rules.applies(rules.element("lot_disposition"), st)                          # product_impact not decided yet
    assert rules.current_value({"decisions": {"x": {"value": "v", "tbd": True}}, "answers": {}}, "x") is None   # TBD is not a value


# ------------------------------------------------------------------ routes: refusals carry what a human must do next
def test_api_returns_a_structured_refusal_with_required_steps(tmp_path):
    e = Engine(Store(str(tmp_path / "a.db")))
    app.dependency_overrides[get_engine] = lambda: e
    import app.main as m
    m._engine = e
    try:
        st, rec = call(app, "POST", "/internal/deviations", {"actor": "initiator-1", "role": "initiator", "description": "x", "fields": {}})
        assert st == 200 and rec["id"] == "WF-0001"
        st, body = call(app, "POST", "/internal/deviations/WF-0001/advance", {"actor": "initiator-1", "role": "initiator", "to": "draft"})
        assert st == 409 and body["code"] == "skip" and body["required"] and "detail" in body
        st, body = call(app, "POST", "/internal/deviations/WF-0001/advance", {"actor": "qa-1", "role": "qa", "to": "questions"})
        assert st == 403 and body["code"] == "not_owner"
        assert call(app, "GET", "/internal/deviations/WF-9999")[0] == 404
        assert call(app, "POST", "/internal/deviations", {"actor": "viewer-1", "role": "viewer", "description": "x"})[0] == 403
        assert call(app, "POST", "/internal/deviations", {"actor": "initiator-1", "role": "initiator", "description": " "})[0] == 422
        assert call(app, "GET", "/internal/deviations/WF-0001/checklist")[0] == 200
        assert call(app, "GET", "/internal/deviations/WF-0001/sources/description")[1]["source_id"] == "answer:WF-0001:description"
        assert call(app, "GET", "/internal/deviations/WF-0001/draft")[0] == 404
        assert [r["id"] for r in call(app, "GET", "/internal/deviations")[1]] == ["WF-0001"]
        assert [x["action"] for x in call(app, "GET", "/internal/deviations/WF-0001/events")[1]] == ["create"]
    finally:
        m._engine = None
        app.dependency_overrides.clear()
