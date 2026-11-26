"""Исправление искажённых медицинских терминов (§9 «Медицинская коррекция»).

Два уровня, оба консервативные:
1. Явные замены фраз из словаря (config/lexicon/corrections.yaml и профиль врача):
   «окно кодальный» → «кранио-каудальный».
2. Нечёткое сравнение слова со словарём терминов (rapidfuzz). Срабатывает только
   для длинных слов, которых нет в словаре, и только при высоком сходстве.

Каждая замена возвращается в журнал, чтобы её было видно в интерфейсе проверки.
Смысловое восстановление целых предложений делает LLM на шаге извлечения;
здесь — только то, что можно сделать надёжно без контекста.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process


@dataclass
class Correction:
    original: str
    replacement: str
    kind: str  # phrase | fuzzy
    score: float = 100.0


@dataclass
class Lexicon:
    phrases: dict[str, str] = field(default_factory=dict)   # искажённая фраза → правильная
    terms: set[str] = field(default_factory=set)             # правильные термины (слова)
    fuzzy_min_score: float = 88.0
    fuzzy_min_len: int = 7

    @classmethod
    def load(cls, *paths: str | Path) -> "Lexicon":
        lex = cls()
        for p in paths:
            if not p:
                continue
            path = Path(p)
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            lex.phrases.update({k.lower(): v for k, v in (data.get("phrases") or {}).items()})
            for t in data.get("terms") or []:
                lex.terms.update(w.lower() for w in re.findall(r"[а-яё-]+", t.lower()) if len(w) >= 4)
            if "fuzzy_min_score" in data:
                lex.fuzzy_min_score = float(data["fuzzy_min_score"])
        # правильные формы из замен тоже считаются терминами
        for v in lex.phrases.values():
            lex.terms.update(w.lower() for w in re.findall(r"[а-яё-]+", v.lower()) if len(w) >= 4)
        return lex


# окончания русских слов: слово и термин, различающиеся только ими, — формы одного слова
_ENDINGS = set("""
- а я о е ы и у ю ь ью ой ей ом ем ам ям ах ях ами ями ов ев ий ый ая яя ое ее ые ие ого его ому ему
ым им ых их ую юю ет ют ит ят ется ются ится ятся ен ена ено ены ия ии ию ией ий ая ую
""".split()) | {""}


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def correct_terms(text: str, lex: Lexicon) -> tuple[str, list[Correction]]:
    fixes: list[Correction] = []
    # 1. фразы — от длинных к коротким, по границам слов
    for wrong in sorted(lex.phrases, key=len, reverse=True):
        right = lex.phrases[wrong]
        pat = re.compile(rf"(?<![А-Яа-яЁё]){re.escape(wrong)}(?![А-Яа-яЁё])", re.I)
        if pat.search(text):
            text = pat.sub(right, text)
            fixes.append(Correction(wrong, right, "phrase"))

    # 2. отдельные слова
    if not lex.terms:
        return text, fixes
    vocab = list(lex.terms)

    def repl(m: re.Match) -> str:
        word = m.group(0)
        low = word.lower()
        if len(low) < lex.fuzzy_min_len or low in lex.terms:
            return word
        best = process.extractOne(low, vocab, scorer=fuzz.ratio)
        if not best or best[1] < lex.fuzzy_min_score:
            return word
        cand = best[0]
        # окончание не трогаем: термины в словаре — в начальной форме, поэтому
        # заменяем только если отличается корень, а длина почти та же
        if abs(len(cand) - len(low)) > 2:
            return word
        # отличается только окончание («плотностью» ~ «плотность», «признаками» ~ «признаки») —
        # это другая форма того же слова, а не ошибка распознавания
        common = len(_common_prefix(low, cand))
        if common >= 5 and low[common:] in _ENDINGS and cand[common:] in _ENDINGS:
            return word
        fixes.append(Correction(word, cand, "fuzzy", best[1]))
        return cand
    text = re.sub(r"(?<![А-Яа-яЁё])[А-Яа-яЁё][а-яё-]+", repl, text)
    return text, fixes
