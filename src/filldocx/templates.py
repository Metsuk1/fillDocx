"""Карты шаблонов (config/templates/*.yaml) и определение типа исследования."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Slot(BaseModel):
    kind: Literal["size", "density"]
    about: str = ""
    before: str | None = None   # текст шаблона перед местом значения (заполнение «на месте»)
    after: str | None = None    # текст шаблона после значения («мм», «ед.Н»)
    rules: list[int] = Field(default_factory=list)

    @property
    def inline(self) -> bool:
        return bool(self.before and self.after)


class Rewrite(BaseModel):
    pattern: str
    replace: str
    rule: int | None = None


class ClauseRule(BaseModel):
    match: str
    out: str
    rule: int | None = None


class Organ(BaseModel):
    id: str
    name: str
    anchor: str | None = None
    alt_anchors: list[str] = Field(default_factory=list)
    norm: str | None = None
    slots: dict[str, Slot] = Field(default_factory=dict)
    formed: list[str] = Field(default_factory=list)
    rewrites: list[Rewrite] = Field(default_factory=list)
    clause_rules: list[ClauseRule] = Field(default_factory=list)
    italic_prefixes: list[str] = Field(default_factory=list)
    virtual: bool = False
    place_after: str | None = None
    own_paragraph: bool = False
    contrast_only: bool = False
    notes: str = ""

    def all_anchors(self) -> list[str]:
        return [a for a in [self.anchor, *self.alt_anchors] if a]


class Hint(BaseModel):
    when: str
    suggest: str
    rule: int | None = None


class ConclusionSpec(BaseModel):
    anchor: str = "Заключение:"
    label: str = "Заключение:"
    hints: list[Hint] = Field(default_factory=list)


class RecommendationsSpec(BaseModel):
    default_label: str = "Рекомендовано:"


class MatchSpec(BaseModel):
    filename: list[str] = Field(default_factory=list)
    heading: list[str] = Field(default_factory=list)


class TemplateMap(BaseModel):
    id: str
    title: str
    match: MatchSpec = Field(default_factory=MatchSpec)
    fields: dict[str, str] = Field(default_factory=dict)
    content_start: str | None = None
    style: dict[str, bool] = Field(default_factory=dict)
    organs: list[Organ]
    conclusion: ConclusionSpec = Field(default_factory=ConclusionSpec)
    recommendations: RecommendationsSpec = Field(default_factory=RecommendationsSpec)

    def organ(self, organ_id: str) -> Organ | None:
        return next((o for o in self.organs if o.id == organ_id), None)

    def organ_ids(self) -> list[str]:
        return [o.id for o in self.organs]

    def order(self, organ_id: str) -> int:
        ids = self.organ_ids()
        return ids.index(organ_id) if organ_id in ids else len(ids)

    def host_organ(self, organ_id: str) -> Organ | None:
        """Для виртуального органа — ближайший предшествующий орган с якорем в шаблоне."""
        org = self.organ(organ_id)
        seen: set[str] = set()
        while org is not None and org.virtual and org.place_after and org.id not in seen:
            seen.add(org.id)
            org = self.organ(org.place_after)
        return org if org is not None and not org.virtual else None


def load_template(path: str | Path) -> TemplateMap:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TemplateMap.model_validate(data)


def load_all_templates(templates_dir: str | Path) -> dict[str, TemplateMap]:
    out: dict[str, TemplateMap] = {}
    for p in sorted(Path(templates_dir).glob("*.yaml")):
        t = load_template(p)
        out[t.id] = t
    return out


_LOOKALIKES = str.maketrans({"a": "а", "c": "с", "e": "е", "k": "к", "o": "о", "p": "р", "t": "т",
                             "x": "х", "y": "у", "m": "м", "h": "н", "b": "в"})


def _norm_name(s: str) -> str:
    """Регистр, ё, латинские буквы-двойники → кириллица («kt обп C+» = «кт обп С+»)."""
    return s.lower().replace("ё", "е").translate(_LOOKALIKES)


def detect_template(templates: dict[str, TemplateMap], filename: str, heading: str = "") -> TemplateMap | None:
    """Тип исследования по имени файла, затем по заголовку документа."""
    name = _norm_name(Path(filename).stem)
    for t in templates.values():
        if any(re.search(p, name, re.I) for p in t.match.filename):
            return t
    head = _norm_name(heading)
    for t in templates.values():
        if head and any(re.search(p, head, re.I) for p in t.match.heading):
            return t
    return None


_CONTRAST_MARKS = re.compile(r"(с\+|\+с|контр|с\s*контраст|\+)", re.I)


def contrast_from_filename(filename: str) -> bool:
    """Признак контраста по названию (С+, С+++, +С, контр, +контр). Кириллица/латиница не важны."""
    return bool(_CONTRAST_MARKS.search(_norm_name(Path(filename).stem)))
