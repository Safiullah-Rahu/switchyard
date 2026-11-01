"""Settings: where state lives, which mode we run in, and the loaded policy files."""

from __future__ import annotations

import json
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

Mode = Literal["offline", "live"]


def load_toml(name: str) -> dict[str, Any]:
    """Load a config file, preferring SWITCHYARD_CONFIG_DIR over the packaged defaults."""
    override_dir = os.environ.get("SWITCHYARD_CONFIG_DIR")
    if override_dir:
        candidate = Path(override_dir) / name
        if candidate.exists():
            return tomllib.loads(candidate.read_text("utf-8"))
    text = resources.files("switchyard.defaults").joinpath(name).read_text("utf-8")
    return tomllib.loads(text)


def resolve_mode(explicit: str | None = None) -> Mode:
    """Live mode needs an API key and is never chosen silently for tests or demos."""
    wanted = (explicit or os.environ.get("SWITCHYARD_MODE") or "").lower()
    if wanted == "offline":
        return "offline"
    if wanted == "live":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("SWITCHYARD_MODE=live needs OPENAI_API_KEY")
        return "live"
    return "live" if os.environ.get("OPENAI_API_KEY") else "offline"


@dataclass
class Settings:
    home: Path
    mode: Mode
    policy: dict[str, Any]
    pricing: dict[str, Any]
    estimator: dict[str, Any]
    approval_secret: bytes
    webhook_secret: bytes
    approver_tokens: dict[str, str]
    lease_seconds: float = 60.0
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 300.0
    verify_attempts: int = 3
    max_review_revisions: int = 2
    demo_flags: set[str] = field(default_factory=set)

    @property
    def db_path(self) -> Path:
        return self.home / "switchyard.db"

    @property
    def sandbox_db_path(self) -> Path:
        return self.home / "sandbox_apps.db"

    def principal_for_token(self, token: str) -> str | None:
        return self.approver_tokens.get(token)

    def token_for(self, principal: str) -> str:
        for token, who in self.approver_tokens.items():
            if who == principal:
                return token
        raise KeyError(principal)


def _load_or_create_secrets(home: Path, principals: list[str]) -> dict[str, Any]:
    path = home / "secrets.json"
    if path.exists():
        data = json.loads(path.read_text("utf-8"))
    else:
        data = {
            "approval_secret": secrets.token_hex(32),
            "webhook_secret": secrets.token_hex(32),
            "approver_tokens": {},
        }
    tokens: dict[str, str] = data.setdefault("approver_tokens", {})
    known = set(tokens.values())
    for principal in principals:
        if principal not in known:
            tokens[f"sy_{secrets.token_urlsafe(18)}"] = principal
    path.write_text(json.dumps(data, indent=2), "utf-8")
    return data


def load_settings(home: str | Path | None = None, mode: str | None = None, **overrides: Any) -> Settings:
    root = Path(home or os.environ.get("SWITCHYARD_HOME") or ".switchyard").resolve()
    root.mkdir(parents=True, exist_ok=True)
    policy = load_toml("policy.toml")
    principals = list(policy.get("principals", {}))
    stored = _load_or_create_secrets(root, principals)

    approval_secret = os.environ.get("SWITCHYARD_APPROVAL_SECRET") or stored["approval_secret"]
    webhook_secret = os.environ.get("SWITCHYARD_WEBHOOK_SECRET") or stored["webhook_secret"]
    tokens = dict(stored["approver_tokens"])
    # "maya.patel:token,omar.haddad:token" — lets a deployment manage tokens outside the file.
    for pair in filter(None, os.environ.get("SWITCHYARD_APPROVER_TOKENS", "").split(",")):
        who, _, token = pair.partition(":")
        tokens[token.strip()] = who.strip()

    settings = Settings(
        home=root,
        mode=resolve_mode(mode),
        policy=policy,
        pricing=load_toml("pricing.toml"),
        estimator=load_toml("estimator.toml"),
        approval_secret=approval_secret.encode(),
        webhook_secret=webhook_secret.encode(),
        approver_tokens=tokens,
    )
    for key, value in overrides.items():
        if not hasattr(settings, key):
            raise TypeError(f"unknown setting {key!r}")
        setattr(settings, key, value)
    return settings
