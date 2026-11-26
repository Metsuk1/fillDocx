"""Конвейер обработки одной диктовки и очереди.

  аудио → ASR (кеш) → нормализация и словарь → разбор на части → LLM (факты)
        → проверка источника → правила → запись в Word → журнал и спорные места

Очередь идёт в две фазы: сначала распознавание всех файлов (модель ASR в памяти одна),
затем извлечение и запись (в памяти только LLM). На видеокарте 4 ГБ обе модели сразу
не помещаются.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from . import ALGO_VERSION
from .asr import Transcript, file_sha256, make_asr
from .config import Settings
from .docx_writer import DocxEditor
from .llm import LocalLLM
from .rules import build_operations, conclusion_hints
from .schema import Issue
from .segment import split_dictation
from .storage import Store, asr_cache_key
from .templates import TemplateMap, contrast_from_filename, detect_template, load_all_templates
from .textnorm import normalize_text
from .textnorm.terms import Lexicon, correct_terms
from .verify import VerifyConfig, verify_conclusion, verify_extraction


class PipelineError(RuntimeError):
    pass


@dataclass
class TimedText:
    """Нормализованный текст диктовки и привязка символов ко времени в аудио."""
    text: str
    spans: list[tuple[int, int, float, float]] = field(default_factory=list)

    def time_of(self, start: int, end: int) -> tuple[float | None, float | None]:
        t0 = t1 = None
        for s, e, a, b in self.spans:
            if s <= start < e or (t0 is None and start < s):
                t0 = a if t0 is None else t0
            if s < end <= e or s <= end - 1 < e:
                t1 = b
        return t0, t1

    def locate(self, quote: str) -> tuple[float | None, float | None]:
        q = normalize_text(quote).lower()
        if not q or not self.text:
            return None, None
        if all(a == b for _, _, a, b in self.spans):  # расшифровка без меток времени (движок text)
            return None, None
        al = fuzz.partial_ratio_alignment(q, self.text.lower())
        if al is None or al.score < 70:
            return None, None
        return self.time_of(al.dest_start, al.dest_end)


def build_timed_text(tr: Transcript, lex: Lexicon) -> tuple[TimedText, list]:
    """Нормализуется весь текст сразу: число может начаться в одном фрагменте аудио и кончиться
    в следующем («…двадцать шесть и один на | пятьдесят четыре и шесть миллиметров»).
    Привязка ко времени — приблизительная, по длине нормализованного начала текста."""
    chunks = [ch for ch in tr.chunks if ch.text.strip()]
    raw = " ".join(ch.text.strip() for ch in chunks)
    norm = normalize_text(raw)
    text, fixes = correct_terms(norm, lex)
    scale = len(text) / len(norm) if norm else 1.0
    spans, prev, acc = [], 0, []
    for ch in chunks:
        acc.append(ch.text.strip())
        end = min(len(text), round(len(normalize_text(" ".join(acc))) * scale))
        if end > prev:
            spans.append((prev, end, ch.start, ch.end))
            prev = end
    return TimedText(text, spans), fixes


@dataclass
class JobResult:
    output_docx: Path
    issues: list[Issue]
    timings: dict[str, float]
    journal: Path


def _contrast_from_doc(field_value: str | None) -> bool | None:
    if field_value is None:
        return None
    v = field_value.lower()
    if re.search(r"не\s+введ", v):
        return False
    if re.search(r"кт[\s-]?скан|йогексол|омнипак|ультравист|контраст", v):
        return True
    return None


class Pipeline:
    def __init__(self, settings: Settings, store: Store | None = None, asr=None, extractor=None):
        self.s = settings
        self.store = store or Store(settings.work_dir / "filldocx.sqlite")
        self.templates: dict[str, TemplateMap] = load_all_templates(settings.path("templates_dir"))
        self._asr = asr
        self._extractor = extractor

    # --- компоненты по требованию ---
    @property
    def asr(self):
        if self._asr is None:
            self._asr = make_asr(self.s["asr"], self.s.path("models_dir"))
        return self._asr

    @property
    def extractor(self):
        if self._extractor is None:
            c = self.s["llm"]
            self._extractor = LocalLLM(c["base_url"], c["model"], c["temperature"], c["max_tokens"],
                                       c["timeout_sec"], c["response_format"], c.get("allow_remote", False))
        return self._extractor

    def lexicon(self, doctor: str) -> Lexicon:
        lex_dir = self.s.path("lexicon_dir")
        return Lexicon.load(lex_dir / "corrections.yaml", lex_dir / "terms.yaml",
                            lex_dir / "terms_archive.yaml", self.s.doctor_lexicon(doctor))

    def job_dir(self, job_id: int) -> Path:
        d = self.s.work_dir / "jobs" / f"{job_id:06d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- шаг 1–2: распознавание с кешем ---
    def transcribe(self, audio: str | Path, doctor: str, job_dir: Path, force: bool = False) -> Transcript:
        asr = self.asr
        settings = {k: v for k, v in self.s["asr"].items() if k not in ("device",)}
        key = asr_cache_key(file_sha256(audio), doctor, asr.name, asr.model_name, settings, ALGO_VERSION)
        if not force:
            cached = self.store.get_transcript(key)
            if cached:
                return Transcript.from_dict(cached)
        tr = asr.transcribe(audio, job_dir)
        self.store.put_transcript(key, tr.to_dict())
        (job_dir / "transcript_raw.json").write_text(json.dumps(tr.to_dict(), ensure_ascii=False, indent=1), "utf-8")
        return tr

    # --- шаги 3–7: от расшифровки к документу ---
    def fill(self, transcript: Transcript, docx_path: str | Path, doctor: str, job_dir: Path,
             audio_name: str = "", template_id: str | None = None, today: dt.date | None = None) -> JobResult:
        timings: dict[str, float] = {}
        t = time.perf_counter()
        issues: list[Issue] = []

        editor = DocxEditor(docx_path, self.s["docx"]["red_color"], self.s["docx"]["font_name"],
                            self.s["docx"]["font_size_pt"])
        tmpl = self.templates.get(template_id) if template_id else detect_template(
            self.templates, audio_name or Path(docx_path).name, editor.heading())
        if tmpl is None:
            raise PipelineError(f"Не удалось определить шаблон для «{Path(docx_path).name}». Укажите --template.")

        if self.s.data.get("fill_mode", "edit") == "edit":
            return self._fill_edit(editor, tmpl, transcript, docx_path, doctor, job_dir, audio_name, today)

        field_contrast = editor.read_field(tmpl.fields.get("contrast", "Контрастное вещество:"))
        contrast = _contrast_from_doc(field_contrast)
        if contrast is None:
            contrast = contrast_from_filename(audio_name or Path(docx_path).name)
        fio = editor.read_field("Ф.И.О.:")

        timed, fixes = build_timed_text(transcript, self.lexicon(doctor))
        parts = split_dictation(timed.text, fio, today=today)
        body = parts.body + "".join(" " + txt for _, txt in parts.late_additions)
        timings["normalize_segment"] = time.perf_counter() - t

        t = time.perf_counter()
        ext, raw = self.extractor.extract(tmpl, body, parts.conclusion, contrast)
        timings["llm_extract"] = time.perf_counter() - t

        t = time.perf_counter()
        vcfg = VerifyConfig(self.s["verify"]["quote_min_score"], self.s["verify"]["statement_min_support"])
        organs, v_issues = verify_extraction(ext, tmpl, body, vcfg)
        issues += v_issues
        conclusion, c_issues = verify_conclusion(ext.conclusion, parts.conclusion, vcfg.statement_min_support)
        issues += c_issues

        bd = parts.birth_date
        birth = bd.value if bd and not bd.ambiguous else None
        if bd and bd.ambiguous:
            issues.append(Issue(level="warning", code="birth_date", rule=8,
                                message="Дата рождения не вписана: " + "; ".join(bd.notes or ["не распознана"])))
        for n in (bd.notes if bd and not bd.ambiguous else []):
            issues.append(Issue(level="info", code="birth_date_note", rule=8, message=f"Дата рождения: {n}"))

        if parts.contrast_said:
            said_no = bool(re.search(r"не\s+введ|не\s+вводил", parts.contrast_said, re.I))
            said_yes = bool(re.search(r"скан|йогексол|омнипак|ультравист", parts.contrast_said, re.I))
            doc = _contrast_from_doc(field_contrast)
            if doc is not None and ((said_no and doc) or (said_yes and not doc)):
                issues.append(Issue(level="warning", code="contrast_mismatch", rule=16,
                                    message=f"Продиктовано «{parts.contrast_said}», а в документе «{field_contrast}» — поле не изменено, проверьте"))

        ops, r_issues = build_operations(tmpl, organs, birth_date=birth, conclusion=conclusion,
                                         recommendations=parts.recommendations,
                                         recommendations_label=parts.recommendations_label)
        issues += r_issues
        described = " ".join(" ".join(v.texts) for v in organs.values()) + " " + body
        issues += conclusion_hints(tmpl, described, conclusion)

        not_said = [o.name for o in tmpl.organs
                    if not o.virtual and o.id not in organs and (contrast or not o.contrast_only)]
        if not_said:
            issues.append(Issue(level="info", code="not_dictated",
                                message="Не продиктовано — текст шаблона оставлен как есть и не подтверждён: " + ", ".join(not_said)))
        for f in fixes:
            issues.append(Issue(level="info", code="term_corrected", quote=f.original,
                                message=f"Исправлено распознавание: «{f.original}» → «{f.replacement}»"))
        timings["verify_rules"] = time.perf_counter() - t

        t = time.perf_counter()
        out = self._output_path(docx_path, job_dir)
        res = editor.apply(ops, tmpl.content_start)
        issues += res.issues
        editor.save(out)
        timings["write_docx"] = time.perf_counter() - t

        for i in issues:
            if i.quote and i.t_start is None:
                i.t_start, i.t_end = timed.locate(i.quote)

        journal = job_dir / "journal.json"
        journal.write_text(json.dumps({
            "algo_version": ALGO_VERSION,
            "template": tmpl.id, "contrast": contrast, "doctor": doctor,
            "transcript": transcript.to_dict(),
            "normalized": timed.text,
            "parts": {"body": parts.body, "conclusion": parts.conclusion, "recommendations": parts.recommendations,
                      "late_additions": parts.late_additions, "removed": parts.removed,
                      "birth_date": bd.value if bd else None},
            "llm_raw": raw,
            "verified": {k: v.model_dump() for k, v in organs.items()},
            "operations": [o.model_dump() for o in ops],
            "applied": len(res.applied),
            "issues": [i.model_dump() for i in issues],
            "timings": timings,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return JobResult(out, issues, timings, journal)

    def _fill_edit(self, editor: DocxEditor, tmpl: TemplateMap, transcript: Transcript, docx_path,
                   doctor: str, job_dir: Path, audio_name: str, today) -> JobResult:
        """Режим «правка шаблона»: LLM точечно правит абзацы шаблона врача (красное — варианты)."""
        from .edit_fill import fill_by_edit
        timings: dict[str, float] = {}
        t = time.perf_counter()
        timed, fixes = build_timed_text(transcript, self.lexicon(doctor))
        parts = split_dictation(timed.text, editor.read_field("Ф.И.О.:") or None, today=today)
        timings["normalize_segment"] = time.perf_counter() - t

        t = time.perf_counter()
        r = fill_by_edit(editor, self.extractor, parts, timed.text, tmpl.content_start,
                         Path(docx_path).name, self.s["verify"]["quote_min_score"],
                         int(self.s.data.get("llm_workers", 4)))
        timings["llm_total"] = time.perf_counter() - t
        timings.update(r.timings)
        issues = r.issues
        for f in fixes:
            issues.append(Issue(level="info", code="term_corrected", quote=f.original,
                                message=f"Исправлено распознавание: «{f.original}» → «{f.replacement}»"))

        t = time.perf_counter()
        out = self._output_path(docx_path, job_dir)
        editor.save(out)
        timings["write_docx"] = time.perf_counter() - t
        for i in issues:
            if i.quote and i.t_start is None:
                i.t_start, i.t_end = timed.locate(i.quote)

        journal = job_dir / "journal.json"
        journal.write_text(json.dumps({
            "algo_version": ALGO_VERSION, "mode": "edit",
            "template": tmpl.id, "doctor": doctor,
            "transcript": transcript.to_dict(),
            "normalized": timed.text,
            "parts": {"body": parts.body, "conclusion": parts.conclusion, "recommendations": parts.recommendations,
                      "late_additions": parts.late_additions, "removed": parts.removed,
                      "birth_date": parts.birth_date.value if parts.birth_date else None,
                      "fio_said": parts.fio_said, "contrast_said": parts.contrast_said},
            "edit": r.journal,
            "issues": [i.model_dump() for i in issues],
            "timings": timings,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return JobResult(out, issues, timings, journal)

    def _output_path(self, docx_path: str | Path, job_dir: Path) -> Path:
        src = Path(docx_path)
        if self.s["output_mode"] == "copy":
            out = self.s.work_dir / "out" / src.name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            return out
        backup = job_dir / "backup" / src.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(src, backup)  # копия подготовленного этапом 1 документа
        return src

    # --- одно задание целиком ---
    def process(self, audio: str | Path, docx: str | Path, doctor: str, template_id: str | None = None,
                force: bool = False) -> JobResult:
        job_id = self.store.add_job(str(audio), str(docx), doctor, template_id)
        jd = self.job_dir(job_id)
        try:
            t = time.perf_counter()
            tr = self.transcribe(audio, doctor, jd, force)
            asr_time = time.perf_counter() - t
            self.store.update(job_id, status="transcribed", job_dir=str(jd))
            res = self.fill(tr, self._fresh_docx(docx, jd), doctor, jd, Path(audio).name, template_id)
            res.timings["asr"] = asr_time
            self._finish(job_id, res)
            return res
        except Exception as e:
            self.store.update(job_id, status="error", error=str(e))
            raise

    def _fresh_docx(self, docx: str | Path, jd: Path) -> Path:
        """При повторной обработке берём исходную копию, чтобы не вписать значения дважды."""
        backup = jd / "backup" / Path(docx).name
        if backup.exists() and self.s["output_mode"] == "in_place":
            shutil.copy2(backup, docx)
        return Path(docx)

    def _finish(self, job_id: int, res: JobResult):
        self.store.update(job_id, status="needs_review",
                          issues_json=json.dumps([i.model_dump() for i in res.issues], ensure_ascii=False),
                          timings_json=json.dumps(res.timings), error=None, confirmed_hash=None)

    # --- очередь в две фазы ---
    def run_queue(self, doctor: str | None = None, force: bool = False, log=print) -> None:
        jobs = [j for j in self.store.jobs(doctor=doctor) if j["status"] in ("queued", "error") or force]
        if not jobs:
            log("Очередь пуста.")
            return
        log(f"Фаза 1: распознавание {len(jobs)} диктовок…")
        transcripts: dict[int, tuple[Transcript, float]] = {}
        try:
            for j in jobs:
                jd = self.job_dir(j["id"])
                t = time.perf_counter()
                try:
                    transcripts[j["id"]] = (self.transcribe(j["audio_path"], j["doctor"], jd, force), time.perf_counter() - t)
                    self.store.update(j["id"], status="transcribed", job_dir=str(jd))
                    log(f"  [{j['id']}] распознано за {time.perf_counter() - t:.1f} с")
                except Exception as e:  # одна ошибка не останавливает очередь
                    self.store.update(j["id"], status="error", error=f"ASR: {e}")
                    log(f"  [{j['id']}] ОШИБКА распознавания: {e}")
        finally:
            if self._asr is not None:
                self._asr.unload()

        log("Фаза 2: извлечение фактов и заполнение Word…")
        for j in jobs:
            if j["id"] not in transcripts:
                continue
            tr, asr_time = transcripts[j["id"]]
            jd = self.job_dir(j["id"])
            try:
                res = self.fill(tr, self._fresh_docx(j["docx_path"], jd), j["doctor"], jd,
                                Path(j["audio_path"]).name, j["template"])
                res.timings["asr"] = asr_time
                self._finish(j["id"], res)
                n_err = sum(1 for i in res.issues if i.level != "info")
                log(f"  [{j['id']}] готово к проверке, спорных мест: {n_err}")
            except Exception as e:
                self.store.update(j["id"], status="error", error=f"Заполнение: {e}")
                log(f"  [{j['id']}] ОШИБКА заполнения: {e}")

    # --- подтверждение (этап 3) ---
    def confirm(self, job_id: int) -> str:
        j = self.store.job(job_id)
        h = file_sha256(j["docx_path"])
        self.store.update(job_id, status="confirmed", confirmed_hash=h)
        return h

    def is_confirmed_current(self, job_id: int) -> bool:
        """Подтверждение относится к конкретной сохранённой версии документа."""
        j = self.store.job(job_id)
        return bool(j and j["status"] == "confirmed" and j["confirmed_hash"]
                    and Path(j["docx_path"]).exists() and file_sha256(j["docx_path"]) == j["confirmed_hash"])
