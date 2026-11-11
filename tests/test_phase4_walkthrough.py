"""Phase 4 end-to-end: ONE realistic synthetic deviation taken through the whole guided drafting loop with the REAL
library, knowledge, mock-eqms and workflow services over localhost, the real gateway, and a stand-in Ollama returning
scripted model output (no internet, no real model).

At every step the test asserts (a) what the system required of a human, (b) that skipping or auto-advancing is refused,
(c) the audit trail (DI-03): what the AI produced and what the person did with it.

Scenario (all fictional): tablet press TAB-PRESS-02 compression force drifted ~6% above set point during lot EXA-26-031."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))

D14 = "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"
DESC = ("Compression force on tablet press TAB-PRESS-02 drifted about 6% above the set point during the run of lot "
        "EXA-26-031; reject counts rose and the operator stopped the press.")
ACTION = "Press stopped; lot EXA-26-031 quarantined; rejects segregated."


# ------------------------------------------------------------------------------------------------ infrastructure
def _port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(service: str, env: dict):
    import httpx
    py = ROOT / service / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / service / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip(f"{service} venv not created")
    port = _port()
    proc = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT / service), env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc, f"http://127.0.0.1:{port}"
        except httpx.HTTPError:
            time.sleep(0.25)
    proc.kill()
    pytest.fail(f"{service} did not start")


class FakeOllama:
    """Stands in for 127.0.0.1:11434. `reply(task, request)` returns the scripted model output for that AI task."""

    def __init__(self, reply):
        self.calls, outer = [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send({"models": [{"name": "qwen2.5:14b-instruct", "digest": D14, "size": 1}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                user = body["messages"][1]["content"]
                task = ("proposals" if "NEW DEVIATION" in user else "gap" if "WHAT THE INITIATOR HAS SAID" in user
                        else "draft" if "DEVIATION DRAFT REQUEST" in user else "other")
                outer.calls.append({"task": task, "user": user, "body": body})
                self._send({"model": body["model"], "done": True, "eval_count": 7, "prompt_eval_count": 9,
                            "message": {"role": "assistant", "content": reply(task, outer)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, task):
        return sum(1 for c in self.calls if c["task"] == task)

    def close(self):
        self.server.shutdown()


def cite(sid, ver, loc, quote):
    return {"source_id": sid, "version": ver, "locator": loc, "quote": quote}


def _rev(fake, key):
    """The revision of an answer exactly as shown to the model in its prompt (a real model copies it from the header)."""
    import re
    m = re.search(rf"source_id=answer:WF-0001:{key} version=(\d+)", fake.calls[-1]["user"])
    return m.group(1) if m else "1"


def scripted(task, fake):
    """What the 'model' says. Deliberately includes one invented SOP, one fabricated citation and one blame sentence,
    so the walkthrough shows flagged content being handled by people."""
    if task == "proposals":
        return json.dumps({
            "event_type": {"value": "equipment_failure", "reasoning": "The description reports that compression force on the tablet press drifted about 6% above the set point.",
                           "citations": [cite("answer:WF-0001:description", "1", "value", "drifted about 6% above the set point")]},
            "regulations": [
                {"requirement_id": "211.192", "reasoning": "21 CFR 211.192 production record review is implemented by SOP-DEV-001 section 5.4.",
                 "citations": [cite("req:211.192", "", "SOP-DEV-001#5.4", "is implemented by SOP-DEV-001 section 5.4")]},
                {"requirement_id": "211.68", "reasoning": "21 CFR 211.68 automatic, mechanical, and electronic equipment is implemented by SOP-CHG-001 section 7.",
                 "citations": [cite("req:211.68", "", "SOP-CHG-001#7", "is implemented by SOP-CHG-001 section 7")]}],
            "sops": [
                {"sop_number": "SOP-DEV-001", "reasoning": "SOP-DEV-001 requires the originator to record the event within 24 hours of detection.",
                 "citations": [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")]},
                {"sop_number": "SOP-DEV-002", "reasoning": "SOP-DEV-002 sets the criteria for classifying deviations as minor, major or critical.",
                 "citations": [cite("sop:SOP-DEV-002", "2.0", "1", "sets the criteria for classifying deviations as minor, major or critical")]},
                {"sop_number": "SOP-QC-009", "reasoning": "A press-calibration SOP applies.", "citations": []}],           # invented SOP
            "classification": {"value": "major", "reasoning": "A potential impact on product quality is indicated, which is the major criterion.",
                               "citations": [cite("sop:SOP-DEV-002", "2.0", "3", "Major: potential impact on product quality or a repeat of a minor event")]}})
    if task == "gap":
        return json.dumps({"questions": [
            {"text": "Was the compression force sensor within its calibration interval when the drift occurred?",
             "why": "SOP-CHG-002 section 5 says changes to calibration or maintenance intervals require assessment, even when made to reduce workload.",
             "citations": [cite("sop:SOP-CHG-002", "1.0", "5", "require assessment even when made to reduce workload")]},
            {"text": "Who approved the shift schedule?", "why": "Shift schedules can affect deviations.",
             "citations": [cite("sop:SOP-HR-001", "1.0", "1", "shift schedule")]}],                                      # fabricated source
            "no_gaps": False})
    if task == "draft":
        return json.dumps({"sections": {
            "event_summary": [
                {"text": "Compression force on tablet press TAB-PRESS-02 drifted about 6% above the set point during the run of lot EXA-26-031.",
                 "citations": [cite("answer:WF-0001:description", "1", "value", "drifted about 6% above the set point")]},
                {"text": "The press was stopped and lot EXA-26-031 was quarantined.",
                 "citations": [cite("answer:WF-0001:immediate_action", _rev(fake, "immediate_action"), "value", "Press stopped; lot EXA-26-031 quarantined")]},
                {"text": "The operator was careless and probably ignored the drift alarm.",
                 "citations": [cite("answer:WF-0001:description", "1", "value", "the operator stopped the press")]}],
            "regulatory_and_procedural_context": [
                {"text": "SOP-DEV-001 requires the originator to record the event within 24 hours of detection.",
                 "citations": [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")]}],
            "precedent_review": [
                {"text": "In a similar past deviation, calibration interval extended without a change assessment.",
                 "citations": [cite("record:SYN-DEV-2024-001", "1.0", "root_cause", "Calibration interval extended without a change assessment")]}]},
            "no_support_found": False, "gaps": []})
    return "{}"


@pytest.fixture
def stack(tmp_path, monkeypatch):
    procs, urls = {}, {}
    for name, env_key in (("library", "LIBRARY_DB"), ("knowledge", "KNOWLEDGE_DB"), ("mock-eqms", "EQMS_DB"), ("workflow", "WORKFLOW_DB")):
        procs[name], urls[name] = _spawn(name, {env_key: str(tmp_path / f"{name}.db")})
    for env, name in (("LIBRARY_URL", "library"), ("KNOWLEDGE_URL", "knowledge"), ("MOCK_EQMS_URL", "mock-eqms"), ("WORKFLOW_URL", "workflow")):
        monkeypatch.setenv(env, urls[name])
    yield urls
    for p in procs.values():
        p.terminate()
        p.wait(10)


@pytest.fixture
def world(stack, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app, get_registry
    from app.registry import Registry

    fake = FakeOllama(scripted)
    monkeypatch.setenv("OLLAMA_URL", fake.url)
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    c = TestClient(app)
    assert c.post("/sync/eqms-records", headers={"X-User": "steward-a"}).json()["indexed"] == 20     # past records -> knowledge
    yield c, reg, fake
    app.dependency_overrides.clear()
    fake.close()


def H(user):
    return {"X-User": user}


OWN, SME, QA, OTHER, VIEW = "initiator-1", "sme-1", "qa-1", "initiator-2", "viewer-1"


def audit(reg, prefix="deviation."):
    return [(e["actor"], e["action"], json.loads(e["detail"])) for e in reg.audit_entries() if e["action"].startswith(prefix)]


# ================================================================================================ the walkthrough
def test_full_nine_step_walkthrough_nothing_advances_on_its_own(world):
    c, reg, fake = world
    D = "/deviations"

    def post(path, user, body=None, code=200):
        r = c.post(f"{D}{path}", json=body or {}, headers=H(user))
        assert r.status_code == code, (path, user, r.status_code, r.text)
        return r.json()

    def refuse(path, user, body=None, code=409, expect_code=None):
        r = c.post(f"{D}{path}", json=body or {}, headers=H(user))
        assert r.status_code == code, (path, user, r.status_code, r.text)
        out = r.json()
        out = out["detail"] if isinstance(out.get("detail"), dict) else out       # workflow refusals are nested under 'detail'
        if expect_code:
            assert out.get("code") == expect_code, (path, out)
        return out

    def get(path, user=OWN):
        r = c.get(f"{D}{path}", headers=H(user))
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    # ---- STEP 1 (GW-01): the initiator starts the record with a description and minimal fields
    assert c.post(D, json={"description": DESC}, headers=H(VIEW)).status_code == 403                   # a viewer cannot start a record
    assert c.post(D, json={"description": " "}, headers=H(OWN)).status_code == 422
    rec = post("", OWN, {"description": DESC, "site": "SITE-RB", "product": "Examplamol 500 mg tablets",
                         "equipment_id": "TAB-PRESS-02", "detected_date": "2026-11-10"}, 201)
    rid = rec["id"]
    assert rid == "WF-0001" and rec["status"] == "in_progress" and rec["step"] == "proposals" and rec["created_by"] == OWN
    assert set(rec["answers"]) == {"description", "site", "product", "equipment_id", "detected_date"}   # lot was not given: NOT filled in
    assert rec["decisions"] == {} and rec["next_required"] == ["request the AI proposals (GW-02)"]
    assert fake.calls == []                                                                             # creating a record calls no model

    # nothing may skip ahead from here
    refuse(f"/{rid}/advance", OWN, {"to": "questions"}, expect_code="guard")
    refuse(f"/{rid}/advance", OWN, {"to": "draft"}, expect_code="skip")
    refuse(f"/{rid}/questions/generate", OWN, expect_code="precheck")                                   # wrong step: refused BEFORE any model call
    refuse(f"/{rid}/draft/generate", OWN, expect_code="precheck")
    refuse(f"/{rid}/mark-ready", OWN, expect_code="not_ready")
    refuse(f"/{rid}/proposals/event_type/decide", OWN, {"action": "confirm"}, expect_code="step_order")
    assert fake.calls == []

    # ---- STEP 2 (GW-02): the AI proposes; the initiator must confirm or change EACH item
    assert c.post(f"{D}/{rid}/proposals/generate", json={}, headers=H(OTHER)).status_code == 403       # only the initiator
    prop = post(f"/{rid}/proposals/generate", OWN)
    assert fake.count("proposals") == 1 and prop["step"] == "proposals" and prop["decisions"] == {}     # shown, NOT decided
    items = prop["proposals"]["items"]
    ev = items["event_type"]["entries"][0]
    assert ev["value"] == "equipment_failure" and ev["verification"]["verdict"] == "verified" and ev["validity"]["ok"]
    assert ev["citations"][0]["link"].endswith("/deviations/WF-0001/answers/description#value")           # cites what the initiator wrote
    regs = items["regulations"]["entries"]
    assert [e["value"] for e in regs] == ["211.192", "211.68"] and all(e["verification"]["verdict"] == "verified" for e in regs)
    sops = {e["value"]: e for e in items["sops"]["entries"]}
    assert sops["SOP-DEV-001"]["verification"]["verdict"] == "verified" and sops["SOP-DEV-002"]["verification"]["verdict"] == "verified"
    assert not sops["SOP-QC-009"]["validity"]["ok"] and sops["SOP-QC-009"]["verification"]["verdict"] == "unsupported"   # invented: flagged, not hidden
    cls = items["classification"]["entries"][0]
    assert cls["value"] == "major" and cls["verification"]["verdict"] == "verified"
    assert cls["citations"][0]["link"].endswith("/documents/SOP-DEV-002?version=2.0#3")
    assert prop["next_required"] == [f"confirm or change the proposed {i}" for i in ("event_type", "regulations", "sops", "classification")]
    refuse(f"/{rid}/advance", OWN, {"to": "questions"}, expect_code="guard")                            # AI proposals do not advance anything
    for who in (OTHER, QA, SME):
        assert c.post(f"{D}/{rid}/proposals/event_type/decide", json={"action": "confirm"}, headers=H(who)).status_code == 403

    d = post(f"/{rid}/proposals/event_type/decide", OWN, {"action": "confirm"})
    assert d["decisions"]["event_type"]["value"] == "equipment_failure" and d["decisions"]["event_type"]["by"] == OWN and d["step"] == "proposals"
    assert "classification" not in d["decisions"]                                                       # confirming one item confirms only that one
    refuse(f"/{rid}/proposals/sops/decide", OWN, {"action": "confirm"}, expect_code="flag_needs_ack")  # the invented SOP blocks a blind confirm
    d = post(f"/{rid}/proposals/sops/decide", OWN, {"action": "change", "value": ["SOP-DEV-001", "SOP-DEV-002"],
                                                    "rationale": "SOP-QC-009 does not exist; dropped it."})
    assert d["decisions"]["applicable_sops"]["via"] == "ai_proposal_changed"
    post(f"/{rid}/proposals/regulations/decide", OWN, {"action": "confirm"})
    d = post(f"/{rid}/proposals/classification/decide", OWN, {"action": "confirm"})
    assert d["decisions"]["classification"]["value"] == "major" and d["decisions"]["classification"]["by"] == OWN
    assert d["step"] == "proposals" and d["next_required"] == ["advance to step 'questions'"]            # all decided, STILL in step 2

    # ---- STEP 3 (GW-03): guided questions from requirements, SOP/form fields and gaps; each says what it satisfies
    d = post(f"/{rid}/advance", OWN, {"to": "questions"})                                                # an explicit human action
    assert d["step"] == "questions"
    qs = {q["id"]: q for q in d["questions"]}
    assert qs["immediate_action"]["source"] == "regulatory" and {"type": "requirement", "ref": "211.100"} in qs["immediate_action"]["satisfies"]
    assert qs["reported_by"]["source"] == "sop_form" and "equipment_status" in qs                        # adapted to the confirmed event type
    assert all(q["why"] and q["satisfies"] for q in qs.values()) and "lot_disposition" not in qs
    refuse(f"/{rid}/advance", OWN, {"to": "checklist"}, expect_code="guard")                             # gap questions not yet generated/waived
    gap = post(f"/{rid}/questions/generate", OWN)
    assert fake.count("gap") == 1 and gap["step"] == "questions"
    g = {q["id"]: q for q in gap["questions"] if q["source"] == "gap"}
    assert g["g1"]["verification"]["verdict"] == "verified" and g["g1"]["satisfies"][0]["link"].endswith("#5")
    assert g["g2"]["verification"]["verdict"] == "invalid_citation"                                      # fabricated citation: shown, flagged
    refuse(f"/{rid}/questions/generate", OWN, expect_code="already_generated")
    refuse(f"/{rid}/questions/g2/dismiss", OWN, {"justification": ""}, 422)
    post(f"/{rid}/questions/g2/dismiss", OWN, {"justification": "The citation does not exist; not a real requirement."})

    # ---- STEP 4 (GW-04): answers, 'unknown', evidence, assignment, adaptation
    refuse(f"/{rid}/answers/root_cause", OWN, {"kind": "answer", "value": "x"}, expect_code="is_decision")
    refuse(f"/{rid}/answers/immediate_action", SME, {"kind": "answer", "value": "x"}, 403)              # not assigned
    post(f"/{rid}/assign/immediate_action", OWN, {"to_user": SME})
    refuse(f"/{rid}/assign/immediate_action", OWN, {"to_user": "nobody"}, 422)
    a = post(f"/{rid}/answers/immediate_action", SME, {"kind": "answer", "value": ACTION})
    assert a["answers"]["immediate_action"]["by"] == SME and a["step"] == "questions"
    post(f"/{rid}/evidence/immediate_action", SME, {"name": "press_display.jpg", "description": "photo of the force display"})
    q = next(x for x in get(f"/{rid}")["questions"] if x["id"] == "immediate_action")
    assert q["evidence"][0]["placeholder"] is True and q["evidence"][0]["by"] == SME and q["assigned_to"] == SME
    post(f"/{rid}/answers/reported_by", OWN, {"kind": "answer", "value": "A. Operator, shift 2"})
    post(f"/{rid}/answers/event_date", OWN, {"kind": "answer", "value": "2026-11-09"})
    post(f"/{rid}/answers/lot_number", OWN, {"kind": "answer", "value": "EXA-26-031"})
    post(f"/{rid}/answers/equipment_status", OWN, {"kind": "answer", "value": "Press tagged out pending sensor check."})
    post(f"/{rid}/answers/g1", OWN, {"kind": "unknown"})                                                 # "unknown" is a valid answer
    post(f"/{rid}/answers/recurrence_check", OWN, {"kind": "tbd", "note": "QA to check the deviation history"})
    post(f"/{rid}/answers/capa_decision", OWN, {"kind": "answer", "value": "CAPA to be raised for the calibration interval."})
    d = post(f"/{rid}/decisions/product_impact", OWN, {"value": "potential_impact", "rationale": "Weight and hardness not yet confirmed within limits."})
    ids = {q["id"] for q in d["questions"]}
    assert {"lot_disposition", "affected_lots"} <= ids                                                   # questions ADAPTED to the decision
    post(f"/{rid}/answers/lot_disposition", OWN, {"kind": "answer", "value": "Hold lot EXA-26-031 pending extended testing."})
    post(f"/{rid}/answers/affected_lots", OWN, {"kind": "answer", "value": "No other lots run on TAB-PRESS-02 since the last calibration check."})
    refuse(f"/{rid}/decisions/root_cause", OWN, {"value": "", "rationale": ""}, 422)                     # AI-05: a person must record the decision
    post(f"/{rid}/decisions/root_cause", OWN, {"tbd": True})
    post(f"/{rid}/decisions/reportability", QA, {"value": "not_reportable", "rationale": "No product reached distribution."})
    assert get(f"/{rid}")["decisions"]["reportability"]["by"] == QA

    # ---- STEP 5 (GW-05): the completeness checklist is shown before drafting
    d = post(f"/{rid}/advance", OWN, {"to": "checklist"})
    assert d["step"] == "checklist" and d["checklist_shown"]["by"] == OWN
    cl = get(f"/{rid}/checklist")
    st = {i["key"]: i["status"] for i in cl["items"]}
    assert st["root_cause"] == "tbd" and st["recurrence_check"] == "tbd" and st["g1"] == "tbd" and st["product_impact"] == "answered"
    assert "g2" not in st and cl["complete_for_draft"] and not cl["missing"]
    assert any(s["type"] == "requirement" for i in cl["items"] for s in i["satisfies"])                  # each element names what it satisfies
    assert [e[1] for e in audit(reg) if e[1] == "deviation.checklist_shown"]
    # missing items block drafting: demonstrate on a SECOND record in the same stack
    r2 = post("", OWN, {"description": "Something else happened."}, 201)["id"]
    refuse(f"/{r2}/advance", OWN, {"to": "draft"}, expect_code="skip")

    # ---- STEP 6 (GW-06/DI-05/AI-13): the draft; AI text is marked, verified, and stays pending until a person decides
    d = post(f"/{rid}/advance", OWN, {"to": "draft"})
    assert d["step"] == "draft" and d["draft"] is None and d["next_required"] == ["generate the draft"]
    assert fake.count("draft") == 0                                                                     # entering the step did not call the model
    d = post(f"/{rid}/draft/generate", OWN)
    assert fake.count("draft") == 1 and d["draft"]["version"] == 1 and d["draft"]["pending_ai_statements"] == 5 and d["status"] == "in_progress"
    dr = get(f"/{rid}/draft")
    assert "NOT part of the record" in dr["legend"]
    stmts = [s for sec in dr["sections"] for s in sec["statements"]]
    ai = [s for s in stmts if s["author"]["type"] == "ai"]
    human = [s for s in stmts if s["author"]["type"] == "human"]
    assert len(ai) == 5 and all(s["status"] == "pending" and s["author"]["model"] == "qwen2.5:14b-instruct" and s["author"]["call_id"] for s in ai)
    assert human and all(s["status"] == "human_entered" and s["sources"][0]["link"].startswith("/deviations/WF-0001/answers/") for s in human)
    by_text = {s["text"][:40]: s for s in ai}
    good = [s for s in ai if s["verification"]["verdict"] == "verified"]
    assert len(good) == 4 and all(s["sources"] and s["sources"][0]["link"] for s in good)
    careless = next(s for s in ai if "careless" in s["text"])
    assert careless["needs_ack"] and {f["type"] for f in careless["style_flags"]} == {"blame", "speculation"}   # AI-13 flagged
    rc = next(s for s in human if s["element"] == "root_cause")
    assert "to be determined" in rc["text"] and "not yet accepted" in rc["text"]                         # carried as TBD, never invented
    assert dr["ready_blockers"]
    # the draft is a draft: it cannot be marked ready, and the AI text is not accepted by being displayed
    e = refuse(f"/{rid}/mark-ready", OWN, expect_code="not_ready")
    req = e["required"]
    assert sum("decide AI-written statement" in r for r in req) == 5 and any("root_cause" in r for r in req) and any("recurrence_check" in r for r in req)
    assert c.post(f"{D}/{rid}/draft/generate", json={}, headers=H(OWN)).status_code == 409             # a current draft already exists

    # ---- STEP 7 (GW-09/AI-05): explicit human decisions on every AI statement and every TBD
    sid = careless["id"]
    refuse(f"/{rid}/draft/statements/{sid}", OWN, {"action": "accept"}, expect_code="flag_needs_ack")  # cannot accept flagged text silently
    for who in (QA, SME, OTHER):
        assert c.post(f"{D}/{rid}/draft/statements/{sid}", json={"action": "reject"}, headers=H(who)).status_code == 403
    post(f"/{rid}/draft/statements/{sid}", OWN, {"action": "reject"})                                    # the person removes the blame sentence
    second = next(s for s in ai if s["text"].startswith("The press was stopped"))
    post(f"/{rid}/draft/statements/{second['id']}", OWN, {"action": "edit", "text": "The press was stopped at 14:10 and lot EXA-26-031 was quarantined."})
    for s in ai:
        if s["id"] not in (sid, second["id"]):
            post(f"/{rid}/draft/statements/{s['id']}", OWN, {"action": "accept"})                         # verified ones: accept without ack
    dr = get(f"/{rid}/draft")
    stmts = {s["id"]: s for sec in dr["sections"] for s in sec["statements"]}
    edited = stmts[second["id"]]
    assert edited["status"] == "edited" and edited["author"] == {"type": "human", "user": OWN, "derived_from_ai": True}
    assert edited["ai_original"]["author"]["type"] == "ai"                                               # DI-05: the AI original is kept
    assert [r["id"] for r in dr["rejected"]] == [sid]                                                    # rejected text is listed, not hidden
    assert all(stmts[s["id"]]["author"]["type"] == "ai" and stmts[s["id"]]["status"] == "accepted"
               for s in ai if s["id"] not in (sid, second["id"]))                                       # accepted AI text stays the AI's
    e = refuse(f"/{rid}/mark-ready", OWN, expect_code="not_ready")
    assert len(e["required"]) == 3 and all("or accept it as 'to be determined at investigation'" in r for r in e["required"])
    assert [r.split("'")[1] for r in e["required"]] == ["root_cause", "recurrence_check", "g1"]
    refuse(f"/{rid}/tbd-accept/root_cause", OWN, {"justification": ""}, 422)
    refuse(f"/{rid}/tbd-accept/product_impact", OWN, {"justification": "x"}, expect_code="not_tbd")
    post(f"/{rid}/tbd-accept/root_cause", QA, {"justification": "Root cause will be established by the investigation team."})
    post(f"/{rid}/tbd-accept/recurrence_check", OWN, {"justification": "QA will review the deviation history during investigation."})
    post(f"/{rid}/tbd-accept/g1", OWN, {"justification": "Calibration records are with the maintenance team."})
    # a change after the draft makes it stale: nothing can be marked ready on stale text
    post(f"/{rid}/answers/immediate_action", SME, {"kind": "answer", "value": ACTION + " QA informed."})
    assert get(f"/{rid}")["draft"]["stale"] is True
    e = refuse(f"/{rid}/mark-ready", OWN, expect_code="not_ready")
    assert any("stale" in r for r in e["required"])
    refuse(f"/{rid}/draft/statements/{good[0]['id']}", OWN, {"action": "accept"}, 409)
    d = post(f"/{rid}/draft/generate", OWN)                                                              # regenerate = new AI call, new pending text
    assert fake.count("draft") == 2 and d["draft"]["version"] == 2 and d["draft"]["pending_ai_statements"] == 5 and not d["draft"]["stale"]
    dr = get(f"/{rid}/draft")
    for s in [s for sec in dr["sections"] for s in sec["statements"] if s["author"]["type"] == "ai"]:
        post(f"/{rid}/draft/statements/{s['id']}", OWN, {"action": "reject" if "careless" in s["text"] else "accept"})
    assert get(f"/{rid}")["next_required"] == ["mark the draft ready for approval"]
    assert get(f"/{rid}")["status"] == "in_progress"                                                    # everything done, STILL not ready

    # ---- STEP 8/9: only the initiator can mark ready; it is explicit, logged, and freezes the record
    for who in (QA, SME, OTHER):
        assert c.post(f"{D}/{rid}/mark-ready", headers=H(who)).status_code == 403
    done = post(f"/{rid}/mark-ready", OWN)
    assert done["status"] == "ready_for_approval" and done["step"] == "ready" and done["ready"]["by"] == OWN and done["ready"]["draft_version"] == 2
    refuse(f"/{rid}/answers/immediate_action", OWN, {"kind": "answer", "value": "late edit"}, expect_code="frozen")
    refuse(f"/{rid}/decisions/root_cause", OWN, {"value": "x", "rationale": "y"}, expect_code="frozen")
    refuse(f"/{rid}/draft/generate", OWN, expect_code="precheck")

    # ---- the audit trail: what the AI produced, what was shown, what each person did (DI-03)
    log = audit(reg)
    actions = [a for _, a, _ in log]
    assert reg.verify_audit() and all(actor != "system" for actor, _, _ in log)
    for needed in ("deviation.create", "deviation.proposals_generated", "deviation.proposal_decision", "deviation.advance",
                   "deviation.gap_questions_generated", "deviation.answer", "deviation.assign", "deviation.evidence",
                   "deviation.decision", "deviation.checklist_shown", "deviation.draft_generated", "deviation.statement_decision",
                   "deviation.tbd_accepted", "deviation.gap_question_dismissed", "deviation.mark_ready", "deviation.refused"):
        assert needed in actions, needed
    ai_calls = [d for _, a, d in log if a in ("deviation.proposals_generated", "deviation.gap_questions_generated", "deviation.draft_generated")]
    assert len(ai_calls) == 4                                                                           # proposals, gap, draft v1, draft v2
    for d in ai_calls:                                                                                  # DI-03 fields on every AI event
        assert d["input"]["query"] and d["prompt_messages"] and d["retrieved_source_ids"] and d["raw_output"] and d["output"]
        assert d["model"]["name"] == "qwen2.5:14b-instruct" and d["model"]["digest"] == D14 and d["config"]["config_version"] == "1.4.0"
        assert d["library_version"].startswith("lib-") and d["knowledge_snapshot"].startswith("kn-") and d["user"] == OWN and d["call_id"]
    assert ai_calls[0]["task"] == "proposals" and any(s.startswith("answer:WF-0001:description") for s in ai_calls[0]["retrieved_source_ids"])
    decisions = [(actor, d) for actor, a, d in log if a == "deviation.proposal_decision"]
    assert [d["request"]["item"] for _, d in decisions] == ["event_type", "sops", "regulations", "classification"] and all(actor == OWN for actor, _ in decisions)
    assert decisions[1][1]["request"]["ai_call_id"] == ai_calls[0]["call_id"]                            # linked to the AI output the person acted on
    sd = [(actor, d) for actor, a, d in log if a == "deviation.statement_decision"]
    assert {d["request"]["action"] for _, d in sd} == {"accept", "edit", "reject"} and all(actor == OWN for actor, _ in sd)
    assert ("qa-1", "deviation.tbd_accepted") in [(a, b) for a, b, _ in log]                              # who accepted TBD is logged
    refusals = [d for _, a, d in log if a == "deviation.refused"]
    assert len(refusals) >= 12 and all(r["reason"] and r["status"] in (403, 409, 422) for r in refusals)  # every blocked skip is on record
    assert {r["code"] for r in refusals} >= {"skip", "guard", "not_ready", "flag_needs_ack", "frozen", "step_order"}
    wf = c.get(f"{D}/{rid}/events", headers=H(OWN)).json()
    assert [e["action"] for e in wf][0] == "create" and wf[-1]["action"] == "mark_ready" and wf[-1]["actor"] == OWN


# ================================================================================================ failure modes
def test_ai_output_that_cannot_be_used_advances_nothing_and_is_logged(world, monkeypatch):
    c, reg, fake = world
    import types
    broken = FakeOllama(lambda task, f: "Sure! I think it is an equipment failure.")
    monkeypatch.setenv("OLLAMA_URL", broken.url)
    try:
        rid = c.post("/deviations", json={"description": DESC, "site": "SITE-RB"}, headers=H(OWN)).json()["id"]
        r = c.post(f"/deviations/{rid}/proposals/generate", json={}, headers=H(OWN))
        assert r.status_code == 502 and r.json()["detail"]["code"] == "unusable" and r.json()["detail"]["required"]
        rec = c.get(f"/deviations/{rid}", headers=H(OWN)).json()
        assert rec["step"] == "proposals" and rec["decisions"] == {} and not rec["proposals"]["generated"]    # nothing stored, nothing advanced
        assert "deviation.proposals_unusable" in [a for _, a, _ in audit(reg)]
        # the person can still proceed WITHOUT the AI: enter each proposal themselves is not possible before proposals exist,
        # so the record stays at step 2 until the AI works or the config/model is fixed - it never auto-completes.
    finally:
        broken.close()


def test_unapproved_model_and_missing_model_are_refused_without_touching_the_record(world, monkeypatch):
    c, reg, fake = world
    rid = c.post("/deviations", json={"description": DESC}, headers=H(OWN)).json()["id"]
    r = c.post(f"/deviations/{rid}/proposals/generate", json={"model": "gpt-4"}, headers=H(OWN))
    assert r.status_code == 400 and "approved" in str(r.json())
    assert fake.calls == [] and c.get(f"/deviations/{rid}", headers=H(OWN)).json()["step"] == "proposals"
    assert "deviation.proposals_failed" in [a for _, a, _ in audit(reg)]


def test_other_users_cannot_act_on_someone_elses_record(world):
    c, reg, fake = world
    rid = c.post("/deviations", json={"description": DESC}, headers=H(OWN)).json()["id"]
    for path, body in [(f"/deviations/{rid}/proposals/generate", {}), (f"/deviations/{rid}/advance", {"to": "questions"}),
                       (f"/deviations/{rid}/mark-ready", {}), (f"/deviations/{rid}/questions/waive", {"justification": "x"})]:
        r = c.post(path, json=body, headers=H(OTHER))
        assert r.status_code in (403, 409), (path, r.status_code)
    assert c.get(f"/deviations/{rid}", headers=H(VIEW)).status_code == 200                              # read-only visibility
    assert c.get(f"/deviations/{rid}") .status_code == 401
    assert fake.calls == []
    refused = [d for _, a, d in audit(reg) if a == "deviation.refused"]
    assert len(refused) >= 3
