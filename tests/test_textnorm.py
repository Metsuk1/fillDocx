"""Нормализация текста на примерах из RULES_1_51 (номера правил в именах тестов)."""

import datetime as dt

import pytest

from filldocx.textnorm import normalize_text, words_to_digits
from filldocx.textnorm.dates import find_birth_date
from filldocx.textnorm.formatters import fmt_density, fmt_size
from filldocx.textnorm.terms import Lexicon, correct_terms

TODAY = dt.date(2026, 11, 26)


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("сто семьдесят три запятая четыре", "173,4"),
        ("пятьдесят четыре запятая пять", "54,5"),
        ("десять запятая ноль", "10,0"),
        ("десять запятая ноль пять", "10,05"),
        ("пять пять", "5 5"),
        ("двадцать пять", "25"),
        ("сто два", "102"),
        ("две тысячи двадцать шесть", "2026"),
        ("тысяча девятьсот шестьдесят четыре", "1964"),
        ("ноль четыре тысяча девятьсот шестьдесят четыре", "0 4 1964"),
        ("минус один", "-1"),
        ("173 запятая 4", "173,4"),
        ("размер 10-12", "размер 10-12"),
    ],
)
def test_words_to_digits(spoken, expected):
    assert words_to_digits(spoken) == expected


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("Нуль один нуль четыре тысяча девятьсот шестьдесят четыре", "01.04.1964"),
        ("Нуль нуль один нуль четыре тысяча девятьсот шестьдесят четыре", "01.04.1964"),
        ("12 07 57 года", "12.07.1957"),
        ("05 12 58 года", "05.12.1958"),
        ("15 10 76 года", "15.10.1976"),
        ("двенадцать ноль семь пятьдесят седьмого года", "12.07.1957"),
        ("дата рождения пятнадцать десять семьдесят шесть", "15.10.1976"),
    ],
)
def test_rule08_birth_date(spoken, expected):
    res = find_birth_date(spoken, TODAY)
    assert res.value == expected, res.notes
    assert not res.ambiguous


@pytest.mark.parametrize("spoken", ["03 05 10 года", "31 02 1960", "дата рождения"])
def test_rule08_birth_date_not_guessed(spoken):
    res = find_birth_date(spoken, TODAY)
    assert res.value is None
    assert res.ambiguous


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("плотность один", "плотность +1 ед.Н"),
        ("плотность минус один", "плотность -1 ед.Н"),
        ("плотность шестьдесят три", "плотность +63 ед.Н"),
        ("плотность пятьдесят четыре запятая пять", "плотность +54,5 ед.Н"),
        ("плотность ноль", "плотность 0 ед.Н"),
        ("плотностью восемь единиц хаунсфилда", "плотностью +8 ед.Н"),
        ("плотность паренхимы 63 ед.Н", "плотность паренхимы +63 ед.Н"),
    ],
)
def test_rule18_density(spoken, expected):
    assert normalize_text(spoken) == expected


def test_rule25_double_density():
    assert normalize_text("плотность паренхимы шестьдесят, шестьдесят три") == "плотность паренхимы +60, +63 ед.Н"
    assert (
        normalize_text("денситометрические показатели в пределах тридцать семь, тридцать девять")
        == "денситометрические показатели в пределах +37, +39 ед.Н"
    )
    assert fmt_density(["60", "63"]) == "+60, +63 ед.Н"
    assert fmt_density(["0"]) == "0 ед.Н"
    assert fmt_density(["-1"]) == "-1 ед.Н"


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("размер 173,4", "размер 173,4мм"),
        ("шириной 10,0", "шириной 10,0мм"),
        ("45,5 на 45,4", "45,5х45,4мм"),
        ("45,5 на 45,4 и третий размер 35,5мм", "45,5х45,4х35,5мм"),
        ("20 на 35 на 42", "20х35х42мм"),
        ("размерами 29,6 на 28,1 миллиметров", "размерами 29,6х28,1мм"),
        ("холедох шириной девять запятая ноль", "холедох шириной 9,0мм"),
    ],
)
def test_rule23_sizes(spoken, expected):
    assert normalize_text(spoken) == expected


def test_rule23_size_slot_format():
    assert fmt_size(["173,4"]) == "173,4мм"
    assert fmt_size(["45.5", "45,4"]) == "45,5х45,4мм"


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("не исключается заболевание в скобочках с р под вопросом", "не исключается заболевание (c-r?)"),
        ("с р под вопросом", "c-r?"),
        ("мтс характера", "mts характера"),
        ("в скобках мтс", "(mts)"),
        ("в скобках мтс под вопросом", "(mts?)"),
        ("не визуализируется в скобках холецистэктомия", "не визуализируется (холецистэктомия)"),
        ("с ровными контурами", "с ровными контурами"),  # «с» — предлог, не c-r
    ],
)
def test_rule51_commands(spoken, expected):
    assert normalize_text(spoken) == expected


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("в пятом сегменте печени", "в V сегменте печени"),
        ("в седьмом сегменте", "в VII сегменте"),
        ("третий желудочек шириной 11,4", "III – желудочек шириной 11,4мм"),
        ("четвертый желудочек не расширен", "IV – желудочек не расширен"),
    ],
)
def test_roman_numerals(spoken, expected):
    assert normalize_text(spoken) == expected


def test_vertebrae():
    out = normalize_text("смещение тела л пять позвонка кпереди на пять миллиметров по отношению к телу с один позвонка")
    assert "L5 позвонка" in out and "S1 позвонка" in out and "на 5мм" in out
    assert "Th12" in normalize_text("тело т двенадцать позвонка")
    assert normalize_text("с 2 до 5 часов") == "с 2 до 5 часов"  # без контекста позвоночника не трогаем


@pytest.mark.parametrize(
    "text",
    [
        "плотность паренхимы +63 ед.Н",
        "размерами 45,5х45,4х35,5мм",
        "не исключается заболевание (c-r?)",
        "в V сегменте печени",
        "III – желудочек шириной 11,4мм",
        "воротная вена шириной 10,0мм, селезеночная вена 9,0мм",
    ],
)
def test_normalize_is_idempotent(text):
    assert normalize_text(normalize_text(text)) == normalize_text(text)


def test_term_corrections_phrase_and_fuzzy():
    lex = Lexicon(phrases={"окно кодальный": "кранио-каудальный"}, terms={"паренхимы", "остеопороз"})
    text, fixes = correct_terms("окно кодальный размер, плотность паронхимы", lex)
    assert text == "кранио-каудальный размер, плотность паренхимы"
    assert {f.kind for f in fixes} == {"phrase", "fuzzy"}


def test_term_corrections_leave_short_and_unknown_words():
    lex = Lexicon(terms={"паренхимы"})
    text, fixes = correct_terms("печень обычной формы", lex)
    assert text == "печень обычной формы" and not fixes
