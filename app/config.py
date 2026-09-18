"""Runtime configuration from environment variables (never logged, never returned)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("gridwise.config")

# OpenAI-compatible presets. Any other provider: set LLM_BASE_URL.
PRESETS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openrouter": ("https://openrouter.ai/api/v1", None),
    "agentrouter": ("https://agentrouter.org/v1", None),
    "commandcode": ("https://api.commandcode.ai/provider/v1", "poolside/laguna-s-2.1-free"),
}


@dataclass
class LLMEndpoint:
    name: str
    base_url: str
    api_key: str
    model: str
    extra_headers: dict = field(default_factory=dict)
    json_mode: bool = True
    reasoning_effort: Optional[str] = None


def _headers(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return {str(k): str(v) for k, v in val.items()}
    except json.JSONDecodeError:
        pass
    log.warning("ignoring malformed *_HEADERS value (expected a JSON object)")
    return {}


def _endpoints(prefix: str, label: str) -> list[LLMEndpoint]:
    """One endpoint per model. {prefix}MODEL may list several models separated by commas,
    each optionally suffixed with @<reasoning_effort>, e.g. "openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none".
    Providers rate-limit per model, so a list multiplies the available throughput."""
    env = os.environ.get
    provider = (env(f"{prefix}PROVIDER") or "").strip().lower()
    key = (env(f"{prefix}API_KEY") or "").strip()
    if provider == "stub" or not key:
        return []
    base, default_model = PRESETS.get(provider, (None, None))
    base = (env(f"{prefix}BASE_URL") or base or "").strip().rstrip("/")
    models = [m.strip() for m in (env(f"{prefix}MODEL") or default_model or "").split(",") if m.strip()]
    if not base or not models:
        log.warning("%s LLM disabled: set %sBASE_URL and %sMODEL (or a known %sPROVIDER)", label, prefix, prefix, prefix)
        return []
    default_effort = env(f"{prefix}REASONING_EFFORT") or None
    out = []
    for spec in models:
        model, _, effort = spec.partition("@")
        out.append(LLMEndpoint(
            name=f"{label}:{provider or 'custom'}:{model}",
            base_url=base,
            api_key=key,
            model=model,
            extra_headers=_headers(env(f"{prefix}HEADERS")),
            json_mode=(env(f"{prefix}JSON_MODE") or "true").lower() != "false",
            reasoning_effort=effort or default_effort,
        ))
    return out


@dataclass
class Settings:
    endpoints: list[LLMEndpoint]      # primary models first, then fallback models
    timeout_s: float
    request_budget_s: float
    n_primary: int = 0                # primary models are rotated round-robin to spread rate limits


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader for local runs. Real environment variables take precedence."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


def load_settings() -> Settings:
    load_dotenv()
    primary = _endpoints("LLM_", "primary")
    return Settings(
        endpoints=primary + _endpoints("LLM_FALLBACK_", "fallback"),
        n_primary=len(primary),
        timeout_s=float(os.environ.get("LLM_TIMEOUT_SECONDS", "10")),
        request_budget_s=float(os.environ.get("LLM_REQUEST_BUDGET_SECONDS", "20")),
    )
