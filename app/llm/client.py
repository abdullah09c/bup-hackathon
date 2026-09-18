"""Minimal OpenAI-compatible chat-completions client (Gemini, OpenAI, Groq, OpenRouter, AgentRouter, Ollama...)."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import LLMEndpoint


class LLMError(Exception):
    """Controlled failure: message never contains the API key or raw response bodies."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def chat_json(ep: LLMEndpoint, messages: list[dict], timeout_s: float) -> tuple[Any, str]:
    """Send messages, return (parsed JSON, raw text)."""
    body: dict[str, Any] = {"model": ep.model, "messages": messages, "temperature": 0, "max_tokens": 1500}
    if ep.json_mode:
        body["response_format"] = {"type": "json_object"}
    if ep.reasoning_effort:
        body["reasoning_effort"] = ep.reasoning_effort
    headers = {"Authorization": f"Bearer {ep.api_key}", "Content-Type": "application/json", **ep.extra_headers}

    try:
        resp = await _http().post(f"{ep.base_url}/chat/completions", json=body, headers=headers, timeout=timeout_s)
    except httpx.TimeoutException:
        raise LLMError("LLM request timed out") from None
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM connection error ({type(exc).__name__})") from None
    if resp.status_code != 200:
        retry_after = None
        try:
            retry_after = float(resp.headers.get("retry-after", ""))
        except ValueError:
            pass
        raise LLMError(f"LLM HTTP {resp.status_code}", status=resp.status_code, retry_after=retry_after)
    try:
        text = resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("LLM response had an unexpected shape") from None
    return extract_json(text), text


def extract_json(text: str) -> Any:
    """Parse JSON from model text, tolerating code fences or surrounding prose."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError("LLM did not return valid JSON")
