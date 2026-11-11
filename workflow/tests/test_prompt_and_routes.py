import json

from asgi_helper import call

from app import prompt
from app.main import app


def src(sid, ver, score, typ="sop", **kw):
    return {"type": typ, "source_id": sid, "version": ver, "title": sid, "score": score,
            "passages": [{"locator": "1", "text": f"text of {sid}"}], **kw}


# ---- merge / context
def test_merge_dedupes_orders_by_type_then_score_and_caps():
    srcs = [src("sop:A", "1", 1), src("sop:A", "1", 5), src("req:211.1", None, 1, "req"), src("library:1", "1", 1, "library"),
            src("record:R", "1.0", 9, "record"), src("sop:B", "1", 3)]
    m = prompt.merge_sources(srcs, 4)
    assert [s["source_id"] for s in m] == ["req:211.1", "library:1", "sop:A", "sop:B"]
    assert m[2]["score"] == 5


def test_build_prompt_shows_ids_versions_locators_and_marks_non_effective():
    p = prompt.build_prompt("What is the rule?", [src("sop:A", "2.0", 1, effective=False, status="superseded"),
                                                 {"type": "library", "source_id": "library:3", "version": "1", "title": "reg",
                                                  "lines": ["l1", "l2"], "passages": [], "score": 1}], "SYSTEM")
    user = p["messages"][1]["content"]
    assert p["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert "source_id=sop:A version=2.0" in user and "(NOT EFFECTIVE)" in user
    assert "[locator: lines:2] l2" in user and "[locator: 1] text of sop:A" in user
    assert p["context_source_ids"] == ["library:3@1", "sop:A@2.0"] and p["schema"]["required"] == ["statements", "no_support_found", "gaps"]
    assert "no_support_found" in user and "word for word" in user


def test_context_is_truncated_to_the_configured_size():
    big = {"type": "sop", "source_id": "sop:A", "version": "1", "title": "t", "passages": [{"locator": "1", "text": "x" * 5000}]}
    p = prompt.build_prompt("q", [big], "S", max_context_chars=500)
    assert len(p["messages"][1]["content"]) < 1500 and "[truncated]" in p["messages"][1]["content"]


# ---- parse
GOOD = {"statements": [{"text": "T", "kind": "procedure", "citations": [{"source_id": "sop:A", "version": "1", "locator": "1", "quote": "q"}]}],
        "no_support_found": False, "gaps": ["g"]}


def test_parse_valid_json_and_collects_cited_sources():
    r = prompt.parse_output(json.dumps(GOOD))
    assert r["ok"] and r["statements"][0]["citations"][0]["locator"] == "1" and r["cited"] == [{"source_id": "sop:A", "version": "1"}]
    assert r["gaps"] == ["g"] and r["no_support_found"] is False


def test_parse_tolerates_code_fences_and_chatter():
    assert prompt.parse_output("```json\n" + json.dumps(GOOD) + "\n```")["ok"]
    assert prompt.parse_output("Here you go: " + json.dumps(GOOD) + " thanks")["ok"]


def test_parse_failures_are_reported_not_raised():
    for raw in ("", "not json", "[1,2]", '{"statements": "x"}', None):
        r = prompt.parse_output(raw)
        assert r["ok"] is False and r["error"] and r["statements"] == []


def test_malformed_statement_items_are_kept_as_citationless():
    r = prompt.parse_output(json.dumps({"statements": ["plain string", {"text": "ok", "citations": "bad"}, 5], "no_support_found": False}))
    assert r["ok"] and len(r["statements"]) == 3
    assert all(s["citations"] == [] for s in r["statements"]) and r["statements"][0]["malformed"] is True


# ---- routes (raw ASGI)
def test_workflow_routes_are_pure_compute():
    st, p = call(app, "POST", "/internal/build-prompt", {"query": "q", "sources": [src("sop:A", "1", 1)], "system_prompt": "S"})
    assert st == 200 and p["context_source_ids"] == ["sop:A@1"]
    assert call(app, "POST", "/internal/build-prompt", {"sources": []})[0] == 400
    st, parsed = call(app, "POST", "/internal/parse", {"raw": json.dumps(GOOD)})
    assert st == 200 and parsed["ok"]
    sources = {"sop:A@1": {"type": "sop", "source_id": "sop:A", "version": "1", "status": "effective", "effective": True, "link": "L",
                           "passages": [{"locator": "1", "text": "the quick brown fox"}]}}
    st, out = call(app, "POST", "/internal/verify", {"statements": [
        {"text": "the quick brown fox", "citations": [{"source_id": "sop:A", "version": "1", "locator": "1", "quote": "quick brown"}]},
        {"text": "nothing cited"}], "sources": sources})
    assert st == 200 and [r["verdict"] for r in out["results"]] == ["verified", "unsupported"]
    assert out["summary"]["flagged"] == 1
    assert call(app, "GET", "/internal/verify")[0] == 405


def test_passage_template_and_instructions_come_from_the_caller():
    p = prompt.build_prompt("q", [src("sop:A", "1", 1)], "S", passage_template="<<{locator}>> {text}", instructions="CUSTOM RULES")
    user = p["messages"][1]["content"]
    assert "<<1>> text of sop:A" in user and user.endswith("CUSTOM RULES")
    import pytest
    with pytest.raises(prompt.PromptConfigError):
        prompt.build_prompt("q", [src("sop:A", "1", 1)], "S", passage_template="{text} only")
    st, _ = call(app, "POST", "/internal/build-prompt", {"query": "q", "sources": [], "system_prompt": "S",
                                                          "passage_template": "bad"})
    assert st == 400
