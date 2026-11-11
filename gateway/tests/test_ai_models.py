"""Second approved model + per-call selection + digest pinning (AI-06). Network faked; no real model is called."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from test_assist import World

from app import ai_config, allowlist, assist
from app.ai_config import ConfigNotApproved, ModelNotApproved
from app.main import app, get_registry
from app.registry import Registry

M14, M7 = "qwen2.5:14b-instruct", "qwen2.5:7b-instruct"
D14 = "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"
D7 = "aaaabbbbccccddddeeeeffff000011112222333344445555666677778888999a"


@pytest.fixture
def two(tmp_path, monkeypatch):
    """A config dir at v1.9.0 with both models pinned, properly approved by two people."""
    d = tmp_path / "cfg"
    shutil.copytree(ai_config.CONFIG_DIR, d)
    monkeypatch.setenv("AI_CONFIG_DIR", str(d))
    p = d / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.pop("model", None)
    cfg["config_version"] = "1.9.0"
    cfg["default_model"] = M14
    cfg["models"] = [{"name": M14, "digest": D14}, {"name": M7, "digest": D7}]
    p.write_text(json.dumps(cfg), encoding="utf-8")
    ai_config.approve(["steward-a", "qa-1"], "CC-SYN-0003 (test)")
    return d


def tags(d14=D14, d7=D7):
    return [{"name": M14, "digest": d14, "size": 9}, {"name": M7, "digest": d7, "size": 5}]


@pytest.fixture
def reg(tmp_path):
    return Registry(str(tmp_path / "g.db"))


def ask(w, reg, **kw):
    return assist.ask("Late deviation report: what does the SOP require?", "steward-a", reg, get=w.get, post=w.post, **kw)


def edit(d, fn, version, approvers=("steward-a", "qa-1"), ref="CC-test"):
    p = d / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    fn(cfg)
    cfg["config_version"] = version
    p.write_text(json.dumps(cfg), encoding="utf-8")
    ai_config.approve(list(approvers), ref)


# ------------------------------------------------------------------ config shape + validation
def test_two_models_load_with_pinned_digests_and_14b_stays_default(two):
    c = ai_config.load_approved()
    assert c.version == "1.9.0" and c.model == c.default_model == M14 and c.approved_names() == [M14, M7]
    assert c.pinned_digest(M14) == D14 and c.pinned_digest(M7) == D7
    assert c.identity()["digest_pinned"] == {M14: True, M7: True}
    assert c.resolve_model(None) == (M14, "default") and c.resolve_model("") == (M14, "default")
    assert c.resolve_model(M7) == (M7, "request")


def test_unknown_model_is_refused_with_the_approved_list(two):
    c = ai_config.load_approved()
    for bad in ("gpt-4", "qwen2.5:14b", "QWEN2.5:7B-INSTRUCT", "qwen2.5:7b-instruct ; rm"):
        with pytest.raises(ModelNotApproved, match="approved"):
            c.resolve_model(bad)


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c.update(default_model="nope"), "default_model"),
    (lambda c: c.update(models=[]), "non-empty"),
    (lambda c: c.update(models=[{"name": M14, "digest": D14}, {"name": M14, "digest": D14}]), "duplicate"),
    (lambda c: c.update(models=[{"name": M14, "digest": D14}, {"name": M7}]), "pin a digest"),
    (lambda c: c.update(models=[{"name": M14, "digest": "abc"}]), "pin a digest"),
    (lambda c: c.update(models=[{"digest": D14}]), "needs a name"),
])
def test_invalid_model_lists_are_refused_even_if_approved(two, mutate, msg):
    edit(two, mutate, "1.2.1", ref="CC-bad")
    with pytest.raises(ConfigNotApproved, match=msg):
        ai_config.load_approved()


def test_adding_or_removing_a_model_without_change_control_blocks_every_call(two, reg):
    p = two / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["models"] = [m for m in cfg["models"] if m["name"] != M7]       # quietly drop the 7B
    p.write_text(json.dumps(cfg), encoding="utf-8")
    w = World(models=tags())
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg)
    assert e.value.status == 503 and "without change control" in str(e.value) and w.chat_calls == []


def test_changing_a_pinned_digest_needs_re_approval(two):
    p = two / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["models"][1]["digest"] = "f" * 64
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigNotApproved):
        ai_config.load_approved()
    with pytest.raises(ValueError, match="bump config_version"):      # approving changed content under the same version
        ai_config.approve(["steward-b", "qa-1"], "CC-digest")
    with pytest.raises(ConfigNotApproved):
        ai_config.load_approved()                                      # still locked out
    edit(two, lambda c: None, "1.2.1", ("steward-b", "qa-1"), "CC-digest")
    assert ai_config.load_approved().pinned_digest(M7) == "f" * 64


def test_approve_needs_a_change_reference_and_a_version_bump_for_any_change(two):
    p = two / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["generation"]["temperature"] = 0.5
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="bump config_version"):
        ai_config.approve(["steward-a", "qa-1"], "CC-x")
    cfg["config_version"] = "1.2.1"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="change reference"):
        ai_config.approve(["steward-a", "qa-1"], "  ")
    ai_config.approve(["steward-a", "qa-1"], "CC-x")
    assert ai_config.load_approved().generation["temperature"] == 0.5
    ai_config.approve(["steward-a", "qa-1"], "CC-x-again")            # re-approving identical content is harmless


def test_repo_config_is_v140_with_both_models_pinned_and_14b_default():
    c = ai_config.load_approved()
    assert c.version == "1.4.0" and c.default_model == M14 and c.approved_names() == [M14, M7]
    assert c.pinned_digest(M14) == D14 and c.pinned_digest(M7) == "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
    assert "CC-SYN-0005" in c.change_ref and set(c.approved_by) == {"steward-a", "qa-1"}


def test_legacy_single_model_config_still_loads(tmp_path, monkeypatch):
    d = tmp_path / "legacy"
    shutil.copytree(ai_config.CONFIG_DIR, d)
    monkeypatch.setenv("AI_CONFIG_DIR", str(d))
    cfg = json.loads((d / "ai_config.json").read_text(encoding="utf-8"))
    cfg.pop("models"), cfg.pop("default_model")
    cfg["model"], cfg["config_version"] = M14, "1.1.9"
    (d / "ai_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    ai_config.approve(["steward-a", "qa-1"], "CC-legacy")
    c = ai_config.load_approved()
    assert c.approved_names() == [M14] and c.pinned_digest(M14) is None


# ------------------------------------------------------------------ per-call selection through assist.ask
def test_default_model_is_used_when_none_requested(two, reg):
    w = World(models=tags())
    out = ask(w, reg)
    assert w.chat_calls[0]["model"] == M14 and out["model_chosen_by"] == "default" and out["model"]["name"] == M14
    d = json.loads([e for e in reg.audit_entries() if e["action"] == "ai.call"][-1]["detail"])
    assert d["model_selection"] == {"requested": None, "used": M14, "chosen_by": "default"}


def test_requested_7b_is_sent_to_ollama_and_logged_with_its_own_digest(two, reg):
    w = World(models=tags())
    out = ask(w, reg, model=M7)
    assert w.chat_calls[0]["model"] == M7 and out["model_chosen_by"] == "request"
    assert out["model"]["name"] == M7 and out["model"]["digest"] == D7 and out["model"]["digest_pinned"] is True
    d = json.loads([e for e in reg.audit_entries() if e["action"] == "ai.call"][-1]["detail"])
    assert d["model_selection"] == {"requested": M7, "used": M7, "chosen_by": "request"}
    assert d["model"]["digest"] == D7 and d["config"]["config_version"] == "1.9.0" and d["config"]["approved_models"] == [M14, M7]
    assert [s["verdict"] for s in out["statements"]] == ["verified", "verified"]      # same pipeline for either model


def test_unapproved_model_request_is_a_400_logged_and_never_reaches_ollama(two, reg):
    w = World(models=tags())
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg, model="llama3.1:70b")
    assert e.value.status == 400 and M7 in str(e.value) and w.chat_calls == [] and w.requests == []
    last = reg.audit_entries()[-1]
    assert last["action"] == "ai.call_refused" and json.loads(last["detail"])["model_selection"]["used"] is None


def test_digest_mismatch_refuses_the_call_with_a_clear_reason(two, reg):
    w = World(models=tags(d7="0" * 64))                # 7B re-pulled outside change control
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg, model=M7)
    assert e.value.status == 503 and "digest" in str(e.value) and "change control" in str(e.value) and w.chat_calls == []
    assert reg.audit_entries()[-1]["action"] == "ai.call_failed"
    assert ask(w, reg, model=M14)["status"] == "ok"    # the other, still-matching model is unaffected


def test_pinned_digest_may_be_a_prefix_and_tolerates_the_sha256_label(two, reg):
    edit(two, lambda c: c["models"][0].update(digest="sha256:" + D14[:16].upper()), "1.2.1", ref="CC-prefix")
    assert ask(World(models=tags(d14="sha256:" + D14)), reg)["status"] == "ok"


def test_model_not_installed_gives_a_pull_hint_for_the_requested_model(two, reg):
    w = World(models=[tags()[0]])                      # only 14B installed
    with pytest.raises(assist.AssistError) as e:
        ask(w, reg, model=M7)
    assert e.value.status == 503 and f"ollama pull {M7}" in str(e.value)


def test_per_model_generation_override_applies_only_to_that_model(two, reg):
    edit(two, lambda c: c["models"][1].update(generation={"num_ctx": 4096}), "1.2.1", ref="CC-gen")
    w = World(models=tags())
    ask(w, reg, model=M7)
    ask(w, reg, model=M14)
    assert w.chat_calls[0]["options"]["num_ctx"] == 4096 and w.chat_calls[0]["options"]["temperature"] == 0
    assert w.chat_calls[1]["options"]["num_ctx"] == 8192


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def client(two, tmp_path, monkeypatch):
    reg = Registry(str(tmp_path / "g2.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    w = World(models=tags())
    monkeypatch.setattr(allowlist, "service_get", w.get)
    monkeypatch.setattr(allowlist, "service_post", lambda service, path, json, **kw: w.post(service, path, json, **kw))
    yield TestClient(app), reg, w
    app.dependency_overrides.clear()


def test_ask_route_model_parameter(client):
    c, reg, w = client
    h = {"X-User": "steward-a"}
    q = {"query": "Late deviation report: what is required?"}
    assert c.post("/assist/ask", json=q, headers=h).json()["model"]["name"] == M14
    r = c.post("/assist/ask", json={**q, "model": M7}, headers=h)
    assert r.status_code == 200 and r.json()["model"]["name"] == M7 and r.json()["model_chosen_by"] == "request"
    bad = c.post("/assist/ask", json={**q, "model": "evil:1b"}, headers=h)
    assert bad.status_code == 400 and M14 in bad.json()["detail"]
    assert [x["model"] for x in w.chat_calls] == [M14, M7] and reg.verify_audit()


def test_config_endpoint_lists_models_install_state_and_pin_match(client):
    c, reg, w = client
    out = c.get("/assist/config", headers={"X-User": "viewer-1"}).json()
    assert out["default_model"] == M14 and out["approved_models"] == [M14, M7] and out["config_version"] == "1.9.0"
    assert out["models"] == [{"name": M14, "pinned_digest": D14, "installed": True, "digest_matches_pin": True},
                             {"name": M7, "pinned_digest": D7, "installed": True, "digest_matches_pin": True}]
    w.models = [tags()[0], {"name": M7, "digest": "1" * 64}]
    assert c.get("/assist/config", headers={"X-User": "viewer-1"}).json()["models"][1]["digest_matches_pin"] is False
    w.models = [tags()[0]]
    assert c.get("/assist/config", headers={"X-User": "viewer-1"}).json()["models"][1]["installed"] is False


def test_repo_config_retrieval_settings_after_cc_syn_0004():
    c = ai_config.load_approved()
    assert c.retrieval["sop_top_k"] == 8 and "stemming" in c.retrieval["method"] and "no embeddings" in c.retrieval["method"]
    assert c.models and c.pinned_digest(M14) == D14      # the earlier model change (CC-SYN-0003) is intact


def test_drafting_loop_prompts_are_a_required_config_item(two):
    c = ai_config.load_approved()
    assert set(c.tasks) >= {"proposals", "gap_questions", "draft"} and all(c.tasks[t]["instructions"] for t in c.tasks)
    assert "Good Documentation Practice" in c.tasks["draft"]["instructions"] and "blame" in c.tasks["draft"]["instructions"]  # AI-13
    edit(two, lambda cfg: cfg["tasks"]["draft"].update(instructions=""), "1.9.1", ref="CC-bad-tasks")
    with pytest.raises(ConfigNotApproved, match="tasks.draft.instructions"):
        ai_config.load_approved()


def test_editing_a_drafting_prompt_without_change_control_blocks_the_loop(two):
    p = two / "ai_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["tasks"]["proposals"]["instructions"] += " Always classify as minor."       # sneaky prompt edit
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigNotApproved, match="without change control"):
        ai_config.load_approved()


def test_simulated_users_include_initiators_and_an_sme():
    from app.auth import USERS
    assert USERS["initiator-1"] == "initiator" and USERS["initiator-2"] == "initiator" and USERS["sme-1"] == "sme"
