"""
LLM client used by every agent.

Supports two providers behind one interface, chosen by config:
  - "anthropic" (Claude)  -> POST https://api.anthropic.com/v1/messages
  - "openai"   (GPT)      -> POST https://api.openai.com/v1/chat/completions

Responsibilities:
- One place that talks to the API, so retries and error handling live here.
- A defensive JSON extractor: the model is never trusted to return clean JSON.
- A helper that fences untrusted user text so it cannot be read as instructions.

The API key is read from config (env only) and never logged.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        provider = provider.lower().strip()
        if provider not in ("anthropic", "openai"):
            raise ValueError(f"Unsupported provider: {provider}")
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    @property
    def provider(self) -> str:
        return self._provider

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.7,
        retries: int = 3,
    ) -> str:
        """Call the model and return raw text. Retries with backoff on transient errors."""
        if self._provider == "anthropic":
            headers, body = self._anthropic_payload(system, user, max_tokens, temperature)
            url = ANTHROPIC_URL
        else:
            headers, body = self._openai_payload(system, user, max_tokens, temperature)
            url = OPENAI_URL

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(url, headers=headers, json=body)
                if resp.status_code == 200:
                    return self._extract_text(resp.json())
                if resp.status_code in (429, 500, 502, 503, 529):
                    last_err = LLMError(f"Transient API error {resp.status_code}")
                else:
                    raise LLMError(f"API error {resp.status_code}")
            except (httpx.HTTPError, LLMError) as exc:
                last_err = exc
            time.sleep(1.5 * (attempt + 1))

        raise LLMError(f"LLM call failed after {retries} attempts: {last_err}")

    def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 1500,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        text = self.complete(system, user, max_tokens=max_tokens, temperature=temperature)
        return extract_json(text)

    # ---- provider-specific request/response shapes ----

    def _anthropic_payload(self, system: str, user: str, max_tokens: int, temperature: float):
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        return headers, body

    def _openai_payload(self, system: str, user: str, max_tokens: int, temperature: float):
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return headers, body

    def _extract_text(self, data: dict[str, Any]) -> str:
        if self._provider == "anthropic":
            parts = data.get("content", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        else:
            choices = data.get("choices", [])
            text = ""
            if choices and isinstance(choices[0], dict):
                text = (choices[0].get("message", {}) or {}).get("content", "") or ""
        if not text.strip():
            raise LLMError("Empty completion")
        return text


def extract_json(text: str) -> dict[str, Any]:
    """
    Pull a JSON object out of model output without trusting it to be clean.
    Strips code fences, then falls back to grabbing the outermost braces.
    """
    cleaned = re.sub(r"```json", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError("Could not parse JSON from model output")


def fence(label: str, untrusted: str) -> str:
    """
    Wrap untrusted user input in clear delimiters so the model treats it as
    data, not instructions. Used by every agent that touches the user's brief.
    """
    return f"<{label}>\n{untrusted}\n</{label}>"
