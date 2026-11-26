"""Подготовка аудио: любой формат → WAV 16 кГц моно → фрагменты речи по VAD.

Фрагменты режутся только в паузах (Silero VAD), поэтому слова и числа на стыках
не теряются. Если врач говорит без пауз дольше max_chunk_sec, фрагмент режется
принудительно с небольшим перекрытием, а повтор слов на стыке убирается при склейке.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class AudioError(RuntimeError):
    pass


@dataclass
class AudioChunk:
    start: float
    end: float
    path: Path
    forced_split: bool = False   # кусок отрезан не в паузе — возможен повтор слов на стыке


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioError("ffmpeg не найден. Установите ffmpeg и добавьте его в PATH (см. README).")
    return exe


def to_wav16k(src: str | Path, dst: str | Path, sample_rate: int = 16000) -> Path:
    """Конвертация с нормализацией громкости (loudnorm) без шумоподавления, искажающего речь."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-ac", "1", "-ar", str(sample_rate), "-af", "loudnorm=I=-20:TP=-2:LRA=11",
           "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioError(f"ffmpeg не смог обработать {src}: {r.stderr.strip()[:500]}")
    return dst


def plan_chunks(speech: list[tuple[float, float]], max_len: float, overlap: float) -> list[tuple[float, float, bool]]:
    """Склеивает речевые участки в фрагменты не длиннее max_len, разрезая только в паузах.

    Чистая функция — покрыта тестами без моделей.
    """
    chunks: list[tuple[float, float, bool]] = []
    cur_s: float | None = None
    cur_e = 0.0
    for s, e in speech:
        # слишком длинный непрерывный участок — режем принудительно
        while e - s > max_len:
            if cur_s is not None:
                chunks.append((cur_s, cur_e, False))
                cur_s = None
            chunks.append((s, s + max_len, True))
            s = s + max_len - overlap
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= max_len:
            cur_e = e
        else:
            chunks.append((cur_s, cur_e, False))
            cur_s, cur_e = s, e
    if cur_s is not None:
        chunks.append((cur_s, cur_e, False))
    return chunks


def vad_chunks(wav: str | Path, out_dir: str | Path, max_len: float = 22.0, overlap: float = 0.3,
               sample_rate: int = 16000, pad: float = 0.15) -> list[AudioChunk]:
    try:
        import soundfile as sf
        from silero_vad import get_speech_timestamps, load_silero_vad, read_audio
    except ImportError as e:  # pragma: no cover - зависит от установленных пакетов
        raise AudioError("Не установлены silero-vad / soundfile: pip install -e .[asr]") from e

    model = load_silero_vad()
    audio = read_audio(str(wav), sampling_rate=sample_rate)
    ts = get_speech_timestamps(audio, model, sampling_rate=sample_rate, return_seconds=True,
                               min_silence_duration_ms=300, speech_pad_ms=int(pad * 1000))
    speech = [(float(t["start"]), float(t["end"])) for t in ts]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = audio.numpy()
    total = len(data) / sample_rate
    chunks = []
    for i, (s, e, forced) in enumerate(plan_chunks(speech, max_len, overlap)):
        s, e = max(0.0, s), min(total, e)
        path = out_dir / f"chunk_{i:04d}.wav"
        sf.write(str(path), data[int(s * sample_rate): int(e * sample_rate)], sample_rate)
        chunks.append(AudioChunk(s, e, path, forced))
    return chunks


def dedupe_join(prev: str, nxt: str, max_words: int = 4) -> str:
    """Убирает повтор слов на стыке принудительно разрезанных фрагментов."""
    a, b = prev.split(), nxt.split()
    for k in range(min(max_words, len(a), len(b)), 0, -1):
        if [w.lower() for w in a[-k:]] == [w.lower() for w in b[:k]]:
            return " ".join(b[k:])
    return nxt
