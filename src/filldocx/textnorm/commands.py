"""Команды форматирования в диктовке (правила 28, 51).

«в скобках холецистэктомия»            → «(холецистэктомия)»
«в скобочках с р под вопросом»         → «(c-r?)»
«с р под вопросом»                     → «c-r?»
«мтс характера»                        → «mts характера»
«в скобках мтс под вопросом»           → «(mts?)»
Слова «в скобках», «под вопросом» в готовый текст не переносятся.
"""

from __future__ import annotations

import re

# аббревиатура (как может прийти из ASR) → как пишется в протоколе
_ABBREV = [
    (re.compile(r"\b(?:с|ц|си|c)\s*-?\s*(?:р|эр|r)\b(?=\s*(?:под\s+вопросом|\)|$|[.,;]))", re.I), "c-r"),
    (re.compile(r"\b(?:с|ц|си|c)\s*-?\s*(?:р|эр|r)\b", re.I), "c-r"),
    (re.compile(r"\b(?:мтс|мэтээс|м\s*т\s*с|mts)\b", re.I), "mts"),
]

# «с р» ловим только в медицинском контексте: после «заболевание», «скобках»
# или перед «под вопросом», иначе «с» — это предлог.
_CR_CONTEXT = re.compile(
    r"(заболевани\w*|скобк\w*|скобочк\w*)\s+(?:с|ц|си|c)\s*-?\s*(?:р|эр|r)\b|"
    r"\b(?:с|ц|си|c)\s*-?\s*(?:р|эр|r)\s+под\s+вопросом",
    re.I,
)

_BRACKET_RE = re.compile(
    r"\s*\bв\s+скоб(?:ках|очках|ке|очке)\s+([A-Za-zА-Яа-яЁё][\w-]*)(\s+под\s+вопросом)?(?:\s+закрыть\s+скобк\w*)?",
    re.I,
)
_QUESTION_RE = re.compile(r"\b(c-r|mts)\s+под\s+вопросом", re.I)


_UPPER_ABBR = re.compile(
    r"(?<![А-Яа-яЁё])(кт|мрт|мскт|узи|фгдс|фкс|экг|эхокг|чмт|омт|обп|огк|ппн|чдс|члс|оакр)(?![А-Яа-яЁё])",
    re.I,
)


def apply_commands(text: str) -> str:
    text = _UPPER_ABBR.sub(lambda m: m.group(1).upper(), text)
    def cr_fix(m: re.Match) -> str:
        s = m.group(0)
        return re.sub(r"(?:с|ц|си|c)\s*-?\s*(?:р|эр|r)\b", "c-r", s, count=1, flags=re.I)

    text = _CR_CONTEXT.sub(cr_fix, text)
    text = _ABBREV[2][0].sub("mts", text)

    def bracket(m: re.Match) -> str:
        word = m.group(1)
        q = "?" if m.group(2) else ""
        return f" ({word}{q})"

    text = _BRACKET_RE.sub(bracket, text)
    text = _QUESTION_RE.sub(lambda m: f"{m.group(1)}?", text)
    text = re.sub(r"\s+\)", ")", text)
    # продиктованные знаки препинания («…в диаметре точка в малом тазу…»). «запятая» между
    # числами к этому моменту уже стала десятичной запятой (words_to_digits).
    text = _DOT_RE.sub(".", text)
    text = _COMMA_RE.sub(",", text)
    text = re.sub(r"([.,])(?:\s*[.,])+", r"\1", text)
    return text


_DOT_RE = re.compile(r"\s*(?<![А-Яа-яЁё])(?:точка|тачка)(?![А-Яа-яЁё])(?!\s+зрения)", re.I)
_COMMA_RE = re.compile(r"\s*(?<![А-Яа-яЁё])запятая(?![А-Яа-яЁё])", re.I)
