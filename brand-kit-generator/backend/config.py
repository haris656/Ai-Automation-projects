"""
Central configuration. Reads everything from the environment.
No secrets are ever hardcoded. Fails fast and loud on startup if a
required value is missing, rather than failing silently mid-request.

Provider selection:
  LLM_PROVIDER = "anthropic" (default) or "openai"
  - anthropic uses ANTHROPIC_API_KEY, default model claude-sonnet-4-6
  - openai    uses OPENAI_API_KEY,    default model gpt-4o
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
}


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str
    model: str
    max_brief_chars: int
    rate_limit_per_minute: int
    allowed_origins: tuple[str, ...]

    @staticmethod
    def load() -> "Settings":
        provider = os.environ.get("LLM_PROVIDER", "anthropic").lower().strip()
        if provider not in ("anthropic", "openai"):
            raise RuntimeError(
                f"LLM_PROVIDER must be 'anthropic' or 'openai', got '{provider}'."
            )

        if provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            key_name = "ANTHROPIC_API_KEY"
        else:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            key_name = "OPENAI_API_KEY"

        if not key:
            raise RuntimeError(
                f"{key_name} is not set for provider '{provider}'. "
                f"Copy .env.example to .env and add your key."
            )

        model = os.environ.get("MODEL", "").strip() or _DEFAULT_MODELS[provider]

        origins_raw = os.environ.get("ALLOWED_ORIGINS", "*").strip()
        origins = tuple(o.strip() for o in origins_raw.split(",") if o.strip())

        return Settings(
            provider=provider,
            api_key=key,
            model=model,
            max_brief_chars=int(os.environ.get("MAX_BRIEF_CHARS", "1200")),
            rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "10")),
            allowed_origins=origins or ("*",),
        )
