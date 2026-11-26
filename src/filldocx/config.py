"""Загрузка config/settings.yaml."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "work_dir": "work",
    "models_dir": "models",
    "output_mode": "in_place",
    "asr": {"engine": "gigaam", "model_name": "v2_rnnt", "model_path": None, "device": "cuda",
            "sample_rate": 16000, "max_chunk_sec": 22, "chunk_overlap_sec": 0.3},
    "llm": {"base_url": "http://127.0.0.1:8080/v1", "model": "local", "temperature": 0.0,
            "max_tokens": 4096, "timeout_sec": 300, "response_format": "json_schema", "allow_remote": False},
    "verify": {"quote_min_score": 85, "statement_min_support": 0.75},
    "docx": {"red_color": "FF0000", "font_name": "Times New Roman", "font_size_pt": 14},
    "doctors": {},
    "templates_dir": "config/templates",
    "lexicon_dir": "config/lexicon",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Settings:
    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        if path is None:
            path = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        root = path.resolve().parent.parent if path.parent.name == "config" else path.resolve().parent
        return cls(_merge(DEFAULTS, data or {}), root)

    def path(self, key: str) -> Path:
        p = Path(self.data[key])
        return p if p.is_absolute() else self.root / p

    def rel(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def __getitem__(self, key):
        return self.data[key]

    @property
    def work_dir(self) -> Path:
        return self.path("work_dir")

    def doctor_lexicon(self, doctor: str) -> Path | None:
        d = self.data["doctors"].get(doctor) or {}
        return self.rel(d.get("lexicon"))
