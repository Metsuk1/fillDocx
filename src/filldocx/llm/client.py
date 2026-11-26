"""Клиент локальной LLM через OpenAI-совместимый HTTP API.

Работает с llama.cpp (llama-server) и Ollama, запущенными на этом же компьютере.
Никаких облачных адресов: base_url по умолчанию — 127.0.0.1.
"""

from __future__ import annotations

import json
import re
import time
from typing import Protocol
from urllib.parse import urlparse

import requests

from ..schema import Extraction, extraction_json_schema
from ..templates import TemplateMap
from .prompt import build_messages


class LLMError(RuntimeError):
    pass


class Extractor(Protocol):
    def extract(self, tmpl: TemplateMap, body: str, conclusion: str | None, contrast: bool) -> tuple[Extraction, str]:
        """Возвращает (разобранный ответ, сырой текст ответа)."""


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LocalLLM:
    def __init__(self, base_url: str, model: str = "local", temperature: float = 0.0,
                 max_tokens: int = 4096, timeout_sec: int = 300, response_format: str = "json_schema",
                 allow_remote: bool = False):
        host = urlparse(base_url).hostname or ""
        if host not in _LOCAL_HOSTS and not allow_remote:
            # Защита от случайной отправки медицинских данных наружу.
            raise LLMError(
                f"LLM-сервер {base_url} не локальный. Медицинские данные не отправляются на внешние "
                "адреса. Если сервер в локальной сети клиники — укажите llm.allow_remote: true."
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout_sec
        self.response_format = response_format

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/models", timeout=5)
            return r.ok
        except requests.RequestException:
            return False

    def _format(self, schema: dict) -> dict:
        if self.response_format == "json_object":
            return {"type": "json_object", "schema": schema}
        return {"type": "json_schema", "json_schema": {"name": "extraction", "strict": True, "schema": schema}}

    def extract(self, tmpl: TemplateMap, body: str, conclusion: str | None, contrast: bool) -> tuple[Extraction, str]:
        schema = extraction_json_schema(tmpl)
        payload = {
            "model": self.model,
            "messages": build_messages(tmpl, body, conclusion, contrast),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": self._format(schema),
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                return parse_extraction(raw), raw
            except (requests.RequestException, KeyError, ValueError) as e:
                last_err = e
                time.sleep(1 + attempt)
        raise LLMError(f"LLM не ответила корректно: {last_err}")


    def chat_json(self, messages: list[dict], schema: dict, name: str = "answer",
                  max_tokens: int | None = None) -> tuple[dict, str]:
        """Запрос с ответом строго по JSON-схеме. Возвращает (разобранный JSON, сырой ответ)."""
        fmt = self._format(schema)
        if fmt["type"] == "json_schema":
            fmt["json_schema"]["name"] = name
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "response_format": fmt,
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                return parse_json_object(raw), raw
            except (requests.RequestException, KeyError, ValueError) as e:
                last_err = e
                time.sleep(1 + attempt)
        raise LLMError(f"LLM не ответила корректно: {last_err}")


def parse_json_object(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("в ответе нет JSON")
    return json.loads(m.group(0))


def parse_extraction(raw: str) -> Extraction:
    """Разбирает ответ модели. Срезает рассуждения <think>…</think>, если модель их выдала."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("в ответе нет JSON")
    return Extraction.model_validate(json.loads(m.group(0)))


class FixedExtractor:
    """Возвращает заранее заданный ответ. Для тестов и отладки без модели."""

    def __init__(self, extraction: Extraction | dict | str):
        if isinstance(extraction, str):
            extraction = parse_extraction(extraction)
        elif isinstance(extraction, dict):
            extraction = Extraction.model_validate(extraction)
        self.extraction = extraction

    def extract(self, tmpl, body, conclusion, contrast):
        return self.extraction, self.extraction.model_dump_json()
