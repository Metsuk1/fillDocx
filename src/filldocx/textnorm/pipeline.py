"""Полная детерминированная нормализация фразы диктовки."""

from __future__ import annotations

import re

from .commands import apply_commands
from .formatters import (
    format_densities,
    format_dimensions,
    format_vertebrae,
    normalize_units,
    to_roman_numerals,
)
from .numbers import words_to_digits


def normalize_text(text: str, ventricle_dash: bool = True) -> str:
    """Числа → цифры, команды скобок, единицы, размеры, плотности, римские цифры, позвонки.

    Функция идемпотентна: normalize_text(normalize_text(x)) == normalize_text(x).
    """
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = words_to_digits(text)
    text = apply_commands(text)
    text = normalize_units(text)
    text = format_dimensions(text)
    text = format_densities(text)
    text = to_roman_numerals(text, ventricle_dash=ventricle_dash)
    text = format_vertebrae(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([.,;:!?)])", r"\1", text)
    return text.strip()


def capitalize_sentences(text: str) -> str:
    """Заглавная буква в начале каждого предложения, точка в конце."""
    text = text.strip()
    if not text:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [p[:1].upper() + p[1:] if p else p for p in parts]
    out = " ".join(parts)
    if not out.endswith((".", "!", "?")):
        out += "."
    return out
