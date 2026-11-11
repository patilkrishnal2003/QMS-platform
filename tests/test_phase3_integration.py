"""Phase 3 end to end over localhost: REAL library, knowledge, mock-eqms and workflow services, the real
gateway orchestration, and a stand-in Ollama (a tiny stdlib HTTP server) that returns scripted model output.
No internet and no real model needed. A live test against the real model is at the bottom (skipped until
the pinned model is installed)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))
LIB_LINES = json.loads((ROOT / "tests" / "fixtures" / "ecfr_211_22.json").read_text(encoding="utf-8"))["lines"]


def _port() -> int:
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
                            cwd=str(ROOT / service), env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc, f"http://127.0.0.1:{port}"
        except httpx.HTTPError:
            time.sleep(0.25)
    proc.kill()
    pytest.fail(f"{service} did not start")


class FakeOllama:
    """Stands in for http://127.0.0.1:11434 (only /api/tags and /api/chat)."""

    def __init__(self, reply_fn):
        self.requests, outer = [], self

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
                outer.requests.append(("GET", self.path, None))
                self._send({"models": [{"name": "qwen2.5:14b-instruct", "digest": "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6", "size": 1}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, body))
                self._send({"model": body["model"], "done": True, "eval_count": 5, "prompt_eval_count": 9,
                            "message": {"role": "assistant", "content": reply_fn(body)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def chats(self):
        return [r for r in self.requests if r[0] == "POST"]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stack(tmp_path, monkeypatch):
    procs = {}
    urls = {}
    for name, env_key in (("library", "LIBRARY_DB"), ("knowledge", "KNOWLEDGE_DB"), ("mock-eqms", "EQMS_DB")):
        procs[name], urls[name] = _spawn(name, {env_key: str(tmp_path / f"{name}.db")})
    procs["workflow"], urls["workflow"] = _spawn("workflow", {"WORKFLOW_DB": str(tmp_path / "workflow.db")})
    for env, name in (("LIBRARY_URL", "library"), ("KNOWLEDGE_URL", "knowledge"), ("MOCK_EQMS_URL", "mock-eqms"),
                      ("WORKFLOW_URL", "workflow")):
        monkeypatch.setenv(env, urls[name])
    yield urls
    for p in procs.values():
        p.terminate()
        p.wait(10)


def test_assistant_end_to_end_with_real_services_and_a_scripted_model(stack, tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from app.main import app, get_registry
    from app.registry import Registry

    # --- load the stores: an accepted library item (real 21 CFR 211.22 text) + eQMS records synced into knowledge
    lib = stack["library"]
    r = httpx.post(f"{lib}/internal/ingest", json={
        "source_url": "https://example.test/211-22", "title": "21 CFR 211.22", "text": "\n".join(LIB_LINES),
        "retrieved_at": "2026-11-11T00:00:00+00:00", "document_date": "2026-11-01", "version_or_docket": "21 CFR 211.22",
        "status": "final", "legal_weight": "regulation"})
    item_id = r.json()["item_id"]
    assert httpx.post(f"{lib}/queue/{item_id}/accept", headers={"X-User": "steward-a"}, json={}).status_code == 200

    prompts = []

    def reply(req):  # what the "model" says: 2 good statements, 1 fabricated section, 1 uncited claim
        prompts.append(req["messages"][1]["content"])                           # asserted after the call, not in the handler
        return json.dumps({"statements": [
            {"text": "The originator must record the event in the deviation record within 24 hours of detection.", "kind": "procedure",
             "citations": [{"source_id": "sop:SOP-DEV-001", "version": "3.0", "locator": "5.1", "quote": "within 24 hours of detection"}]},
            {"text": "The quality control unit has the responsibility and authority to approve or reject all components.", "kind": "requirement",
             "citations": [{"source_id": f"library:{item_id}", "version": "1", "locator": "lines:2",
                            "quote": "responsibility and authority to approve or reject all components"}]},
            {"text": "Every deviation must be approved by the plant director.", "kind": "procedure",
             "citations": [{"source_id": "sop:SOP-DEV-001", "version": "3.0", "locator": "5.9", "quote": "approved by the plant director"}]},
            {"text": "Deviations must always be closed within seven days.", "kind": "procedure", "citations": []}],
            "no_support_found": False, "gaps": []})

    fake = FakeOllama(reply)
    monkeypatch.setenv("OLLAMA_URL", fake.url)
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    try:
        c = TestClient(app)
        a = {"X-User": "steward-a"}
        assert c.post("/sync/eqms-records", headers=a).json()["indexed"] == 20

        out = c.post("/assist/ask", headers=a, json={"query": "A tablet press compression sensor drifted and the "
                                                              "originator reported the deviation late. Must the quality "
                                                              "control unit approve or reject the components?"})
        assert out.status_code == 200, out.text
        o = out.json()
        assert [s["verdict"] for s in o["statements"]] == ["verified", "verified", "invalid_citation", "unsupported"]
        assert [s["usable"] for s in o["statements"]] == [True, True, False, False]
        assert o["summary"]["flagged"] == 2 and "2 of 4 statements are flagged" in o["notice"]
        assert o["statements"][0]["citations"][0]["link"].endswith("/documents/SOP-DEV-001?version=3.0#5.1")
        ids = o["retrieved_source_ids"]
        assert "sop:SOP-DEV-001@3.0" in ids and f"library:{item_id}@1" in ids
        assert any(i.startswith("record:SYN-DEV-") and i.endswith("@1.0") for i in ids)      # similar past deviation, cited by id+version
        assert any(i.startswith("req:") for i in ids)                                        # suggested requirement links
        assert o["library_version"].startswith("lib-1-") and o["model"]["digest"] == "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"

        assert "SOP-DEV-001" in prompts[0] and "quality control unit" in prompts[0].lower()   # real sources reached the prompt
        # the model was called once, with the approved config's settings
        (_, path, body), = fake.chats
        assert path == "/api/chat" and body["model"] == "qwen2.5:14b-instruct" and body["options"]["temperature"] == 0
        assert body["format"]["required"] == ["statements", "no_support_found", "gaps"]

        # DI-03 log entry
        e = [x for x in reg.audit_entries() if x["action"] == "ai.call"][-1]
        d = json.loads(e["detail"])
        assert e["actor"] == "steward-a" and e["ts"] and d["input"]["query"].startswith("A tablet press")
        assert d["retrieved_source_ids"] == ids and d["model"]["digest"] == "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"
        assert d["config"]["config_version"] == "1.4.0" and d["library_version"] == o["library_version"]
        assert d["raw_output"] and d["output"]["summary"]["verified"] == 2

        # what the user did with it
        assert c.post(f"/assist/{o['call_id']}/disposition", headers={"X-User": "qa-1"},
                      json={"action": "rejected"}).status_code == 200

        # nothing relevant anywhere: say so, and never call the model
        before = len(fake.chats)
        none = c.post("/assist/ask", headers=a, json={"query": "zzzqqq wwwxxx"}).json()
        assert none["status"] == "no_support_found" and none["statements"] == [] and len(fake.chats) == before
        assert reg.verify_audit()
        assert [x["action"] for x in reg.audit_entries() if x["action"].startswith("ai.")] == ["ai.call", "ai.disposition", "ai.call"]
    finally:
        app.dependency_overrides.clear()
        fake.close()


# ---------------------------------------------------------------------------- live model (skipped until installed)
# Environment knobs (read by this test only):
#   LIVE_MODELS="qwen2.5:14b-instruct,qwen2.5:7b-instruct"   models to run, in this order (default: the config default)
#   LIVE_RUNS=3                                               runs per model on the SAME question (default 1)
def _installed_names() -> list:
    try:
        import httpx

        return [m["name"] for m in httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json().get("models", [])]
    except Exception:
        return []


LIVE_QUESTION = ("A deviation was reported 3 days after it was discovered. "
                 "What does our SOP require for reporting time?")


@pytest.mark.llm
def test_live_model_returns_schema_conformant_output_and_verifier_runs(stack, tmp_path, monkeypatch):
    """Needs the real approved model(s) installed. Checks plumbing and structure, NOT answer quality.
    With LIVE_MODELS / LIVE_RUNS it becomes the speed-vs-quality comparison (same question, wall-clock per run)."""
    import time as _t

    from app import ai_config

    cfg = ai_config.load_approved()
    wanted = [m.strip() for m in os.environ.get("LIVE_MODELS", "").split(",") if m.strip()] or [cfg.default_model]
    runs = int(os.environ.get("LIVE_RUNS", "1"))
    have = _installed_names()
    missing = [m for m in wanted if m not in have]
    if missing:
        pytest.skip(f"not installed in the local Ollama: {missing}")
    import httpx
    from fastapi.testclient import TestClient

    from app.main import app, get_registry
    from app.registry import Registry

    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    reg = Registry(str(tmp_path / "g.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    table = []
    try:
        c = TestClient(app, raise_server_exceptions=True)
        a = {"X-User": "steward-a"}
        assert c.post("/sync/eqms-records", headers=a).json()["indexed"] == 20
        for model in wanted:
            for run in range(1, runs + 1):
                body = {"query": LIVE_QUESTION}
                if len(wanted) > 1 or model != cfg.default_model:
                    body["model"] = model          # explicit selection (must be an approved model)
                t0 = _t.perf_counter()
                r = c.post("/assist/ask", headers=a, json=body)
                wall = _t.perf_counter() - t0
                assert r.status_code == 200, r.text
                o = r.json()
                assert o["status"] in ("ok", "model_output_unparseable", "no_support_found")
                for st in o.get("statements", []):
                    assert st["verdict"] in {"verified", "low_confidence", "mismatch", "invalid_citation", "unsupported"}
                d = json.loads([x for x in reg.audit_entries() if x["action"] == "ai.call"][-1]["detail"])
                assert d["model"]["name"] == model and d["model"]["digest"] and d["raw_output"] is not None
                mc = d["model_call"]
                ns = lambda v: round((v or 0) / 1e9, 1)
                table.append({"model": model, "run": run, "wall_s": round(wall, 1), "load_s": ns(mc.get("load_duration")),
                              "prompt_eval_s": ns(mc.get("prompt_eval_duration")), "gen_s": ns(mc.get("eval_duration")),
                              "out_tokens": mc.get("eval_count"), "prompt_tokens": mc.get("prompt_eval_count"),
                              "status": o["status"], "verdicts": [st["verdict"] for st in o.get("statements", [])],
                              "scores": [st["score"] for st in o.get("statements", [])]})
                print(f"\n===== {model}  run {run}/{runs}  wall-clock {wall:.1f}s  status={o['status']} =====")
                print("digest:", d["model"]["digest"], "| config:", d["config"]["config_version"], "| chosen_by:", o.get("model_chosen_by"))
                print("RAW MODEL OUTPUT:\n" + str(d["raw_output"]))
                for st in o.get("statements", []):
                    print("STATEMENT:", st["text"], "\n  verdict:", st["verdict"], "score:", st["score"], "reasons:", st["reasons"])
                    for ci in st["citations"]:
                        print("  citation:", json.dumps({k: ci[k] for k in ("source_id", "version", "locator", "quote", "status", "reason")}))
                print("notice:", o["notice"])
        print("\n==== SUMMARY (same question, " + str(runs) + " run(s) per model) ====")
        print(f"{'model':24} {'run':3} {'wall_s':>7} {'load_s':>7} {'prompt_s':>9} {'gen_s':>7} {'out_tok':>7}  verdicts")
        for t in table:
            print(f"{t['model']:24} {t['run']:<3} {t['wall_s']:>7} {t['load_s']:>7} {t['prompt_eval_s']:>9} {t['gen_s']:>7} {t['out_tokens']!s:>7}  {t['verdicts']} {t['scores']}")
    finally:
        app.dependency_overrides.clear()
