import httpx
import pytest

from app import allowlist
from app.allowlist import UrlNotAllowed, check_url, safe_get


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_ALLOWLIST", raising=False)
    monkeypatch.setattr(allowlist, "ALLOWED_HOSTS", [])


def test_no_hosts_allowed_when_list_is_cleared():
    assert allowlist.allowed_hosts() == set()


def test_rejected_before_any_network_call(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    for url in ["https://example.com/x", "http://localhost:9/", "ftp://example.com", "not a url", ""]:
        with pytest.raises(UrlNotAllowed):
            safe_get(url)
    assert calls == []


def test_allowed_host_passes_check(monkeypatch):
    monkeypatch.setattr(allowlist, "ALLOWED_HOSTS", ["api.example.com"])
    assert check_url("https://api.example.com/v1") == "https://api.example.com/v1"
    with pytest.raises(UrlNotAllowed):
        check_url("https://evil.example.com/")
