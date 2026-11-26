"""Распознавание речи. Только локальные модели.

Движки:
  gigaam — GigaAM (Сбер, open source, русский язык). Ставится отдельно, см. README.
  text   — вместо распознавания читается готовая расшифровка «<аудио>.txt».
           Нужен для отладки конвейера без моделей и для проверки на эталонных текстах.

Модель загружается один раз на всю очередь (не на каждый файл).
"""

from __future__ import annotations

import gc
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .audio import AudioChunk, dedupe_join, to_wav16k, vad_chunks


class ASRError(RuntimeError):
    pass


@dataclass
class TimedChunk:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    chunks: list[TimedChunk] = field(default_factory=list)
    engine: str = ""
    model: str = ""

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.chunks if c.text).strip()

    def to_dict(self) -> dict:
        return {"engine": self.engine, "model": self.model, "chunks": [asdict(c) for c in self.chunks]}

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls([TimedChunk(**c) for c in d["chunks"]], d.get("engine", ""), d.get("model", ""))


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class TextASR:
    """Готовая расшифровка рядом с аудио: «запись.mp3» → «запись.txt»."""
    name = "text"
    model_name = "file"

    def load(self):
        return self

    def unload(self):
        pass

    def transcribe(self, audio: str | Path, work_dir: str | Path) -> Transcript:
        p = Path(audio)
        txt = p if p.suffix.lower() == ".txt" else p.with_suffix(".txt")
        if not txt.exists():
            raise ASRError(f"Нет файла расшифровки {txt} (движок asr.engine = text)")
        return Transcript([TimedChunk(0.0, 0.0, txt.read_text(encoding="utf-8").strip())], self.name, self.model_name)


class GigaAMASR:
    name = "gigaam"

    def __init__(self, model_name: str = "v2_rnnt", device: str = "cuda", download_root: str | None = None,
                 sample_rate: int = 16000, max_chunk_sec: float = 22.0, chunk_overlap_sec: float = 0.3):
        self.model_name = model_name
        self.device = device
        self.download_root = download_root
        self.sample_rate = sample_rate
        self.max_chunk_sec = max_chunk_sec
        self.overlap = chunk_overlap_sec
        self._model = None

    def load(self):
        if self._model is not None:
            return self
        try:
            import gigaam  # noqa: WPS433
        except ImportError as e:
            raise ASRError("GigaAM не установлен: pip install git+https://github.com/salute-developers/GigaAM") from e
        kwargs = {"device": self.device}
        if self.download_root:
            kwargs["download_root"] = self.download_root
        try:
            self._model = gigaam.load_model(self.model_name, **kwargs)
        except TypeError:  # старые версии без download_root
            self._model = gigaam.load_model(self.model_name, device=self.device)
        return self

    def unload(self):
        """Освобождает видеопамять перед запуском LLM (4 ГБ VRAM не вмещают обе модели)."""
        self._model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def transcribe(self, audio: str | Path, work_dir: str | Path) -> Transcript:
        self.load()
        work = Path(work_dir)
        wav = to_wav16k(audio, work / "audio16k.wav", self.sample_rate)
        chunks: list[AudioChunk] = vad_chunks(wav, work / "chunks", self.max_chunk_sec, self.overlap, self.sample_rate)
        out: list[TimedChunk] = []
        for ch in chunks:
            text = self._model.transcribe(str(ch.path))
            if isinstance(text, (list, tuple)):
                text = " ".join(map(str, text))
            text = str(getattr(text, "text", text)).strip()
            if out and ch.forced_split:
                text = dedupe_join(out[-1].text, text)
            out.append(TimedChunk(round(ch.start, 2), round(ch.end, 2), text))
        return Transcript(out, self.name, self.model_name)


def make_asr(cfg: dict, models_dir: str | Path | None = None):
    engine = cfg.get("engine", "gigaam")
    if engine == "text":
        return TextASR()
    if engine == "gigaam":
        root = cfg.get("model_path") or (str(Path(models_dir) / "asr") if models_dir else None)
        return GigaAMASR(cfg.get("model_name", "v2_rnnt"), cfg.get("device", "cuda"), root,
                         cfg.get("sample_rate", 16000), cfg.get("max_chunk_sec", 22), cfg.get("chunk_overlap_sec", 0.3))
    raise ASRError(f"Неизвестный движок распознавания: {engine}")
