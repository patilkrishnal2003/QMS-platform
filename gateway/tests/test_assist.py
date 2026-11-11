"""Assistant orchestration with a fake world: stores, Ollama and (real) workflow logic, no network."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ai_config, allowlist, assist, retrieval, sources
from app.ai_config import ConfigNotApproved
from app.llm import LlmError, ModelNotInstalled, OllamaClient
from app.main import app, get_registry
from app.registry import Registry

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


WF_PROMPT = _load("wf_prompt", "workflow/app/prompt.py")      # real workflow logic, loaded by path
WF_VERIFY = _load("wf_verifier", "workflow/app/verifier.py")
LIB_LINES = json.loads((ROOT / "tests" / "fixtures" / "ecfr_211_22.json").read_text(encoding="utf-8"))["lines"]


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data, text or (json.dumps(data) if data is not None else "")

    def json(self):
        return self._d


SOP = {"sop_number": "SOP-DEV-001", "title": "Deviation Management", "version": "3.0", "status": "effective",
       "effective_date": "2026-01-15", "effective": True, "kind": "sop",
       "sections": [{"section_id": "5.1", "title": "Detection and initial report",
                     "text": "The originator records the event in the deviation record within 24 hours of detection."},
                    {"section_id": "5.3", "title": "Classification",
                     "text": "QA classifies the deviation as minor, major or critical within two working days using SOP-DEV-002."}]}
REC = {"id": "SYN-DEV-2024-001", "version": "1.0", "record_kind": "record", "status": "closed", "event_type": "equipment_failure",
       "severity": "major", "root_cause": "Calibration interval extended without a change assessment.",
       "capa": {"capa_id": "CAPA-24-004", "summary": "Restored 3-month interval."}}
DRAFT = {**REC, "id": "SYN-DEV-DRAFT-0001", "version": "0.1", "status": "draft"}
TRACE = {"requirement_id": "211.192", "citation": "21 CFR 211.192 - Production record review",
         "implemented_by": [{"sop_number": "SOP-DEV-001", "section": "5.1", "resolved": True, "stale": False,
                             "version": "3.0", "section_title": "Detection and initial report", "note": None}]}
LIB_ITEM = {"id": 7, "version_no": 1, "title": "21 CFR 211.22", "source_url": "u", "version_or_docket": "21 CFR 211.22",
            "status": "final", "content_kind": "full_text", "legal_weight": "regulation", "is_current": True,
            "text": "\n".join(LIB_LINES), "score": 2.0}

GOOD = {"statements": [
    {"text": "The originator must record the event in the deviation record within 24 hours of detection.", "kind": "procedure",
     "citations": [{"source_id": "sop:SOP-DEV-001", "version": "3.0", "locator": "5.1", "quote": "within 24 hours of detection"}]},
    {"text": "The quality control unit has the responsibility and authority to approve or reject all components.", "kind": "requirement",
     "citations": [{"source_id": "library:7", "version": "1", "locator": "lines:2",
                    "quote": "responsibility and authority to approve or reject all components"}]}],
    "no_support_found": False, "gaps": []}


class World:
    """Fake library + knowledge + workflow + Ollama. `reply` is what the 'model' says."""

    def __init__(self, reply=None, models=None, empty=False):
        self.reply = json.dumps(GOOD) if reply is None else reply
        self.models = models if models is not None else [
            {"name": "qwen2.5:14b-instruct", "digest": "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6", "size": 9},
            {"name": "qwen2.5:7b-instruct", "digest": "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e", "size": 5}]
        self.empty = empty
        self.chat_calls, self.requests = [], []

    def get(self, service, path, params=None, **kw):
        self.requests.append(("GET", service, path))
        q = (params or {}).get("q", "")
        if service == "ollama":
            return Resp(data={"models": self.models})
        if service == "library":
            if path == "/library/version":
                return Resp(data={"library_version": "lib-1-abc", "items": 1})
            if path == "/search":
                return Resp(data=[] if self.empty else [LIB_ITEM])
            if path == "/library/7":
                return Resp(data=LIB_ITEM)
            return Resp(404)
        if service == "knowledge":
            if path == "/documents":
                return Resp(data=[SOP])
            if path.startswith("/documents/"):
                return Resp(data=SOP) if path.endswith("SOP-DEV-001") and params.get("version") == "3.0" else Resp(404)
            if path == "/search":
                if self.empty:
                    return Resp(data=[])
                if params["kind"] == "sop":
                    return Resp(data=[{"sop_number": "SOP-DEV-001", "version": "3.0", "section_id": "5.1", "score": 3.0}])
                return Resp(data=[{"id": REC["id"], "status": "closed", "score": 2.0}, {"id": DRAFT["id"], "status": "draft", "score": 1.9}])
            if path.startswith("/records/"):
                rid = path.rsplit("/", 1)[1]
                return Resp(data={REC["id"]: REC, DRAFT["id"]: DRAFT}[rid]) if rid in (REC["id"], DRAFT["id"]) else Resp(404)
            if path == "/traceability":
                return Resp(data=[TRACE])
            if path.startswith("/traceability/"):
                return Resp(data=TRACE) if path.endswith("211.192") else Resp(404)
        return Resp(404)

    def post(self, service, path, json_body, **kw):
        self.requests.append(("POST", service, path))
        if service == "ollama":
            self.chat_calls.append(json_body)
            if isinstance(self.reply, Exception):
                raise self.reply
            return Resp(data={"model": json_body["model"], "message": {"content": self.reply}, "eval_count": 11,
                              "prompt_eval_count": 22, "total_duration": 5})
        assert service == "workflow", service
        if path == "/internal/build-prompt":
            p = json_body
            return Resp(data=WF_PROMPT.build_prompt(p["query"], p["sources"], p["system_prompt"], p["max_sources"], p["max_context_chars"]))
        if path == "/internal/parse":
            return Resp(data=WF_PROMPT.parse_output(json_body["raw"]))
        if path == "/internal/verify":
            return Resp(data=WF_VERIFY.verify(json_body["statements"], json_body["sources"], json_body.get("thresholds")))
        return Resp(404)


@pytest.fixture
def reg(tmp_path):
    return Registry(str(tmp_path / "g.db"))


def ask(world, reg, q="Originator reported a deviation late; what does the SOP require?", user="steward-a", **kw):
    return assist.ask(q, user, reg, get=world.get, post=world.post, **kw)


def call_entry(reg):
    return json.loads([e for e in reg.audit_entries() if e["action"] == "ai.call"][-1]["detail"])


# ---------------------------------------------------------------------------- happy path + DI-03 log
def test_supported_answer_is_verified_with_links_and_fully_logged(reg):
    w = World()
    out = ask(w, reg)
    assert out["status"] == "ok" and out["no_support_found"] is False
    assert [s["verdict"] for s in out["statements"]] == ["verified", "verified"] and all(s["usable"] for s in out["statements"])
    links = [c["link"] for s in out["statements"] for c in s["citations"]]
    assert links[0].endswith("/documents/SOP-DEV-001?version=3.0#5.1") and links[1].endswith("/library/7#lines%3A2")  # AI-01
    assert out["summary"]["flagged"] == 0 and "A person still confirms" in out["notice"]
    assert set(out["retrieved_source_ids"]) >= {"sop:SOP-DEV-001@3.0", "library:7@1", "record:SYN-DEV-2024-001@1.0", "req:211.192"}
    assert not any("DRAFT" in i for i in out["retrieved_source_ids"])              # open/draft records are not precedent

    e = reg.audit_entries()[-1]
    d = json.loads(e["detail"])
    assert (e["actor"], e["action"]) == ("steward-a", "ai.call") and e["ts"] and e["subject"] == f"ai:{out['call_id']}"   # user, time
    assert d["input"]["query"].startswith("Originator reported") and d["prompt_messages"][1]["content"]                       # input
    assert d["retrieved_source_ids"] == out["retrieved_source_ids"]                                                          # source refs
    assert d["model"] == {"name": "qwen2.5:14b-instruct", "digest": "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6", "size": 9, "digest_pinned": True}                              # model version
    assert d["config"]["config_version"] == "1.4.0" and len(d["config"]["config_sha256"]) == 64 and d["config"]["approved_by"]  # config version
    assert d["settings"]["generation"]["temperature"] == 0 and d["settings"]["system_prompt"] and d["settings"]["retrieval"]
    assert d["library_version"] == "lib-1-abc" and d["knowledge_snapshot"].startswith("kn-1-")                                # library version
    assert d["raw_output"] == w.reply and d["output"]["statements"][0]["verdict"] == "verified"                              # output
    assert d["model_called"] is True and reg.verify_audit()


def test_generation_settings_and_model_come_only_from_the_approved_config(reg, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "some-other-model:70b")       # must have no effect (would bypass change control)
    w = World()
    ask(w, reg)
    body = w.chat_calls[0]
    assert body["model"] == "qwen2.5:14b-instruct" and body["stream"] is False
    assert body["options"] == {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 1500}
    assert body["format"]["required"] == ["statements", "no_support_found", "gaps"]
    assert body["messages"][0]["role"] == "system" and "ONLY from the numbered SOURCES" in body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    assert "[locator: 5.1] Detection and initial report." in user and "[locator: lines:2] (a)" in user   # config v1.1.0+ format
    assert "(5.1)" not in user and "value after 'locator:'" in user


# ---------------------------------------------------------------------------- AI-01 / AI-02
def test_fabricated_citations_are_flagged_never_passed(reg):
    bad = {"statements": [
        {"text": "Deviations must be closed within 7 days.", "kind": "procedure",
         "citations": [{"source_id": "sop:SOP-DEV-001", "version": "3.0", "locator": "9.9", "quote": "closed within 7 days"}]},
        {"text": "A made-up rule.", "kind": "requirement",
         "citations": [{"source_id": "sop:SOP-NOPE", "version": "1.0", "locator": "1", "quote": "made up"}]},
        {"text": "An uncited claim about a requirement.", "kind": "requirement", "citations": []},
        GOOD["statements"][0]], "no_support_found": False, "gaps": []}
    out = ask(World(json.dumps(bad)), reg)
    assert [s["verdict"] for s in out["statements"]] == ["invalid_citation", "invalid_citation", "unsupported", "verified"]
    assert [s["usable"] for s in out["statements"]] == [False, False, False, True]
    assert out["summary"]["flagged"] == 3 and "3 of 4 statements are flagged" in out["notice"]
    assert len(out["statements"]) == 4                                           # nothing silently dropped
    assert call_entry(reg)["cited_sources_resolved"]["sop:SOP-NOPE@1.0"] is False


def test_nothing_verified_says_so_and_model_no_support_is_passed_through(reg):
    allbad = {"statements": [{"text": "x", "kind": "other", "citations": []}], "no_support_found": False, "gaps": []}
    out = ask(World(json.dumps(allbad)), reg)
    assert out["no_support_found"] is True and "Treat all of it as unsupported" in out["notice"]
    out = ask(World(json.dumps({"statements": [], "no_support_found": True, "gaps": ["need the CAPA SOP"]})), reg)
    assert out["no_support_found"] is True and out["statements"] == [] and out["gaps"] == ["need the CAPA SOP"]
    assert "No supporting source" in out["notice"]


def test_no_retrieval_means_no_model_call_and_an_explicit_no_support_answer(reg):
    w = World(empty=True)
    out = ask(w, reg, q="zzz nothing matches")
    assert out["status"] == "no_support_found" and out["no_support_found"] and out["statements"] == []
    assert w.chat_calls == [] and out["model"] is None                            # never asked the model to improvise
    d = call_entry(reg)
    assert d["model_called"] is False and d["retrieved_source_ids"] == [] and d["config"]["config_version"] == "1.4.0"


def test_unparseable_model_output_is_not_presented_as_supported_but_is_logged(reg):
    out = ask(World("Sure! The SOP says 24 hours."), reg)
    assert out["status"] == "model_output_unparseable" and out["statements"] == [] and "not valid JSON" in out["notice"]
    assert call_entry(reg)["raw_output"] == "Sure! The SOP says 24 hours."


# ---------------------------------------------------------------------------- failures are logged too
def test_model_not_installed_is_a_clear_503_and_logged(reg):
    w = World(models=[])
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg)
    assert e.value.status == 503 and "ollama pull qwen2.5:14b-instruct" in str(e.value) and w.chat_calls == []
    last = reg.audit_entries()[-1]
    assert last["action"] == "ai.call_failed" and "not installed" in json.loads(last["detail"])["error"]


def test_ollama_down_is_502_and_logged(reg):
    with pytest.raises(assist.AssistError) as e:
        ask(World(reply=ConnectionError("refused")), reg)
    assert e.value.status == 502 and reg.audit_entries()[-1]["action"] == "ai.call_failed"


def test_retrieval_failure_is_502_not_a_fake_no_support(reg):
    w = World()
    orig = w.get
    w.get = lambda s, p, params=None, **k: Resp(500) if (s, p) == ("library", "/search") else orig(s, p, params, **k)
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg)
    assert e.value.status == 502 and "retrieval failed" in str(e.value)


def test_store_outage_while_checking_citations_is_502_not_a_fabrication_verdict(reg):
    w = World()
    orig = w.get
    w.get = lambda s, p, params=None, **k: Resp(500) if p == "/library/7" else orig(s, p, params, **k)
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg)
    assert e.value.status == 502 and "could not check cited sources" in str(e.value)


def test_empty_query_rejected(reg):
    with pytest.raises(assist.AssistError) as e:
        ask(World(), reg, q="   ")
    assert e.value.status == 400


# ---------------------------------------------------------------------------- AI-06 config under change control
@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    shutil.copytree(ai_config.CONFIG_DIR, d)
    monkeypatch.setenv("AI_CONFIG_DIR", str(d))
    return d


def test_repo_config_is_approved_and_pins_the_model():
    c = ai_config.load_approved()
    assert c.model == "qwen2.5:14b-instruct" and c.version == "1.4.0" and len(set(c.approved_by)) == 2
    assert c.approved_names() == ["qwen2.5:14b-instruct", "qwen2.5:7b-instruct"] and c.pinned_digest(c.model)
    assert c.generation["temperature"] == 0 and c.generation["seed"] == 42 and c.verifier == {"verified": 0.6, "low": 0.35}
    assert "keyword" in c.retrieval["method"]


def test_editing_the_config_without_change_control_blocks_every_ai_call(cfgdir, reg):
    p = cfgdir / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["generation"]["temperature"] = 0.9            # sneaky edit, no re-approval
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigNotApproved):
        ai_config.load_approved()
    w = World()
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg)
    assert e.value.status == 503 and "without change control" in str(e.value) and w.chat_calls == [] and w.requests == []
    assert reg.audit_entries()[-1]["action"] == "ai.call_refused"


def test_model_swap_needs_a_new_version_and_two_approvers(cfgdir):
    p = cfgdir / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["models"] = [{"name": "llama3.1:8b", "digest": "e" * 64}]
    cfg["default_model"], cfg["config_version"] = "llama3.1:8b", "1.2.1"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigNotApproved):
        ai_config.load_approved()
    with pytest.raises(ValueError):
        ai_config.approve(["steward-a"], "CC-2")                       # one person is not enough
    with pytest.raises(ValueError):
        ai_config.approve(["steward-a", "steward-a"], "CC-2")          # nor the same person twice
    with pytest.raises(ValueError):
        ai_config.approve(["steward-a", "viewer-1"], "CC-2")           # approver must be steward/QA
    ai_config.approve(["steward-a", "qa-1"], "CC-2")
    c = ai_config.load_approved()
    assert c.model == "llama3.1:8b" and c.version == "1.2.1" and c.change_ref == "CC-2"


def test_lock_with_a_single_approver_or_missing_is_refused(cfgdir):
    lock = cfgdir / "ai_config.lock.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    lock.write_text(json.dumps({**data, "approved_by": ["steward-a"]}), encoding="utf-8")
    with pytest.raises(ConfigNotApproved, match="two distinct"):
        ai_config.load_approved()
    lock.unlink()
    with pytest.raises(ConfigNotApproved, match="no approval lock"):
        ai_config.load_approved()


# ---------------------------------------------------------------------------- LLM client
def test_model_info_matches_exact_and_latest_tags_and_reports_digest():
    cfg = ai_config.load_approved()
    c = OllamaClient(cfg, get=World(models=[{"name": "qwen2.5:14b-instruct", "digest": "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"}]).get)
    assert c.model_info()["digest"] == "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"

    class C2:
        model = "mymodel"
        generation = cfg.generation
    assert OllamaClient(C2, get=World(models=[{"name": "mymodel:latest", "digest": "d2"}]).get).model_info()["digest"] == "d2"
    with pytest.raises(ModelNotInstalled):
        OllamaClient(cfg, get=World(models=[{"name": "other:7b"}]).get).model_info()


def test_client_error_mapping():
    cfg = ai_config.load_approved()
    with pytest.raises(LlmError):
        OllamaClient(cfg, get=lambda *a, **k: Resp(500)).model_info()
    with pytest.raises(LlmError):
        OllamaClient(cfg, post=lambda *a, **k: Resp(400, text="bad schema")).chat([], {})
    with pytest.raises(LlmError, match="no message content"):
        OllamaClient(cfg, post=lambda *a, **k: Resp(data={"done": True})).chat([], {})


# ---------------------------------------------------------------------------- retrieval + source fetching
def test_requirement_id_query_uses_traceability_then_sop_sections_and_library():
    w = World()
    got = retrieval.retrieve("21 CFR 211.192", {"library_top_k": 3, "sop_top_k": 5, "record_top_k": 3}, w.get)
    ids = [s["source_id"] for s in got["sources"]]
    assert got["requirement_id"] == "211.192" and ids[0] == "req:211.192" and "sop:SOP-DEV-001" in ids and "library:7" in ids
    assert not any(i.startswith("record:") for i in ids)                              # precedent only for event descriptions
    sop = next(s for s in got["sources"] if s["type"] == "sop")
    assert [p["locator"] for p in sop["passages"]] == ["5.1"]                           # only the mapped section


def test_free_text_query_pulls_library_sops_closed_records_and_suggested_requirements():
    w = World()
    got = retrieval.retrieve("tablet press drifted", {"library_top_k": 3, "sop_top_k": 5, "record_top_k": 3}, w.get)
    ids = [s["source_id"] for s in got["sources"]]
    assert {"library:7", "sop:SOP-DEV-001", "record:SYN-DEV-2024-001", "req:211.192"} <= set(ids)
    assert "record:SYN-DEV-DRAFT-0001" not in ids


def test_requirement_without_a_traceability_entry_is_noted():
    got = retrieval.retrieve("211.999", {"library_top_k": 3, "sop_top_k": 5, "record_top_k": 3}, World().get)
    assert any("no traceability entry for requirement 211.999" in n for n in got["notes"])


@pytest.mark.parametrize("q,expect", [("211.192", "211.192"), ("21 CFR 211.192", "211.192"), ("§ 211.22(a)", "211.22"),
                                       ("what is 211.192", None), ("deviation", None)])
def test_requirement_id_detection(q, expect):
    assert retrieval.parse_requirement(q) == expect


def test_fetch_source_existence_rules():
    w = World()
    assert sources.fetch_source("sop:SOP-DEV-001", "3.0", w.get)["version"] == "3.0"
    assert sources.fetch_source("sop:SOP-DEV-001", "v3.0", w.get) is not None            # 'v' prefix tolerated
    assert sources.fetch_source("sop:SOP-DEV-001", "2.0", w.get) is None                 # no such version
    assert sources.fetch_source("sop:SOP-DEV-001", "", w.get) is None                    # AI-01: version is required
    assert sources.fetch_source("library:7", "1", w.get) is not None
    assert sources.fetch_source("library:7", "2", w.get) is None                         # wrong item version
    assert sources.fetch_source("library:abc", "1", w.get) is None
    assert sources.fetch_source("record:SYN-DEV-2024-001", "1.0", w.get) is not None
    assert sources.fetch_source("record:SYN-DEV-2024-001", "9.9", w.get) is None
    assert sources.fetch_source("record:TPL-DEV-001", "1.0", w.get) is None              # a blank template is not a record
    assert sources.fetch_source("req:211.192", None, w.get) is not None
    assert sources.fetch_source("req:nope", None, w.get) is None
    assert sources.fetch_source("bogus:1", "1", w.get) is None
    with pytest.raises(sources.SourceFetchError):
        sources.fetch_source("library:7", "1", lambda *a, **k: Resp(503))


# ---------------------------------------------------------------------------- disposition (DI-03)
def test_user_action_is_recorded_for_accepted_edited_and_rejected(reg):
    out = ask(World(json.dumps({"statements": [GOOD["statements"][0], {"text": "uncited", "kind": "other", "citations": []}],
                                "no_support_found": False, "gaps": []})), reg)
    cid = out["call_id"]
    r1 = assist.record_disposition(cid, "accepted", "qa-1", reg)
    assert r1["statements_flagged_at_decision"] == 1 and "still flagged" in r1["note"]
    r2 = assist.record_disposition(cid, "edited", "steward-b", reg, "My corrected text")
    r3 = assist.record_disposition(cid, "rejected", "steward-a", reg)
    assert r2["edited_text"] == "My corrected text" and r3["edited_text"] is None
    entries = reg.audit_find(f"ai:{cid}")
    assert [(e["action"], e["actor"]) for e in entries] == [("ai.call", "steward-a"), ("ai.disposition", "qa-1"),
                                                            ("ai.disposition", "steward-b"), ("ai.disposition", "steward-a")]
    assert [json.loads(e["detail"])["action"] for e in entries[1:]] == ["accepted", "edited", "rejected"]
    assert json.loads(entries[0]["detail"])["output"]["statements"][0]["verdict"] == "verified"   # original untouched
    assert reg.verify_audit()


def test_disposition_validation(reg):
    cid = ask(World(), reg)["call_id"]
    for bad in ("approved", "", "ACCEPTED"):
        with pytest.raises(assist.AssistError) as e:
            assist.record_disposition(cid, bad, "qa-1", reg)
        assert e.value.status == 400
    with pytest.raises(assist.AssistError) as e:
        assist.record_disposition(cid, "edited", "qa-1", reg, "  ")
    assert e.value.status == 400
    with pytest.raises(assist.AssistError) as e:
        assist.record_disposition("deadbeef", "accepted", "qa-1", reg)
    assert e.value.status == 404


# ---------------------------------------------------------------------------- HTTP routes
@pytest.fixture
def client(tmp_path, monkeypatch):
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    w = World()
    monkeypatch.setattr(allowlist, "service_get", w.get)
    monkeypatch.setattr(allowlist, "service_post", lambda service, path, json, **kw: w.post(service, path, json, **kw))
    yield TestClient(app), reg, w
    app.dependency_overrides.clear()


def test_assist_routes_roles_and_flow(client):
    c, reg, w = client
    body = {"query": "Late deviation report: what is required?"}
    assert c.post("/assist/ask", json=body).status_code == 401
    assert c.post("/assist/ask", json=body, headers={"X-User": "viewer-1"}).status_code == 403
    assert c.post("/assist/ask", json={"query": " "}, headers={"X-User": "steward-a"}).status_code == 400
    r = c.post("/assist/ask", json=body, headers={"X-User": "steward-a"})
    assert r.status_code == 200 and r.json()["statements"][0]["verdict"] == "verified"
    cid = r.json()["call_id"]
    assert c.post(f"/assist/{cid}/disposition", json={"action": "accepted"}, headers={"X-User": "viewer-1"}).status_code == 403
    ok = c.post(f"/assist/{cid}/disposition", json={"action": "edited", "edited_text": "x"}, headers={"X-User": "qa-1"})
    assert ok.status_code == 200 and ok.json()["user"] == "qa-1"
    assert c.post("/assist/nope/disposition", json={"action": "accepted"}, headers={"X-User": "qa-1"}).status_code == 404
    cfg = c.get("/assist/config", headers={"X-User": "viewer-1"}).json()
    assert cfg["model"] == "qwen2.5:14b-instruct" and cfg["config_version"] == "1.4.0"
    assert reg.verify_audit()


def test_assist_route_maps_failures(client):
    c, reg, w = client
    w.models = []
    r = c.post("/assist/ask", json={"query": "anything"}, headers={"X-User": "steward-a"})
    assert r.status_code == 503 and "ollama pull" in r.json()["detail"]


def test_prompt_format_is_a_change_controlled_config_item(cfgdir, reg):
    """The locator format change (CC-SYN-0002) only takes effect through approval; tampering with it blocks calls."""
    c = ai_config.load_approved()
    assert c.version == "1.4.0" and c.prompt["passage_template"] == "[locator: {locator}] {text}"
    p = cfgdir / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["prompt"]["passage_template"] = "({locator}) {text}"      # revert the format without change control
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigNotApproved):
        ai_config.load_approved()
    cfg["prompt"]["passage_template"] = "no placeholders"          # even if approved, a broken template is refused
    cfg["config_version"] = "1.1.1"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    ai_config.approve(["steward-b", "qa-1"], "CC-X")
    with pytest.raises(ConfigNotApproved, match="passage_template"):
        ai_config.load_approved()
