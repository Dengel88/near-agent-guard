"""One small client for any OpenAI-compatible /chat/completions endpoint:
Gemini (its OpenAI-compatible API) or NEAR AI Cloud. `None` means mock mode."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx

from .config import Settings

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class LLMError(RuntimeError):
    pass


class ChatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise LLMError("An API key is required (GEMINI_API_KEY or LLM_API_KEY)")
        self.model = model
        self._kw = dict(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"},
                        timeout=60, transport=transport)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            # a client per call: serverless runtimes and test clients may switch event loops
            async with httpx.AsyncClient(**self._kw) as client:
                r = await client.post("/chat/completions", json={"model": self.model, **body})
        except httpx.TimeoutException as exc:
            raise LLMError("LLM timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM unreachable: {type(exc).__name__} {exc}") from exc
        if r.status_code >= 400:
            raise LLMError(f"LLM {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   effort: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": messages, "temperature": 0.2}
        if effort:
            body["reasoning_effort"] = effort
        if tools:
            body["tools"] = tools
        msg = await self._post(body)
        calls = []
        for tc in msg.get("tool_calls") or []:
            raw = tc["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id") or uuid.uuid4().hex, "name": tc["function"]["name"], "arguments": args,
                          "extra_content": tc.get("extra_content")})
        return {"content": _clean(msg.get("content")), "tool_calls": calls}

    async def json(self, system: str, user: str) -> dict[str, Any]:
        msg = await self._post({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        })
        text = _clean(msg.get("content")) or ""
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError(f"screener returned no JSON: {text[:200]}")
        return json.loads(m.group(0))


def _clean(content: str | None) -> str | None:
    if not content:
        return content
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def make_llm(settings: Settings) -> ChatLLM | None:
    if settings.llm_provider == "mock":
        return None
    # a missing key degrades to the offline screener instead of a dead deploy;
    # /api/health then reports llm=mock so it is visible
    if settings.llm_provider == "gemini":
        return ChatLLM(GEMINI_BASE_URL, settings.gemini_api_key, settings.gemini_model) if settings.gemini_api_key else None
    if settings.llm_provider == "openai":
        return ChatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model) if settings.llm_api_key else None
    raise LLMError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")
