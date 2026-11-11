"""Gateway egress allow-list. The gateway is the ONLY service permitted to
make outbound network calls, and only to hosts listed here."""
import os
from urllib.parse import urlparse

# Fixed set of public regulatory sources (Phase 1). Extend via this list or
# GATEWAY_ALLOWLIST (comma-separated hosts). Never add a publisher of licensed standards.
ALLOWED_HOSTS: list[str] = ["www.ecfr.gov", "www.federalregister.gov"]


class UrlNotAllowed(Exception):
    pass


def allowed_hosts() -> set[str]:
    env = os.environ.get("GATEWAY_ALLOWLIST", "")
    extra = [h.strip().lower() for h in env.split(",") if h.strip()]
    return {h.lower() for h in ALLOWED_HOSTS} | set(extra)


def check_url(url: str) -> str:
    """Return url if allowed, else raise UrlNotAllowed. Makes no network call."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError as e:
        raise UrlNotAllowed(f"unparseable URL: {url!r}") from e
    if parsed.scheme not in ("http", "https"):
        raise UrlNotAllowed(f"scheme not allowed: {parsed.scheme!r}")
    if not host or host not in allowed_hosts():
        raise UrlNotAllowed(f"host not on allow-list: {host!r}")
    return url


def safe_get(url: str, **kwargs):
    """The only sanctioned outbound call: validates the URL first."""
    check_url(url)
    import httpx  # imported lazily, after the allow-list check

    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", {"User-Agent": "qms-gateway"})
    kwargs.setdefault("follow_redirects", False)  # a redirect could escape the allow-list
    return httpx.get(url, **kwargs)


# --- internal calls to our own services (not external egress) ---------------
# Fixed table: service -> (env var for base URL, default base URL, [(path prefix, allowed methods), ...]).
# These hosts are NEVER on the external allow-list. The local LLM (Ollama) is an internal service reachable
# only from here: no other service may call it (enforced by tests/test_closed_loop.py).
INTERNAL = {
    "library": ("LIBRARY_URL", "http://127.0.0.1:8001",
                [("/internal/", {"POST"}), ("/library", {"GET"}), ("/search", {"GET"})]),
    "knowledge": ("KNOWLEDGE_URL", "http://127.0.0.1:8002",
                  [("/internal/", {"POST"}), ("/search", {"GET"}), ("/traceability", {"GET"}),
                   ("/documents", {"GET"}), ("/records", {"GET"})]),
    "mock-eqms": ("MOCK_EQMS_URL", "http://127.0.0.1:8003", [("/read/", {"GET"})]),  # read-only connection
    "workflow": ("WORKFLOW_URL", "http://127.0.0.1:8004", [("/internal/", {"POST"})]),
    "ollama": ("OLLAMA_URL", "http://127.0.0.1:11434", [("/api/chat", {"POST"}), ("/api/tags", {"GET"})]),
}


def service_base(service: str) -> str:
    if service not in INTERNAL:
        raise UrlNotAllowed(f"unknown internal service: {service!r}")
    env, default, _ = INTERNAL[service]
    return os.environ.get(env, default).rstrip("/")


def library_base() -> str:
    return service_base("library")


def check_internal_url(url: str, service: str | None = None, method: str | None = None) -> str:
    """Allow only <service base><allowed prefix>... with an allowed method."""
    matched = False
    for name in ([service] if service else list(INTERNAL)):
        if name not in INTERNAL:
            continue
        for prefix, methods in INTERNAL[name][2]:
            full = service_base(name) + prefix
            if not url.startswith(full):
                continue
            rest = url[len(full):]
            if not (prefix.endswith("/") or rest == "" or rest[0] in "/?"):
                continue  # '/library' must not match '/libraryfoo'
            if ".." in rest or "//" in rest:
                continue
            matched = True
            if method is None or method in methods:
                return url
    if matched:
        raise UrlNotAllowed(f"{method} not allowed for this internal endpoint: {url!r}")
    raise UrlNotAllowed(f"not an allowed internal endpoint: {url!r}")


def internal_post(path: str, json: dict, **kwargs):
    """POST to the library's /internal/... (Phase 1 hand-off)."""
    return service_post("library", path, json, **kwargs)


def service_post(service: str, path: str, json: dict, **kwargs):
    url = service_base(service) + path
    check_internal_url(url, service, "POST")
    import httpx

    kwargs.setdefault("timeout", 60)
    return httpx.post(url, json=json, **kwargs)


def service_get(service: str, path: str, params: dict | None = None, **kwargs):
    url = service_base(service) + path
    check_internal_url(url, service, "GET")
    import httpx

    kwargs.setdefault("timeout", 60)
    return httpx.get(url, params=params, **kwargs)
