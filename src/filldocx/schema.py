"""Структуры данных между шагами конвейера."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .templates import TemplateMap


# --- выход LLM ---------------------------------------------------------------

class SlotValue(BaseModel):
    slot: str
    values: list[str]                      # цифрами, как продиктовано: ["173,4"], ["60", "63"], ["45,5", "45,4"]


class Finding(BaseModel):
    organ: str                             # id органа из карты шаблона
    status: Literal["norm", "changed", "absent"]
    text: str = ""                         # формулировка для протокола (для norm — пусто)
    slots: list[SlotValue] = Field(default_factory=list)
    quote: str                             # дословный фрагмент диктовки, на котором основан факт


class Extraction(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    conclusion: str | None = None          # восстановленный текст заключения
    offtopic: list[str] = Field(default_factory=list)
    unclear: list[str] = Field(default_factory=list)


def extraction_json_schema(tmpl: TemplateMap) -> dict:
    """JSON-схема ответа LLM с перечислением органов и слотов именно этого шаблона.

    Ограничение схемой не даёт модели придумать орган или поле, которых нет в шаблоне.
    """
    slot_names = sorted({s for o in tmpl.organs for s in o.slots}) or ["none"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings", "conclusion", "offtopic", "unclear"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["organ", "status", "text", "slots", "quote"],
                    "properties": {
                        "organ": {"type": "string", "enum": tmpl.organ_ids()},
                        "status": {"type": "string", "enum": ["norm", "changed", "absent"]},
                        "text": {"type": "string"},
                        "slots": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["slot", "values"],
                                "properties": {
                                    "slot": {"type": "string", "enum": slot_names},
                                    "values": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "quote": {"type": "string"},
                    },
                },
            },
            "conclusion": {"type": ["string", "null"]},
            "offtopic": {"type": "array", "items": {"type": "string"}},
            "unclear": {"type": "array", "items": {"type": "string"}},
        },
    }


# --- проверки и журнал -------------------------------------------------------

class Issue(BaseModel):
    """Спорное место. Показывается ТОЛЬКО в интерфейсе, в Word не попадает."""
    level: Literal["info", "warning", "error"]
    code: str
    message: str
    organ: str | None = None
    quote: str | None = None
    rule: int | None = None
    t_start: float | None = None
    t_end: float | None = None


class VerifiedOrgan(BaseModel):
    organ: str
    status: Literal["norm", "changed", "absent"]
    texts: list[str] = Field(default_factory=list)
    slots: dict[str, list[str]] = Field(default_factory=dict)
    quotes: list[str] = Field(default_factory=list)


# --- операции над документом -------------------------------------------------

class Segment(BaseModel):
    text: str
    bold: bool = False
    italic: bool = False


class FillSlot(BaseModel):
    op: Literal["fill_slot"] = "fill_slot"
    anchors: list[str]
    before: str
    after: str
    value: str                             # то, что встаёт между before и after, с пробелами
    organ: str
    rules: list[int] = Field(default_factory=list)
    quote: str = ""


class InsertParagraph(BaseModel):
    op: Literal["insert_after"] = "insert_after"
    host_anchors: list[str]                # абзац шаблона, под которым встаёт новый
    order: float                           # порядок среди вставок под одним абзацем
    segments: list[Segment]
    own_paragraph: bool = True
    align: Literal["left", "center", "both"] | None = None
    organ: str
    rules: list[int] = Field(default_factory=list)
    quote: str = ""


class SetField(BaseModel):
    op: Literal["set_field"] = "set_field"
    label: str
    value: str
    rules: list[int] = Field(default_factory=list)


Operation = FillSlot | InsertParagraph | SetField
