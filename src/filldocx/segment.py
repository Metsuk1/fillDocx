"""Разбор диктовки на служебную часть, описание, заключение и рекомендации.

Детерминированно (без LLM) снимается то, что надёжно ловится по словам:
  правило 24 — «пациент» в начале;
  правило 8  — Ф.И.О. (берётся из документа, в текст не попадает) и дата рождения;
  правило 9  — название исследования после даты рождения;
  правило 16 — «контраст КТ-скан», «контраст не введён» — служебные фразы в начале;
  правило 15 — «всё на этом», «пока всё», «всё» в конце диктовки;
  §12, §13, правило 46 — границы заключения и рекомендаций.
Посторонние разговоры внутри описания отделяет LLM на шаге извлечения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .textnorm.dates import DateResult, find_birth_date

_PATIENT_RE = re.compile(r"^\s*(?:пациент(?:ка)?|фамилия)\b[\s,.:]*", re.I)
_STUDY_RE = re.compile(
    r"^[\s,.:]*(?:кт|мрт|мскт|к[оа]т[еэ]?|компьютерная томография)\b[\sа-яё,.-]{0,60}?"
    r"(?:сегмента|мозга|носа|клетки|полости|позвоночника|таза|суставов|сустава|отдела|органов|шеи)\b[\s,.:]*",
    re.I,
)
_CONTRAST_RE = re.compile(
    r"\bконтраст(?:ное вещество)?\s*[,:-]?\s*(кт[\s-]?скан|к[оа]т[еэи]?[\s-]?скан\w*|ктск\w*|не\s+введ[еёо]н[оа]?|не\s+вводил(?:ся|и)?|йогексол|омнипак\w*|ультравист\w*)?[\s,.]*",
    re.I,
)
_END_RE = re.compile(r"[\s,.]*(?:(?:вс[её]|это)\s+на\s+этом|пока\s+вс[её]|вс[её])[\s.!]*$", re.I)
_CONCL_FALLBACK_RE = re.compile(r"(?<![А-Яа-яЁё])(?:КТ[\s-]*|кото\s*)?картин[аы](?![А-Яа-яЁё])", re.I)
_MID_END_RE = re.compile(r"[\s,.]*\b(?:пока\s+вс[её]|вс[её]\s+на\s+этом|это\s+вс[её])\b[\s,.]*", re.I)
_CONCLUSION_RE = re.compile(r"\bзаключени[еяю]\b\s*[:.,]?\s*", re.I)
_LATE_ADDITION_RE = re.compile(r"\b(?:дополн\w*|вернемся|вернёмся|возвращаемся)\s+(?:к\s+)?(?:описани\w*|описанию)\b", re.I)
_RECO_RE = re.compile(
    r"\b(рекомендовано|рекомендуется|рекомендации|рекомендую|рекомендуем|необходимо|показано)\b\s*[:,]?\s*",
    re.I,
)
_CONSULT_RE = re.compile(r"\bконсультаци[яи]\b", re.I)


@dataclass
class DictationParts:
    body: str
    body_offset: int                      # позиция body в полном тексте (для меток времени)
    conclusion: str | None = None
    conclusion_offset: int | None = None
    recommendations: str | None = None
    recommendations_label: str | None = None
    late_additions: list[tuple[int, str]] = field(default_factory=list)  # (offset, text)
    birth_date: DateResult | None = None
    contrast_said: str | None = None
    fio_said: str | None = None           # Ф.И.О., продиктованное перед датой рождения
    study_said: str | None = None
    removed: list[tuple[str, str]] = field(default_factory=list)  # (что, текст)


def _strip_fio(text: str, fio: str | None) -> tuple[str, int, str]:
    """Убирает Ф.И.О. в начале диктовки. Возвращает (остаток, сдвиг, снятое)."""
    if not fio:
        return text, 0, ""
    parts = [p.lower() for p in re.findall(r"[А-Яа-яЁё-]{2,}", fio)]
    pos = 0
    removed = []
    for m in re.finditer(r"\s*([А-Яа-яЁё-]+)[\s,.]*", text):
        if m.start() != pos or len(removed) >= len(parts) + 1:
            break
        word = m.group(1).lower()
        if any(fuzz.ratio(word, p) >= 75 for p in parts):
            removed.append(m.group(1))
            pos = m.end()
        else:
            break
    return text[pos:], pos, " ".join(removed)


def split_dictation(text: str, fio: str | None = None, today=None) -> DictationParts:
    """Делит нормализованный текст диктовки на части. Смещения — относительно ``text``."""
    removed: list[tuple[str, str]] = []
    offset = 0
    rest = text

    m = _PATIENT_RE.match(rest)
    if m:
        removed.append(("служебное слово (правило 24)", m.group(0).strip()))
        offset += m.end()
        rest = rest[m.end():]

    rest2, shift, fio_said = _strip_fio(rest, fio)
    if fio_said:
        removed.append(("Ф.И.О. (правило 8)", fio_said))
    offset += shift
    rest = rest2

    fio_said = None
    birth = find_birth_date(rest[:220], today=today)
    if birth.span:
        s, e = birth.span
        head = rest[:s]
        # всё до даты в первых словах — продолжение Ф.И.О. или «дата рождения»
        if len(head) <= 60:
            name = re.sub(r"\b(?:дата|год)\s+рождени\w*.*$", "", head, flags=re.I).strip(" ,.:")
            if name and re.fullmatch(r"[А-Яа-яЁё-]+(?:\s+[А-Яа-яЁё-]+){0,3}", name):
                fio_said = name
            removed.append(("дата рождения (правило 8)", rest[:e].strip()))
            offset += e
            rest = rest[e:]

    m = _STUDY_RE.match(rest)
    study = None
    if m:
        study = m.group(0).strip(" ,.:")
        removed.append(("название исследования (правило 9)", study))
        offset += m.end()
        rest = rest[m.end():]

    contrast = None
    m = _CONTRAST_RE.search(rest[:160])
    if m and m.start() <= 40:
        contrast = m.group(0).strip(" ,.")
        removed.append(("контраст — служебная фраза (правило 16)", contrast))
        rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]

    m = _END_RE.search(rest)
    if m:
        removed.append(("конец диктовки (правило 15)", m.group(0).strip(" ,.")))
        rest = rest[:m.start()]

    parts = DictationParts(body=rest, body_offset=offset, birth_date=birth,
                           contrast_said=contrast, study_said=study, removed=removed,
                           fio_said=fio_said)

    m = _CONCLUSION_RE.search(rest)
    if m:
        body_end, concl_start = m.start(), m.end()
    else:
        # слово «заключение» не сказано (или не распознано): заключение протокола КТ
        # начинается с «КТ-картина …» — ищем во второй половине диктовки
        fm = _CONCL_FALLBACK_RE.search(rest, len(rest) // 2)
        if not fm:
            parts.body = rest.strip()
            rm = _RECO_RE.search(rest, len(rest) * 2 // 3)
            if rm:
                parts.recommendations_label = rm.group(1).capitalize() + ":"
                parts.recommendations = rest[rm.end():].strip(" ,.")
                parts.body = rest[:rm.start()].strip()
            return parts
        body_end = concl_start = fm.start()

    parts.body = rest[:body_end].rstrip(" ,.") + "."
    concl = rest[concl_start:]
    concl_offset = offset + concl_start

    # поздние дополнения после заключения возвращаются в описание
    late = _LATE_ADDITION_RE.search(concl)
    if late:
        parts.late_additions.append((concl_offset + late.start(), concl[late.start():].strip()))
        concl = concl[:late.start()]

    # «…пока всё, а при КТ-урографии норма» — после «пока всё» врач вспомнил описание
    mid = _MID_END_RE.search(concl)
    if mid and len(concl[mid.end():].split()) >= 2:
        tail = concl[mid.end():]
        if _RECO_RE.match(tail) or _CONSULT_RE.match(tail):
            concl = concl[:mid.start()] + " " + tail
        else:
            parts.late_additions.append((concl_offset + mid.end(), re.sub(r"^\s*а\s+", "", tail).strip()))
            removed.append(("конец диктовки (правило 15)", mid.group(0).strip(" ,.")))
            concl = concl[:mid.start()]

    rm = _RECO_RE.search(concl)
    if rm:
        parts.recommendations_label = rm.group(1).capitalize() + ":"
        parts.recommendations = concl[rm.end():].strip(" ,.")
        concl = concl[:rm.start()]
    else:
        cm = _CONSULT_RE.search(concl)
        if cm:
            parts.recommendations = concl[cm.start():].strip(" ,.")
            concl = concl[:cm.start()]

    parts.conclusion = concl.strip(" ,.") or None
    parts.conclusion_offset = concl_offset
    return parts
