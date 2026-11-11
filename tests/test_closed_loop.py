"""Closed-loop rule: only the gateway may use networking libraries.
Stdlib only, so it runs under any interpreter."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_SERVICES = ["library", "knowledge", "mock-eqms", "workflow"]
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".pytest_cache", "dist"}

PY_NET = {"requests", "httpx", "aiohttp", "urllib3", "urllib", "http", "httplib2",
          "pycurl", "socket", "websockets", "websocket", "grpc", "ftplib",
          "smtplib", "telnetlib", "xmlrpc", "treq", "httpcore"}
PY_NET_PKGS = PY_NET | {"python-socketio", "grpcio", "websocket-client"}
JS_NET_PKGS = {"axios", "node-fetch", "ky", "superagent", "got", "undici",
               "cross-fetch", "isomorphic-fetch", "socket.io-client", "ws", "request"}
JS_NET_CALLS = re.compile(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon")


def _files(base: Path, suffixes):
    for p in base.rglob("*"):
        if p.is_file() and p.suffix in suffixes and not (set(p.parts) & SKIP_DIRS):
            yield p


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def _req_names(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            yield re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0].lower().replace("_", "-")


def test_python_services_have_no_network_imports():
    bad = []
    for svc in PY_SERVICES:
        for f in _files(ROOT / svc, {".py"}):
            for mod in _imports(f):
                if mod.split(".")[0] in PY_NET:
                    bad.append(f"{f.relative_to(ROOT)} imports {mod}")
    assert not bad, bad


def test_python_services_do_not_list_network_deps():
    bad = []
    for svc in PY_SERVICES:
        req = ROOT / svc / "requirements.txt"
        assert req.exists(), req
        for name in _req_names(req):
            if name in PY_NET_PKGS:
                bad.append(f"{svc}/requirements.txt lists {name}")
    assert not bad, bad


def test_web_has_no_network_libs_or_calls():
    web = ROOT / "web"
    pkg = json.loads((web / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    bad = [f"package.json lists {d}" for d in deps if d in JS_NET_PKGS]
    for f in _files(web / "src", {".js", ".jsx", ".ts", ".tsx"}):
        text = f.read_text(encoding="utf-8")
        if JS_NET_CALLS.search(text):
            bad.append(f"{f.relative_to(ROOT)} makes a network call")
        for m in re.finditer(r"(?:from|require\()\s*['\"]([^'\"]+)['\"]", text):
            if m.group(1) in JS_NET_PKGS:
                bad.append(f"{f.relative_to(ROOT)} imports {m.group(1)}")
    assert not bad, bad


def test_gateway_is_the_only_one_with_http_client():
    assert "httpx" in set(_req_names(ROOT / "gateway" / "requirements.txt"))


# ---- Phase 2: knowledge and mock-eqms stay passive (no networking, minimal dependencies) ----
APPROVED_PASSIVE_DEPS = {"fastapi", "uvicorn", "pytest"}


def test_knowledge_and_mock_eqms_depend_only_on_approved_packages():
    for svc in ("knowledge", "mock-eqms"):
        names = set(_req_names(ROOT / svc / "requirements.txt"))
        assert names <= APPROVED_PASSIVE_DEPS, f"{svc} lists unapproved packages: {names - APPROVED_PASSIVE_DEPS}"
        assert not names & PY_NET_PKGS


def test_knowledge_and_mock_eqms_source_has_no_network_imports_or_url_calls():
    bad = []
    for svc in ("knowledge", "mock-eqms"):
        for f in _files(ROOT / svc, {".py"}):
            for mod in _imports(f):
                if mod.split(".")[0] in PY_NET:
                    bad.append(f"{f.relative_to(ROOT)} imports {mod}")
            if re.search(r"\burlopen\(|\bhttp\.client\b|(?:fastapi|starlette)\.testclient|import\s+TestClient",
                         f.read_text(encoding="utf-8")):
                bad.append(f"{f.relative_to(ROOT)} makes or simulates a network call")
    assert not bad, bad


# ---- Phase 3: the local LLM is internal to the gateway; nobody else may reach it ----
def test_only_the_gateway_knows_the_local_llm_address():
    bad = []
    for svc in ("library", "knowledge", "mock-eqms", "workflow"):
        for f in _files(ROOT / svc, {".py", ".json", ".md", ".txt", ".toml", ".ini"}):
            if f.suffix == ".md" and "data" in f.parts:
                continue  # synthetic SOP text, not code
            t = f.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"11434|OLLAMA_URL|/api/chat|/api/tags", t):
                bad.append(f"{f.relative_to(ROOT)} mentions the local LLM endpoint")
    assert not bad, bad


def test_workflow_is_passive_no_network_and_minimal_dependencies():
    names = set(_req_names(ROOT / "workflow" / "requirements.txt"))
    assert names <= APPROVED_PASSIVE_DEPS, names - APPROVED_PASSIVE_DEPS
    for f in _files(ROOT / "workflow", {".py"}):
        for mod in _imports(f):
            assert mod.split(".")[0] not in PY_NET, f"{f.relative_to(ROOT)} imports {mod}"
        assert "ollama" not in f.read_text(encoding="utf-8").lower() or f.parts[-2] == "tests", f
