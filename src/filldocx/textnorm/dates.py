"""Дата рождения из диктовки (правило 8).

Поддерживаются:
  «нуль один нуль четыре тысяча девятьсот шестьдесят четыре» → 01.04.1964
  «нуль нуль один нуль четыре …» (оговорка с лишним нулём)  → 01.04.1964
  «12 07 57 года» / «двенадцать ноль семь пятьдесят седьмого года» → 12.07.1957
Век двузначного года не угадывается: если год может быть и 19xx, и 20xx,
дата возвращается как неоднозначная и уходит на проверку.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from .numbers import words_to_digits

_ORD_UNITS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "четвёртого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
}
_ORD_TEENS = {
    "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12, "тринадцатого": 13,
    "четырнадцатого": 14, "пятнадцатого": 15, "шестнадцатого": 16,
    "семнадцатого": 17, "восемнадцатого": 18, "девятнадцатого": 19,
}
_ORD_TENS = {
    "двадцатого": 20, "тридцатого": 30, "сорокового": 40, "пятидесятого": 50,
    "шестидесятого": 60, "семидесятого": 70, "восьмидесятого": 80, "девяностого": 90,
}
_ORD = {**_ORD_UNITS, **_ORD_TEENS, **_ORD_TENS, "двухтысячного": 2000}

_HEADER_RE = re.compile(r"дат[аеу]\s+рождени[ея]", re.I)
_YEAR_WORD_RE = re.compile(r"^(года|год|г)\b\.?", re.I)


@dataclass
class DateResult:
    value: str | None              # «ДД.ММ.ГГГГ» или None
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)
    span: tuple[int, int] | None = None  # позиция в нормализованном тексте


def _ordinals_to_digits(text: str) -> str:
    """«пятьдесят седьмого» → «50 7» → склейка ниже."""
    def repl(m: re.Match) -> str:
        return str(_ORD[m.group(0).lower()])
    return re.sub(r"\b(" + "|".join(_ORD) + r")\b", repl, text, flags=re.I)


def _merge_year_parts(nums: list[str]) -> list[str]:
    """«1900 64» → «1964», «50 7» → «57» (составной год, продиктованный словами)."""
    out: list[str] = []
    for n in nums:
        if out:
            prev = int(out[-1])
            cur = int(n)
            if prev in (1900, 2000) and cur < 100:
                out[-1] = str(prev + cur)
                continue
            if 1000 <= prev and prev % 100 == 0 and cur < 100 and len(out[-1]) == 4:
                out[-1] = str(prev + cur)
                continue
            if 20 <= prev < 100 and prev % 10 == 0 and 0 < cur < 10 and len(out) >= 3:
                # «12 07 50 7» — десятки года и единицы
                out[-1] = str(prev + cur)
                continue
        out.append(n)
    return out


def _valid(d: int, m: int, y: int) -> bool:
    try:
        _dt.date(y, m, d)
        return True
    except ValueError:
        return False


def parse_birth_date_numbers(nums: list[str], today: _dt.date | None = None) -> DateResult:
    today = today or _dt.date.today()
    nums = _merge_year_parts([n for n in nums if n.isdigit()])
    if len(nums) < 2:
        return DateResult(None, True, ["не найдено достаточно цифр даты"])
    year_s = nums[-1]
    head = "".join(nums[:-1])
    notes: list[str] = []

    if len(head) == 5 and head.startswith("00"):
        head = head[1:]
        notes.append("лишний ноль в начале даты (оговорка) — убран")

    candidates: list[tuple[int, int]] = []
    if len(head) == 4:
        candidates = [(int(head[:2]), int(head[2:]))]
    elif len(head) == 3:
        candidates = [(int(head[:1]), int(head[1:])), (int(head[:2]), int(head[2:]))]
    elif len(head) == 2 and len(nums) == 3:
        candidates = [(int(nums[0]), int(nums[1]))]
    elif len(head) == 2:
        candidates = [(int(head[0]), int(head[1]))]
    else:
        return DateResult(None, True, [f"не удалось разобрать день и месяц: «{' '.join(nums)}»"])

    ambiguous = False
    if len(year_s) == 4:
        years = [int(year_s)]
    elif len(year_s) == 2:
        yy = int(year_s)
        if yy > today.year % 100:
            years = [1900 + yy]
        else:
            years = [1900 + yy, 2000 + yy]
            ambiguous = True
            notes.append(f"век года «{year_s}» неоднозначен — не угадываем")
    else:
        return DateResult(None, True, [f"не удалось разобрать год «{year_s}»"])

    valid = [(d, m, y) for d, m in candidates for y in years if _valid(d, m, y)]
    valid = [v for v in valid if _dt.date(v[2], v[1], v[0]) <= today]
    if ambiguous or len({(d, m) for d, m, _ in valid}) != 1 or not valid:
        if not valid:
            notes.append("календарно некорректная дата")
        return DateResult(None, True, notes)
    d, m, y = valid[0]
    return DateResult(f"{d:02d}.{m:02d}.{y:04d}", False, notes)


def find_birth_date(text: str, today: _dt.date | None = None, search_chars: int = 200) -> DateResult:
    """Ищет дату рождения в начале диктовки (после Ф.И.О.).

    Берётся первая непрерывная серия чисел — после слов «дата рождения»,
    если они есть, иначе в первых ``search_chars`` символах.
    """
    norm = words_to_digits(_ordinals_to_digits(text))
    m = _HEADER_RE.search(norm)
    start = m.end() if m else 0
    window_end = start + search_chars
    run = re.compile(r"(?:\d+[\s.,/-]*){2,}")
    for mm in run.finditer(norm, start, min(len(norm), window_end)):
        nums = re.findall(r"\d+", mm.group(0))
        if len(nums) < 2:
            continue
        res = parse_birth_date_numbers(nums, today)
        end = mm.end()
        ym = _YEAR_WORD_RE.match(norm[end:].lstrip())
        if ym:
            end = end + (len(norm[end:]) - len(norm[end:].lstrip())) + ym.end()
        res.span = (_to_source_pos(text, mm.start(), word_start=True), _to_source_pos(text, end))
        return res
    return DateResult(None, True, ["дата рождения не найдена"])


def _to_source_pos(text: str, norm_pos: int, word_start: bool = False) -> int:
    """Позиция в исходном тексте, соответствующая позиции в преобразованном.

    Порядковые числительные («семьдесят седьмого» → «70 7») меняют длину текста,
    поэтому смещение ищется по границам слов исходника (концам слов или их началам).
    """
    if norm_pos <= 0:
        return 0
    rx = r"(?<!\S)\S" if word_start else r"(?<=\S)\s|$"
    bounds = [m.start() for m in re.finditer(rx, text)] + [len(text)]
    for b in bounds:
        if len(words_to_digits(_ordinals_to_digits(text[:b]))) >= norm_pos:
            return b
    return len(text)
