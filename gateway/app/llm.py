"""Thin wrapper around the local Ollama server. Only the gateway makes this call (closed loop): Ollama is an
INTERNAL service (127.0.0.1:11434), reachable via allowlist.service_get/service_post and never on the external
allow-list. The model, its pinned digest and the generation settings come from the approved AI config (AI-06);
the caller can only choose among the approved models."""
from . import allowlist


class LlmError(Exception):
    pass


class ModelNotInstalled(LlmError):
    pass


class DigestMismatch(LlmError):
    """The installed model's digest differs from the digest pinned in the approved config."""


class OllamaClient:
    def __init__(self, cfg, get=None, post=None, model: str | None = None):
        self.cfg = cfg
        self.model = model or cfg.model
        self._get = get or allowlist.service_get
        self._post = post or allowlist.service_post

    def _generation(self) -> dict:
        f = getattr(self.cfg, "generation_for", None)
        return f(self.model) if f else self.cfg.generation

    def _pinned(self) -> str | None:
        f = getattr(self.cfg, "pinned_digest", None)
        return f(self.model) if f else None

    def model_info(self) -> dict:
        """The model's name and digest. If the config pins a digest, the installed one must match it."""
        try:
            r = self._get("ollama", "/api/tags")
        except allowlist.UrlNotAllowed:
            raise
        except Exception as e:  # connection refused, timeout...
            raise LlmError(f"Ollama is not reachable: {e}") from e
        if r.status_code != 200:
            raise LlmError(f"Ollama /api/tags returned HTTP {r.status_code}")
        want = self.model
        for m in r.json().get("models", []):
            name = m.get("name") or m.get("model")
            if name == want or name == f"{want}:latest" or (":" not in want and name.split(":")[0] == want):
                digest = (m.get("digest") or "").lower().removeprefix("sha256:")
                pin = self._pinned()
                if pin and not digest.startswith(pin):
                    raise DigestMismatch(
                        f"installed {name!r} has digest {digest[:12] or '(none)'}..., but the approved config pins "
                        f"{pin[:12]}...; the model changed outside change control. Re-approve the config with the new digest.")
                return {"name": name, "digest": m.get("digest"), "size": m.get("size"), "digest_pinned": bool(pin)}
        raise ModelNotInstalled(f"model {want!r} is not installed in Ollama (run: ollama pull {want})")

    def chat(self, messages: list[dict], schema: dict) -> dict:
        g = self._generation()
        body = {"model": self.model, "messages": messages, "stream": False, "format": schema,
                "options": {"temperature": g["temperature"], "seed": g["seed"], "num_ctx": g["num_ctx"],
                            "num_predict": g["num_predict"]}}
        try:
            r = self._post("ollama", "/api/chat", body, timeout=900)
        except allowlist.UrlNotAllowed:
            raise
        except Exception as e:
            raise LlmError(f"Ollama call failed: {e}") from e
        if r.status_code != 200:
            raise LlmError(f"Ollama /api/chat returned HTTP {r.status_code}: {str(r.text)[:300]}")
        data = r.json()
        content = (data.get("message") or {}).get("content")
        if content is None:
            raise LlmError("Ollama response had no message content")
        return {"content": content, "model": data.get("model"), "eval_count": data.get("eval_count"),
                "prompt_eval_count": data.get("prompt_eval_count"), "total_duration": data.get("total_duration"),
                "load_duration": data.get("load_duration"), "eval_duration": data.get("eval_duration"),
                "prompt_eval_duration": data.get("prompt_eval_duration"), "generation": g}
