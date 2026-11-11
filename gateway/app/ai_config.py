"""Version-controlled AI configuration (AI-06): approved models (name + pinned digest), system prompt, prompt format,
retrieval settings, generation settings and verifier thresholds live in ONE file, gateway/config/ai_config.json (in git).

"Changed only through change control" is simulated like the rest of the demo: the file's canonical SHA-256
must match gateway/config/ai_config.lock.json, which records who approved it (two distinct people) under which
change reference. If the config is edited without re-approval the gateway refuses every AI call.
There is deliberately NO environment-variable override of the model: it would bypass change control.

Two config shapes are understood:
  * `models` (list of {name, digest, generation?}) + `default_model`  - current shape. Every model carries a pinned
    digest; a call is refused if the installed digest differs (a silent re-pull cannot change an 'approved' model).
  * legacy single `model` string (config <= 1.1.0): one model, digest not pinned (only logged)."""
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .auth import APPROVER_ROLES, USERS

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class ConfigNotApproved(Exception):
    pass


class ModelNotApproved(ValueError):
    """The caller asked for a model that is not in the approved list."""


@dataclass(frozen=True)
class AiConfig:
    version: str
    sha256: str
    change_ref: str
    default_model: str
    models: tuple          # tuple of {"name", "digest" (str|None), "generation" (dict)}
    system_prompt: str
    retrieval: dict
    generation: dict
    verifier: dict
    prompt: dict
    approved_by: tuple

    @property
    def model(self) -> str:
        """The default model (kept for callers that predate per-call selection)."""
        return self.default_model

    def approved_names(self) -> list[str]:
        return [m["name"] for m in self.models]

    def resolve_model(self, requested: str | None) -> tuple[str, str]:
        """Return (model name, chosen_by). `requested` must be in the approved list; None -> the default."""
        if requested is None or not str(requested).strip():
            return self.default_model, "default"
        name = str(requested).strip()
        if name not in self.approved_names():
            raise ModelNotApproved(f"model {name!r} is not an approved model; approved: {self.approved_names()}")
        return name, "request"

    def _entry(self, name: str) -> dict:
        for m in self.models:
            if m["name"] == name:
                return m
        raise ModelNotApproved(f"model {name!r} is not an approved model; approved: {self.approved_names()}")

    def pinned_digest(self, name: str) -> str | None:
        return self._entry(name)["digest"]

    def generation_for(self, name: str) -> dict:
        """Shared generation settings, overridden per model only where the approved config says so."""
        return {**self.generation, **self._entry(name).get("generation", {})}

    def identity(self) -> dict:
        return {"config_version": self.version, "config_sha256": self.sha256, "change_ref": self.change_ref,
                "approved_by": list(self.approved_by), "model": self.default_model,
                "default_model": self.default_model, "approved_models": self.approved_names(),
                "digest_pinned": {m["name"]: bool(m["digest"]) for m in self.models}}


def config_paths() -> tuple[Path, Path]:
    d = Path(os.environ.get("AI_CONFIG_DIR", CONFIG_DIR))
    return d / "ai_config.json", d / "ai_config.lock.json"


def canonical_sha256(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _models_from(raw: dict) -> tuple[str, tuple]:
    if "models" in raw:
        items = raw["models"]
        if not isinstance(items, list) or not items:
            raise ConfigNotApproved("AI config 'models' must be a non-empty list")
        names, out = [], []
        for m in items:
            name = (m or {}).get("name") if isinstance(m, dict) else None
            digest = (m or {}).get("digest") if isinstance(m, dict) else None
            if not name:
                raise ConfigNotApproved("every approved model needs a name")
            if not isinstance(digest, str) or len(digest.strip()) < 12:
                raise ConfigNotApproved(f"approved model {name!r} must pin a digest (at least 12 characters)")
            names.append(name)
            out.append({"name": name, "digest": digest.strip().lower().removeprefix("sha256:"),
                        "generation": dict(m.get("generation") or {})})
        if len(set(names)) != len(names):
            raise ConfigNotApproved("duplicate model in the approved list")
        default = raw.get("default_model")
        if default not in names:
            raise ConfigNotApproved("default_model must be one of the approved models")
        return default, tuple(out)
    if raw.get("model"):  # legacy single-model shape
        return raw["model"], ({"name": raw["model"], "digest": None, "generation": {}},)
    raise ConfigNotApproved("AI config missing 'models'")


def load_approved() -> AiConfig:
    """Load the config and prove it is the approved one. Raises ConfigNotApproved otherwise."""
    cfg_path, lock_path = config_paths()
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigNotApproved(f"AI config unreadable: {e}") from e
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigNotApproved("AI config has no approval lock (run `python -m app.ai_config approve`)") from e
    sha = canonical_sha256(raw)
    if lock.get("sha256") != sha or lock.get("config_version") != raw.get("config_version"):
        raise ConfigNotApproved("AI config was changed without change control: its hash/version does not match "
                                "the approval lock")
    approvers = tuple(lock.get("approved_by") or ())
    if len(set(approvers)) < 2:
        raise ConfigNotApproved("approval lock needs two distinct approvers")
    for k in ("system_prompt", "retrieval", "generation", "verifier", "prompt", "config_version"):
        if not raw.get(k):
            raise ConfigNotApproved(f"AI config missing {k!r}")
    default, models = _models_from(raw)
    tpl = raw["prompt"].get("passage_template", "")
    if "{locator}" not in tpl or "{text}" not in tpl or not raw["prompt"].get("instructions"):
        raise ConfigNotApproved("AI config prompt needs passage_template with {locator} and {text}, and instructions")
    return AiConfig(version=raw["config_version"], sha256=sha, change_ref=lock.get("change_ref", ""),
                    default_model=default, models=models, system_prompt=raw["system_prompt"],
                    retrieval=raw["retrieval"], generation=raw["generation"], verifier=raw["verifier"],
                    prompt=raw["prompt"], approved_by=approvers)


def approve(approvers: list[str], change_ref: str) -> dict:
    """Record approval of the CURRENT config file by two distinct steward/QA users."""
    if len(set(approvers)) < 2:
        raise ValueError("two distinct approvers are required")
    for a in approvers:
        if USERS.get(a) not in APPROVER_ROLES:
            raise ValueError(f"{a!r} is not a steward/QA approver")
    cfg_path, lock_path = config_paths()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    try:
        prev = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = None
    if prev and prev.get("config_version") == raw["config_version"] and prev.get("sha256") != canonical_sha256(raw):
        raise ValueError(f"config content changed but config_version is still {raw['config_version']}: "
                         "bump config_version before approving (every change is a new version)")
    if not change_ref.strip():
        raise ValueError("a change reference is required")
    lock = {"config_version": raw["config_version"], "sha256": canonical_sha256(raw), "change_ref": change_ref,
            "approved_by": list(approvers), "approved_at": datetime.now(timezone.utc).isoformat(),
            "note": "SIMULATED change-control approval for the demo."}
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock


if __name__ == "__main__":  # python -m app.ai_config approve --by steward-a qa-1 --change-ref CC-SYN-0003
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["approve", "show"])
    ap.add_argument("--by", nargs="+", default=[])
    ap.add_argument("--change-ref", default="")
    a = ap.parse_args()
    if a.cmd == "approve":
        print(json.dumps(approve(a.by, a.change_ref), indent=2))
    else:
        print(json.dumps(load_approved().identity(), indent=2))
    sys.exit(0)
