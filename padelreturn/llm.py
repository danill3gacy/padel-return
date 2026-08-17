"""Тонкая обёртка над LLM. Без ключа продукт работает — просто на правилах и шаблонах."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .config import Config


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.llm_enabled

    def complete(self, prompt: str, system: str = "", max_tokens: int = 600) -> str | None:
        if not self.enabled:
            return None
        try:
            if self.cfg.llm_provider == "anthropic":
                return self._anthropic(prompt, system, max_tokens)
            return self._openai(prompt, system, max_tokens)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:  # pragma: no cover
            print(f"[llm] недоступен, работаем на правилах: {e}")
            return None

    def json(self, prompt: str, system: str = "", max_tokens: int = 600) -> dict | None:
        raw = self.complete(prompt, system, max_tokens)
        if not raw:
            return None
        return extract_json(raw)

    def _anthropic(self, prompt: str, system: str, max_tokens: int) -> str:
        url = (self.cfg.llm_base_url or "https://api.anthropic.com") + "/v1/messages"
        payload = {
            "model": self.cfg.llm_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.cfg.llm_api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))

    def _openai(self, prompt: str, system: str, max_tokens: int) -> str:
        url = (self.cfg.llm_base_url or "https://api.openai.com/v1") + "/chat/completions"
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload = {"model": self.cfg.llm_model, "messages": messages, "max_tokens": max_tokens}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.cfg.llm_api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
