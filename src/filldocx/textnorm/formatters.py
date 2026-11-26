"""Оформление чисел и обозначений по правилам 7, 18, 23, 25 и общим правилам §8.

Все функции идемпотентны: повторный вызов не меняет результат.
"""

from __future__ import annotations

import re

NUM = r"\d+(?:,\d+)?"
_SNUM = r"[-+]?\d+(?:,\d+)?"

# --- единицы измерения -------------------------------------------------------

_HU_RE = re.compile(
    r"\s*(?<![а-яёa-z])(?:единиц[аы]?\s+(?:х[ао]у?н?сфилд\w*|хаусфилд\w*|ха[уо]нсфилд\w*|h|н)\b|"
    # искажения распознавания: «хаунс филда», «хам свято», «хан силы»
    r"единиц[аы]?\s+х[ао]\w*\s+(?:ф\w*л\w*|свят\w*|сил\w*)|"
    r"ед\.?\s*[нhH](?![а-яёa-z])\.?|hu\b)",
    re.I,
)
# «метров» после числа — искажение «миллиметров»: в протоколах КТ метров не бывает
_MM_RE = re.compile(r"(\d)\s*(?:миллиметр\w*|метр(?:а|ов)?\b|мм\b\.?|mm\b)", re.I)


def normalize_units(text: str) -> str:
    """«173,4 миллиметра» → «173,4мм»; «63 единицы Хаунсфилда» → «63 ед.Н»."""
    text = _HU_RE.sub(" ед.Н", text)
    text = re.sub(r"(ед\.Н)(?:\s*ед\.Н)+", r"\1", text)
    text = _MM_RE.sub(r"\1мм", text)
    text = re.sub(r"мм(?:\s*мм)+", "мм", text)
    return text


# --- размеры (правило 23) ----------------------------------------------------

_DIM_RE = re.compile(
    rf"(?<![\d,])({NUM})(?:мм)?\s*(?:на|x|х|×|\*)\s*({NUM})(?:мм)?"
    rf"(?:\s*(?:,\s*)?(?:на|x|х|×|\*|и\s+третий\s+размер|третий\s+размер)\s*({NUM}))?(?:\s*мм)?",
    re.I,
)

_SIZE_KEYWORDS = (
    r"(?:размер\w*|ширин\w*|толщин\w*|диаметр\w*|длин\w*|составляет|на\s+уровне\s+до|"
    r"вен[аы]|холедох\w*|ветвь|ветви|ствол\w*)"
)
_SIZE_SINGLE_RE = re.compile(
    rf"({_SIZE_KEYWORDS}(?:\s+(?:около|до|не\s+более|примерно|в\s+диаметре|правой\s+доли\s+печени|составляет))*\s+)({NUM})(?!\s*(?:мм|см|ед|%|[,х]\s*\d|\d))",
    re.I,
)


def format_dimensions(text: str) -> str:
    """«45,5 на 45,4 и третий размер 35,5мм» → «45,5х45,4х35,5мм»."""
    def repl(m: re.Match) -> str:
        parts = [p for p in m.groups() if p]
        return "х".join(parts) + "мм"
    text = _DIM_RE.sub(repl, text)
    text = _SIZE_SINGLE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}мм", text)
    return text


def fmt_size(values: list[str]) -> str:
    """Значения слота размера → «173,4мм» / «45,5х45,4мм»."""
    clean = [v.replace(".", ",").lstrip("+") for v in values]
    return "х".join(clean) + "мм"


# --- плотности (правила 18, 19, 25) ------------------------------------------

def _signed(v: str) -> str:
    v = v.replace(".", ",")
    if v.startswith(("+", "-")):
        return v
    if re.fullmatch(r"0+(?:,0+)?", v):
        return v
    return "+" + v


def fmt_density(values: list[str]) -> str:
    """[«60», «63»] → «+60, +63 ед.Н». Ноль без знака. Среднее не считается."""
    return ", ".join(_signed(v) for v in values) + " ед.Н"


_DENS_LIST = rf"{_SNUM}(?:\s*,\s*{_SNUM}|\s+[-+]\d+(?:,\d+)?)*"
_DENS_AFTER_WORD_RE = re.compile(
    rf"((?:плотност\w*|денситометрическ\w+\s+показател\w*)(?:\s+(?:паренхимы|печени|селез[её]нки|образования|в\s+пределах|составляет|до|около|кисты|содержимого)){{0,4}}\s+)({_DENS_LIST})(?!\s*(?:мм|х\s*\d|\d))(\s*ед\.Н)?",
    re.I,
)
_DENS_BEFORE_HU_RE = re.compile(rf"(?<![\d,.+-])({_DENS_LIST})(\s*ед\.Н)")


def format_densities(text: str) -> str:
    """Знак «+» у положительных плотностей и единица «ед.Н» (без дублей)."""
    def sign_list(s: str) -> str:
        return ", ".join(_signed(v) for v in re.findall(_SNUM, s))

    text = _DENS_AFTER_WORD_RE.sub(lambda m: f"{m.group(1)}{sign_list(m.group(2))} ед.Н", text)
    text = _DENS_BEFORE_HU_RE.sub(lambda m: f"{sign_list(m.group(1))}{m.group(2)}", text)
    text = re.sub(r"(ед\.Н)(?:\s*ед\.Н)+", r"\1", text)
    return text


# --- римские цифры (§8, правило 7, 26) ----------------------------------------

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]

_ORD_STEMS = [
    (1, r"перв"), (2, r"втор"), (3, r"трет"), (4, r"четв[её]рт"), (5, r"пят"),
    (6, r"шест"), (7, r"седьм"), (8, r"восьм"), (9, r"девят"), (10, r"десят"),
]
_ORD_END = r"(?:ый|ий|ой|ая|ое|ого|ому|ым|ом|ье|ьего|ьему|ьим|ьем|ья|ью|ей|ую|ые|ых)"
_ROMAN_NOUNS = r"(?:сегмент\w*|желудоч\w*)"


def to_roman_numerals(text: str, ventricle_dash: bool = True) -> str:
    """«в пятом сегменте» → «в V сегменте»; «третий желудочек» → «III – желудочек»."""
    for n, stem in _ORD_STEMS:
        text = re.sub(rf"\b{stem}{_ORD_END}\s+({_ROMAN_NOUNS})", rf"{ROMAN[n]} \1", text, flags=re.I)
    text = re.sub(
        rf"\b(10|[1-9])\s+({_ROMAN_NOUNS})",
        lambda m: f"{ROMAN[int(m.group(1))]} {m.group(2)}",
        text,
        flags=re.I,
    )
    if ventricle_dash:
        text = re.sub(r"\b(III|IV)\s+(?:–\s+)?(желудоч\w*)", r"\1 – \2", text)
    return text


# --- позвонки (§8) -----------------------------------------------------------

_VERT_LETTERS = {
    "C": r"(?:це|ц|c|си)",
    "Th": r"(?:th|тх|т|те|тэ)",
    "L": r"(?:эль|л|l)",
    "S": r"(?:эс|s)",
}
_VERT_MAX = {"C": 7, "Th": 12, "L": 5, "S": 5}
_VERT_CONTEXT = re.compile(r"позвон|тел[оаеу]?\b|тел\b|сегмент|уров|диск|сочлен|L\d|Th\d|S\d|C\d", re.I)


def format_vertebrae(text: str) -> str:
    """«л 5» → «L5», «т 12» → «Th12», «эс 1» → «S1» (только рядом со словами о позвоночнике)."""
    def make(prefix: str):
        pat = re.compile(rf"(?<![А-Яа-яЁёA-Za-z]){_VERT_LETTERS[prefix]}\s*-?\s*(\d{{1,2}})(?!\d|,\d)", re.I)

        def repl(m: re.Match) -> str:
            n = int(m.group(1))
            if not 1 <= n <= _VERT_MAX[prefix]:
                return m.group(0)
            window = text[max(0, m.start() - 40): m.end() + 40]
            if not _VERT_CONTEXT.search(window):
                return m.group(0)
            return f"{prefix}{n}"
        return pat, repl

    for prefix in ("Th", "L", "S", "C"):
        pat, repl = make(prefix)
        text = pat.sub(repl, text)
    # кириллическая «с» + цифра: S1 после L5, иначе шейный C (правило 5 «тела С2 позвонка»)
    def cyr_s(m: re.Match) -> str:
        n = int(m.group(1))
        before = text[max(0, m.start() - 80): m.start()]
        if n == 1 and re.search(r"L5\b", before):
            return "S1"
        window = text[max(0, m.start() - 40): m.end() + 40]
        if 1 <= n <= 7 and _VERT_CONTEXT.search(window):
            return f"C{n}"
        return m.group(0)
    text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])с\s*-?\s*(\d)(?!\d|,\d)", cyr_s, text)
    return text
