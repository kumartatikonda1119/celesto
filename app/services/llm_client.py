"""Minimal OpenAI-compatible chat client (OpenRouter by default).

No SDK dependency: one POST to /chat/completions with tool definitions. This
keeps the agent loop transparent and makes it trivial to swap providers by
changing LLM_BASE_URL.

The LLM key never leaves the control plane and is never written into a guest
or a log line (see app/services/evidence.py:redact).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings, get_settings
from .evidence import redact


class LLMError(RuntimeError):
    """The model endpoint failed or returned something unusable."""


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_llm:
            raise LLMError("LLM_API_KEY and LLM_MODEL must be configured")
        self.url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
            # OpenRouter asks for these but they are optional elsewhere.
            "HTTP-Referer": "https://github.com/saferepo/saferepo",
            "X-Title": "SafeRepo",
        }

        last_error = ""
        for attempt in range(3):
            try:
                with httpx.Client(timeout=180.0) as client:
                    response = client.post(self.url, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    time.sleep(2 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise LLMError(
                        f"LLM endpoint returned HTTP {response.status_code}: "
                        f"{redact(response.text[:500], self.settings)}"
                    )
                data = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
                time.sleep(2 * (attempt + 1))
        else:
            raise LLMError(f"LLM request failed after retries: {last_error}")

        usage = data.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.calls += 1

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM response has no choices: {data!r}"[:500])
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMError(f"LLM response has no message object: {data!r}"[:500])
        return message

    def usage_summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
