"""Проверка фактов LLM по диктовке: «не упомянуто — не вписывается».

Факт проходит, только если:
1. его цитата действительно есть в расшифровке (нечёткое сравнение после нормализации);
2. каждое число слота есть в этой цитате;
3. в формулировке нет чисел, которых нет ни в цитате, ни в норме шаблона;
4. новые слова формулировки (которых нет в норме шаблона) подтверждаются цитатой;
5. орган и слот существуют в карте шаблона.
Не прошедшее не пишется в Word и попадает в список спорных мест.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .schema import Extraction, Finding, Issue, VerifiedOrgan
from .templates import TemplateMap
from .textnorm import extract_numbers, normalize_text

_WORD_RE = re.compile(r"[а-яёa-z]+(?:-[а-яёa-z]+)*", re.I)
_STOP = {
    "в", "во", "на", "и", "с", "со", "по", "до", "от", "к", "за", "из", "у", "о", "об", "при",
    "что", "как", "его", "ее", "её", "их", "это", "так", "же", "а", "но", "или", "также",
}
_NEG_RE = re.compile(r"\bне\s+([а-яё]{4,})", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(s).lower().replace("ё", "е")).strip()


def _stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    return w[:5] if len(w) > 5 else w


def _words(s: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(s) if w.lower() not in _STOP and len(w) >= 3]


@dataclass
class VerifyConfig:
    quote_min_score: float = 85.0
    statement_min_support: float = 0.75


def quote_score(quote: str, transcript_norm: str) -> float:
    q = _norm(quote)
    if not q:
        return 0.0
    if q in transcript_norm:
        return 100.0
    return float(fuzz.partial_ratio(q, transcript_norm))


def unsupported_words(text: str, quote: str, norm: str | None) -> tuple[list[str], int]:
    """Слова формулировки, которых нет ни в норме шаблона, ни в цитате (по основе слова)."""
    base = {_stem(w) for w in _words(norm or "")}
    q_words = _words(normalize_text(quote))
    q_stems = {_stem(w) for w in q_words}
    new = [w for w in _words(text) if _stem(w) not in base]
    bad = []
    for w in new:
        if _stem(w) in q_stems:
            continue
        if any(fuzz.ratio(w, qw) >= 80 for qw in q_words):   # восстановленный термин близок к искажённому
            continue
        bad.append(w)
    return bad, len(new)


def _numbers_ok(text: str, allowed: set[str]) -> list[str]:
    return [n for n in extract_numbers(text) if n not in allowed]


def verify_extraction(
    ext: Extraction,
    tmpl: TemplateMap,
    transcript: str,
    cfg: VerifyConfig | None = None,
) -> tuple[dict[str, VerifiedOrgan], list[Issue]]:
    """Возвращает подтверждённые данные по органам (в порядке шаблона) и спорные места."""
    cfg = cfg or VerifyConfig()
    tnorm = _norm(transcript)
    issues: list[Issue] = []
    organs: dict[str, VerifiedOrgan] = {}

    for f in ext.findings:
        org = tmpl.organ(f.organ)
        if org is None:
            issues.append(Issue(level="error", code="unknown_organ", message=f"Орган «{f.organ}» не из шаблона — отброшено", quote=f.quote))
            continue
        score = quote_score(f.quote, tnorm)
        if score < cfg.quote_min_score:
            issues.append(Issue(level="error", code="quote_not_found",
                                message=f"{org.name}: цитата не найдена в расшифровке ({score:.0f}%) — не вписано",
                                organ=org.id, quote=f.quote))
            continue

        q_numbers = set(extract_numbers(f.quote))
        norm_numbers = set(extract_numbers(org.norm or ""))

        # слоты
        good_slots: dict[str, list[str]] = {}
        for sv in f.slots:
            if sv.slot not in org.slots:
                issues.append(Issue(level="warning", code="unknown_slot",
                                    message=f"{org.name}: у органа нет поля «{sv.slot}» — значение {sv.values} не вписано",
                                    organ=org.id, quote=f.quote))
                continue
            vals = [v.strip().replace(".", ",").lstrip("+") for v in sv.values if v.strip()]
            missing = [v for v in vals if v.lstrip("-") not in q_numbers]
            if not vals or missing:
                issues.append(Issue(level="error", code="slot_value_not_in_quote",
                                    message=f"{org.name}: число {missing or sv.values} для «{org.slots[sv.slot].about}» нет в цитате — не вписано",
                                    organ=org.id, quote=f.quote))
                continue
            good_slots[sv.slot] = vals

        # формулировка
        text = f.text.strip()
        if f.status == "norm":
            text = ""
        if text:
            extra = _numbers_ok(text, q_numbers | norm_numbers | set(good_slots_values(good_slots)))
            if extra:
                issues.append(Issue(level="error", code="text_number_not_in_quote",
                                    message=f"{org.name}: в формулировке числа {extra}, которых нет в диктовке — формулировка не вписана",
                                    organ=org.id, quote=f.quote))
                text = ""
        if text:
            bad, total = unsupported_words(text, f.quote, org.norm)
            support = 1.0 - (len(bad) / total) if total else 1.0
            if support < cfg.statement_min_support:
                issues.append(Issue(level="error", code="text_not_supported",
                                    message=f"{org.name}: в формулировке есть слова, которых врач не говорил ({', '.join(bad[:8])}) — не вписано",
                                    organ=org.id, quote=f.quote))
                text = ""
            elif bad:
                issues.append(Issue(level="info", code="text_words_restored",
                                    message=f"{org.name}: слова восстановлены по смыслу, проверьте: {', '.join(bad[:8])}",
                                    organ=org.id, quote=f.quote))
            for neg in _NEG_RE.findall(_norm(f.quote)):
                st = _stem(neg)
                if any(_stem(w) == st for w in _words(text)) and not re.search(rf"\bне\s+{re.escape(st)}", text.lower()):
                    issues.append(Issue(level="warning", code="negation_lost",
                                        message=f"{org.name}: в диктовке «не {neg}», в формулировке отрицание могло потеряться",
                                        organ=org.id, quote=f.quote))
        if f.status in ("changed", "absent") and not text and not good_slots and not org.formed:
            continue  # нечего вписывать, причина уже в issues

        vo = organs.setdefault(org.id, VerifiedOrgan(organ=org.id, status=f.status))
        # слияние поздних уточнений: последнее значение побеждает, расхождение — на проверку
        for slot, vals in good_slots.items():
            if slot in vo.slots and vo.slots[slot] != vals:
                issues.append(Issue(level="warning", code="value_changed_later",
                                    message=f"{org.name}: «{org.slots[slot].about}» сначала {vo.slots[slot]}, затем {vals} — учтено последнее",
                                    organ=org.id, quote=f.quote))
            vo.slots[slot] = vals
        if f.status != vo.status:
            if "norm" in (f.status, vo.status) and "changed" in (f.status, vo.status):
                issues.append(Issue(level="warning", code="norm_and_changes",
                                    message=f"{org.name}: продиктованы и норма, и изменения — проверьте",
                                    organ=org.id, quote=f.quote))
            vo.status = "absent" if "absent" in (f.status, vo.status) else "changed"
        if text and text not in vo.texts:
            vo.texts.append(text)
        vo.quotes.append(f.quote)

    for phrase in ext.offtopic:
        issues.append(Issue(level="info", code="offtopic", message="Посторонний разговор — в Word не попал", quote=phrase))
    for phrase in ext.unclear:
        issues.append(Issue(level="warning", code="unclear",
                            message="Медицинский фрагмент не удалось восстановить — в Word не попал, прослушайте",
                            quote=phrase))

    ordered = dict(sorted(organs.items(), key=lambda kv: tmpl.order(kv[0])))
    return ordered, issues


def good_slots_values(slots: dict[str, list[str]]) -> list[str]:
    return [v.lstrip("-") for vals in slots.values() for v in vals]


def verify_conclusion(text: str | None, raw: str | None, min_support: float = 0.75) -> tuple[str | None, list[Issue]]:
    """Заключение от LLM должно опираться на продиктованное заключение."""
    if not raw:
        return None, []
    if not text:
        return None, [Issue(level="warning", code="conclusion_missing",
                            message="Заключение продиктовано, но не восстановлено — вписано не будет, проверьте", quote=raw)]
    extra = _numbers_ok(text, set(extract_numbers(raw)))
    bad, total = unsupported_words(text, raw, None)
    support = 1.0 - (len(bad) / total) if total else 1.0
    if extra or support < min_support:
        return None, [Issue(level="error", code="conclusion_not_supported",
                            message=f"Заключение содержит то, чего нет в диктовке ({', '.join((extra + bad)[:8])}) — не вписано",
                            quote=raw)]
    issues = []
    if bad:
        issues.append(Issue(level="info", code="conclusion_words_restored",
                            message=f"В заключении восстановлены слова, проверьте: {', '.join(bad[:8])}", quote=raw))
    return text, issues
