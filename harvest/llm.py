"""LLM-based extraction via a local Ollama model (default) or any OpenAI-compatible endpoint."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .config import LLMConfig


def _prompt(cfg: LLMConfig, text: str, url: str) -> str:
    shape = json.dumps(cfg.schema_, indent=2) if cfg.schema_ else "{ any relevant fields }"
    want = f'{{"items": [ {shape} , ... ]}}' if cfg.many else shape
    return (
        f"{cfg.instructions}\n\n"
        f"Return ONLY valid JSON matching this shape:\n{want}\n"
        "Use null for anything not present on the page. Do not invent values.\n\n"
        f"Page URL: {url}\nPage content:\n\"\"\"\n{text[: cfg.max_chars]}\n\"\"\""
    )


def _parse(raw: str, many: bool) -> list[dict[str, Any]]:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", raw, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict) and many and isinstance(data.get("items"), list):
        data = data["items"]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


class LLMExtractor:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.client = httpx.AsyncClient(timeout=cfg.timeout)
        self.last_error: str | None = None

    async def __call__(self, text: str, url: str) -> list[dict[str, Any]]:
        try:
            if self.cfg.provider == "ollama":
                raw = await self._ollama(_prompt(self.cfg, text, url))
            else:
                raw = await self._openai(_prompt(self.cfg, text, url))
            self.last_error = None
            return _parse(raw, self.cfg.many)
        except Exception as e:
            self.last_error = f"LLM {self.cfg.provider} failed: {type(e).__name__}: {e}"
            return []

    async def _ollama(self, prompt: str) -> str:
        r = await self.client.post(
            f"{self.cfg.host.rstrip('/')}/api/chat",
            json={
                "model": self.cfg.model,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    async def _openai(self, prompt: str) -> str:
        key = os.environ.get(self.cfg.api_key_env, "")
        r = await self.client.post(
            f"{self.cfg.host.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            json={
                "model": self.cfg.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()
