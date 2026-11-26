"""SQLite: очередь заданий, кеш распознавания, подтверждения.

Состояние переживает закрытие программы и сбои: задание продолжается с прерванного шага,
готовое повторно не обрабатывается без команды --force.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

STATUSES = ("queued", "transcribed", "filled", "needs_review", "confirmed", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_path TEXT NOT NULL,
    docx_path TEXT NOT NULL,
    doctor TEXT NOT NULL,
    template TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    job_dir TEXT,
    issues_json TEXT,
    timings_json TEXT,
    confirmed_hash TEXT,
    created REAL, updated REAL,
    UNIQUE(audio_path, docx_path, doctor)
);
CREATE TABLE IF NOT EXISTS asr_cache (
    key TEXT PRIMARY KEY,
    transcript_json TEXT NOT NULL,
    created REAL
);
"""


def asr_cache_key(audio_sha: str, doctor: str, engine: str, model: str, settings: dict, algo_version: str) -> str:
    """Кеш учитывает аудио, врача, модель, её настройки и версию алгоритма (§14 ТЗ)."""
    blob = json.dumps({"a": audio_sha, "d": doctor, "e": engine, "m": model, "s": settings, "v": algo_version},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    # --- кеш ASR ---
    def get_transcript(self, key: str) -> dict | None:
        row = self.db.execute("SELECT transcript_json FROM asr_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_transcript(self, key: str, transcript: dict):
        self.db.execute("INSERT OR REPLACE INTO asr_cache VALUES (?,?,?)",
                        (key, json.dumps(transcript, ensure_ascii=False), time.time()))
        self.db.commit()

    # --- задания ---
    def add_job(self, audio: str, docx: str, doctor: str, template: str | None = None) -> int:
        now = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(audio_path, docx_path, doctor, template, created, updated) VALUES (?,?,?,?,?,?)",
            (str(audio), str(docx), doctor, template, now, now))
        self.db.commit()
        row = self.db.execute("SELECT id FROM jobs WHERE audio_path=? AND docx_path=? AND doctor=?",
                              (str(audio), str(docx), doctor)).fetchone()
        return int(row[0])

    def update(self, job_id: int, **fields):
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        self.db.commit()

    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def jobs(self, status: str | None = None, doctor: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if doctor:
            q += " AND doctor=?"
            args.append(doctor)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    def reset(self, job_id: int):
        self.update(job_id, status="queued", error=None, confirmed_hash=None)
