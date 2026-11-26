"""Числа, произнесённые словами, → цифры.

«сто семьдесят три запятая четыре» → «173,4»
«плотность минус один»            → «плотность -1»
«нуль один нуль четыре тысяча девятьсот шестьдесят четыре» → «0 1 0 4 1964»

Грамматика одного числа: [тысячи] [сотни] [десятки] [единицы|11–19].
Каждая часть занимает своё место, поэтому «пять пять» — два числа, а
«двадцать пять» — одно. Ноль всегда отдельное число: так разбираются
даты, продиктованные цифрами.
"""

from __future__ import annotations

import re

_UNITS = {
    "ноль": 0, "нуль": 0, "нуля": 0, "ноля": 0,
    "один": 1, "одна": 1, "одно": 1, "одного": 1, "одной": 1, "одну": 1,
    "два": 2, "две": 2, "двух": 2,
    "три": 3, "трех": 3, "трёх": 3,
    "четыре": 4, "четырех": 4, "четырёх": 4,
    "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9,
}
_TEENS = {
    "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
}
_TENS = {
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70,
    "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
}
_HUNDREDS = {
    "сто": 100, "ста": 100,
    "двести": 200, "двухсот": 200,
    "триста": 300, "трехсот": 300, "трёхсот": 300,
    "четыреста": 400, "четырехсот": 400, "четырёхсот": 400,
    "пятьсот": 500, "пятисот": 500,
    "шестьсот": 600, "шестисот": 600,
    "семьсот": 700, "семисот": 700,
    "восемьсот": 800, "восьмисот": 800,
    "девятьсот": 900, "девятисот": 900,
}
_THOUSAND = {"тысяча", "тысячи", "тысяч", "тысячу"}
_DECIMAL_SEP = {"запятая", "запятой", "целых", "целая"}
_SIGN = {"минус": "-", "плюс": "+"}

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*|[^\sA-Za-zА-Яа-яЁё\d]")


def _is_numword(tok: str) -> bool:
    t = tok.lower()
    return t in _UNITS or t in _TEENS or t in _TENS or t in _HUNDREDS or t in _THOUSAND


def _parse_block(toks: list[str], i: int) -> tuple[int | None, int]:
    """Сотни/десятки/единицы. Возвращает (значение, индекс после), либо (None, i)."""
    value = 0
    j = i
    found = False
    t = toks[j].lower() if j < len(toks) else ""
    if t in _HUNDREDS:
        value += _HUNDREDS[t]
        j += 1
        found = True
        t = toks[j].lower() if j < len(toks) else ""
    if t in _TEENS:
        return value + _TEENS[t], j + 1
    if t in _TENS:
        value += _TENS[t]
        j += 1
        found = True
        t = toks[j].lower() if j < len(toks) else ""
    if t in _UNITS and _UNITS[t] != 0:
        return value + _UNITS[t], j + 1
    if t in _UNITS and _UNITS[t] == 0 and not found:
        return 0, j + 1
    return (value, j) if found else (None, i)


def _thousand_agrees(block: int, word: str) -> bool:
    """Согласование: «одна тысяча», «четыре тысячи», «пять тысяч».

    Нужно для дат цифрами: «…ноль четыре тысяча девятьсот …» — это «4» и «1964»,
    а не 4964.
    """
    last2, last = block % 100, block % 10
    if last == 1 and last2 != 11:
        return word in ("тысяча", "тысячу")
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return word == "тысячи"
    return word == "тысяч"


def _parse_int(toks: list[str], i: int) -> tuple[int | None, int]:
    t = toks[i].lower() if i < len(toks) else ""
    if t in _UNITS and _UNITS[t] == 0:
        return 0, i + 1
    if t in _THOUSAND:  # «тысяча девятьсот …»
        rest, j = _parse_block(toks, i + 1)
        return 1000 + (rest or 0), j
    block, j = _parse_block(toks, i)
    if block is None:
        return None, i
    if j < len(toks) and toks[j].lower() in _THOUSAND and _thousand_agrees(block, toks[j].lower()):
        rest, k = _parse_block(toks, j + 1)
        if rest == 0:  # «две тысячи ноль» — не склеиваем ноль
            rest, k = None, j + 1
        return block * 1000 + (rest or 0), k
    return block, j


def _parse_fraction(toks: list[str], i: int) -> tuple[str | None, int]:
    """Дробная часть после «запятая»: ведущие нули + число, как продиктовано."""
    zeros = ""
    j = i
    while j < len(toks) and toks[j].lower() in ("ноль", "нуль"):
        zeros += "0"
        j += 1
    if j < len(toks) and toks[j].isdigit():
        return zeros + toks[j], j + 1
    val, k = _parse_block(toks, j)
    if val is None or val == 0:
        return (zeros or None), j
    return zeros + str(val), k


def words_to_digits(text: str) -> str:
    """Заменяет числа словами на цифры, десятичный разделитель — запятая.

    Всё, что не является числом, копируется из исходного текста как есть
    (пробелы, дефисы, «Вне- внутрипеченочные»).
    """
    matches = list(_TOKEN_RE.finditer(text))
    toks = [m.group(0) for m in matches]
    out: list[str] = []
    pos = 0          # сколько исходного текста уже скопировано
    prev_num = False
    i = 0
    while i < len(toks):
        tok = toks[i]
        low = tok.lower()
        sign = ""
        start = i
        # «10-12» — диапазон, а не минус; «минус один», «-1» — знак
        symbol_sign = tok in ("-", "+", "−") and not prev_num
        if (low in _SIGN or symbol_sign) and i + 1 < len(toks) and (
            _is_numword(toks[i + 1]) or toks[i + 1][0].isdigit()
        ):
            sign = _SIGN.get(low, "-" if tok in ("-", "−") else "+")
            i += 1
            tok = toks[i]

        num: str | None = None
        j = i
        if re.fullmatch(r"\d+(?:[.,]\d+)?", tok):
            num = tok.replace(".", ",")
            j = i + 1
            # «173 запятая 4» (смешанный вывод ASR)
            if "," not in num and j < len(toks) and toks[j].lower() in _DECIMAL_SEP and j + 1 < len(toks):
                frac, k = _parse_fraction(toks, j + 1)
                if frac is not None:
                    num, j = f"{num},{frac}", k
        else:
            val, j = _parse_int(toks, i)
            if val is not None:
                num = str(val)
                if j < len(toks) and toks[j].lower() in _DECIMAL_SEP and j + 1 < len(toks):
                    frac, k = _parse_fraction(toks, j + 1)
                    if frac is not None:
                        num, j = f"{num},{frac}", k

        if num is None:
            prev_num = False
            i = start + 1
            continue
        out.append(text[pos:matches[start].start()])
        out.append(sign + num)
        pos = matches[j - 1].end()
        prev_num = True
        i = j
    out.append(text[pos:])
    return "".join(out)


_NUM_RE = re.compile(r"[-+]?\d+(?:,\d+)?")


def extract_numbers(text: str) -> list[str]:
    """Все числа в тексте (после words_to_digits), без знака, с запятой."""
    return [m.lstrip("+-") for m in _NUM_RE.findall(words_to_digits(text).replace(".", ","))]
