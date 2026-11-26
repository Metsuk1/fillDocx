"""Точечное редактирование Word-шаблона без пересборки документа.

- Документ открывается и сохраняется целиком через python-docx: шапка, логотипы,
  колонтитулы, стили, служебные поля остаются как есть.
- Значения вставляются новыми run-ами с форматом соседнего текста (Times New Roman 14)
  и красным цветом. Соседние run-ы не меняются.
- Новые абзацы копируют свойства абзаца шаблона, под которым встают.
- В документ не пишется ничего служебного: ни предупреждений, ни расшифровки.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph
from rapidfuzz import fuzz

from .schema import FillSlot, InsertParagraph, Issue, Operation, Segment, SetField


def anchor_regex(text: str) -> re.Pattern:
    """Регулярное выражение для поиска фразы шаблона, терпимое к пробелам, ё/е, кавычкам, тире."""
    out = []
    for ch in text.strip():
        if ch.isspace():
            if not out or out[-1] != r"\s*":
                out.append(r"\s*")
        elif ch in "её":
            out.append("[её]")
        elif ch in "ЕЁ":
            out.append("[ЕЁ]")
        elif ch in "«»\"“”„":
            out.append("[«»\"“”„]")
        elif ch in "-–—":
            out.append("[-–—]")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out), re.I)


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[«»\"“”„]", '"', s)
    s = re.sub(r"[-–—]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class ApplyResult:
    applied: list[Operation] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class DocxEditor:
    def __init__(self, path: str | Path, red_color: str = "FF0000",
                 font_name: str = "Times New Roman", font_size_pt: float = 14):
        self.path = Path(path)
        self.doc = Document(str(path))
        self.red = red_color
        self.font_name = font_name
        self.font_size_half_pt = str(int(font_size_pt * 2))

    # --- чтение -------------------------------------------------------------

    @property
    def paragraphs(self) -> list[Paragraph]:
        return self.doc.paragraphs

    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)

    def heading(self, max_pars: int = 15) -> str:
        return " ".join(p.text for p in self.paragraphs[:max_pars])

    def content_start(self, marker: str | None) -> int:
        """Индекс первого абзаца медицинской части (после «Информированное согласие…»)."""
        if not marker:
            return 0
        rx = anchor_regex(marker)
        for i, p in enumerate(self.paragraphs):
            if rx.search(p.text):
                return i + 1
        return 0

    def find_paragraph(self, anchors: list[str], start: int = 0) -> tuple[int | None, float]:
        """Абзац, где встречается якорь. Точное совпадение, иначе нечёткое (>= 90)."""
        pars = self.paragraphs
        for a in anchors:
            rx = anchor_regex(a)
            for i in range(start, len(pars)):
                if rx.search(pars[i].text):
                    return i, 100.0
        best_i, best_s = None, 0.0
        for a in anchors:
            na = _norm(a)
            for i in range(start, len(pars)):
                s = fuzz.partial_ratio(na, _norm(pars[i].text)) if len(pars[i].text) >= len(a) else 0
                if s > best_s:
                    best_i, best_s = i, s
        return (best_i, best_s) if best_s >= 90 else (None, best_s)

    def read_field(self, label: str) -> str | None:
        rx = re.compile(r"^\s*" + anchor_regex(label).pattern, re.I)
        for p in self.paragraphs:
            m = rx.search(p.text)
            if m:
                return p.text[m.end():].strip()
        return None

    # --- низкоуровневые операции над run-ами ---------------------------------

    @staticmethod
    def _runs_with_offsets(p: Paragraph) -> list[tuple[object, int, int]]:
        out, pos = [], 0
        for r in p.runs:
            t = r.text
            out.append((r, pos, pos + len(t)))
            pos += len(t)
        return out

    @staticmethod
    def _split_run(run, k: int):
        """Делит run на два по смещению k внутри его текста; возвращает правую часть."""
        text = run.text
        new_r = copy.deepcopy(run._r)
        run._r.addnext(new_r)
        from docx.text.run import Run
        right = Run(new_r, run._parent)
        run.text = text[:k]
        right.text = text[k:]
        return right

    def _isolate_range(self, p: Paragraph, start: int, end: int):
        """Режет run-ы так, чтобы [start, end) покрывался целыми run-ами. Возвращает их список."""
        for boundary in (start, end):
            for r, s, e in self._runs_with_offsets(p):
                if s < boundary < e:
                    self._split_run(r, boundary - s)
                    break
        return [r for r, s, e in self._runs_with_offsets(p) if s >= start and e <= end and e > s]

    def _style_run(self, r_el, *, bold: bool | None = None, italic: bool | None = None, red: bool = True):
        rpr = r_el.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            r_el.insert(0, rpr)
        for tag in ("w:highlight", "w:shd"):
            for el in rpr.findall(qn(tag)):
                rpr.remove(el)
        if bold is not None:
            for tag in ("w:b", "w:bCs"):
                for el in rpr.findall(qn(tag)):
                    rpr.remove(el)
            if bold:
                rpr.append(OxmlElement("w:b"))
        if italic is not None:
            for tag in ("w:i", "w:iCs"):
                for el in rpr.findall(qn(tag)):
                    rpr.remove(el)
            if italic:
                rpr.append(OxmlElement("w:i"))
        if red:
            for el in rpr.findall(qn("w:color")):
                rpr.remove(el)
            color = OxmlElement("w:color")
            color.set(qn("w:val"), self.red)
            rpr.append(color)
        self._ensure_font(rpr)
        _sort_rpr(rpr)

    def _ensure_font(self, rpr):
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.insert(0, fonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs"):
            fonts.set(qn(attr), self.font_name)
        for tag in ("w:sz", "w:szCs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rpr.append(el)
            el.set(qn("w:val"), self.font_size_half_pt)

    def replace_range(self, p: Paragraph, start: int, end: int, new_text: str, red: bool = True):
        """Заменяет символы [start, end) абзаца на new_text (новый красный run)."""
        runs = self._runs_with_offsets(p)
        if not runs:
            return
        # образец формата — run, в котором стоит начало диапазона (или предыдущий)
        proto = None
        for r, s, e in runs:
            if s <= max(start - 1, 0) < e:
                proto = r
                break
        proto = proto or runs[0][0]
        inside = self._isolate_range(p, start, end) if end > start else []
        new_r = copy.deepcopy(proto._r)
        for child in list(new_r):
            if child.tag != qn("w:rPr"):
                new_r.remove(child)
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = new_text
        new_r.append(t)
        self._style_run(new_r, red=red)
        if inside:
            inside[0]._r.addprevious(new_r)
            for r in inside:
                r._r.getparent().remove(r._r)
        elif start == 0:
            runs[0][0]._r.addprevious(new_r)
        else:
            # пустой диапазон: вставка сразу после символа start-1
            for r, s, e in self._runs_with_offsets(p):
                if s < start <= e:
                    if start < e:
                        self._split_run(r, start - s)
                    r._r.addnext(new_r)
                    break

    def delete_range(self, p: Paragraph, start: int, end: int):
        """Удаляет символы [start, end) абзаца, не трогая форматирование остальных run-ов."""
        if end <= start:
            return
        for r in self._isolate_range(p, start, end):
            r._r.getparent().remove(r._r)

    @staticmethod
    def delete_paragraph(p: Paragraph):
        p._p.getparent().remove(p._p)

    def insert_paragraph_after(self, anchor_p, segments: list[Segment], align: str | None = None):
        """Новый абзац после anchor_p (Paragraph или элемент w:p). Свойства абзаца — от шаблона."""
        ref_p = anchor_p._p if isinstance(anchor_p, Paragraph) else anchor_p
        new_p = copy.deepcopy(ref_p)
        for child in list(new_p):
            if child.tag != qn("w:pPr"):
                new_p.remove(child)
        # образец run-а — первый run шаблонного абзаца с текстом
        proto = None
        for r in ref_p.iter(qn("w:r")):
            if "".join(t.text or "" for t in r.iter(qn("w:t"))).strip():
                proto = r
                break
        for seg in segments:
            r = copy.deepcopy(proto) if proto is not None else OxmlElement("w:r")
            for child in list(r):
                if child.tag != qn("w:rPr"):
                    r.remove(child)
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = seg.text
            r.append(t)
            self._style_run(r, bold=seg.bold, italic=seg.italic, red=True)
            new_p.append(r)
        ref_p.addnext(new_p)
        para = Paragraph(new_p, anchor_p._parent if isinstance(anchor_p, Paragraph) else None)
        if align:
            para.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "left": WD_ALIGN_PARAGRAPH.LEFT,
                              "both": WD_ALIGN_PARAGRAPH.JUSTIFY}[align]
        return new_p

    # --- операции ------------------------------------------------------------

    def apply(self, ops: list[Operation], content_marker: str | None = None) -> ApplyResult:
        res = ApplyResult()
        start = self.content_start(content_marker)

        for op in [o for o in ops if isinstance(o, SetField)]:
            self._apply_set_field(op, res)
        for op in [o for o in ops if isinstance(o, FillSlot)]:
            self._apply_fill_slot(op, start, res)

        # вставки группируются по абзацу шаблона и идут в порядке органов
        groups: dict[int, list[InsertParagraph]] = {}
        for op in [o for o in ops if isinstance(o, InsertParagraph)]:
            idx, score = self.find_paragraph(op.host_anchors, start)
            if idx is None:
                res.issues.append(Issue(level="error", code="anchor_not_found", organ=op.organ, quote=op.quote,
                                        message=f"Не найден абзац шаблона «{op.host_anchors[0]}» — текст не вписан"))
                continue
            if score < 100:
                res.issues.append(Issue(level="warning", code="anchor_fuzzy", organ=op.organ,
                                        message=f"Абзац «{op.host_anchors[0]}» найден приблизительно ({score:.0f}%) — проверьте место"))
            groups.setdefault(idx, []).append(op)

        pars = self.paragraphs  # фиксируем ссылки до вставок
        for idx in sorted(groups, reverse=True):
            items = sorted(groups[idx], key=lambda o: o.order)
            # подряд идущие органы без own_paragraph сливаются в один абзац
            blocks: list[tuple[list[Segment], str | None]] = []
            buf: list[Segment] = []
            for op in items:
                if op.own_paragraph or op.align:
                    if buf:
                        blocks.append((buf, None))
                        buf = []
                    blocks.append((list(op.segments), op.align))
                else:
                    if buf:
                        buf.append(Segment(text=" "))
                    buf.extend(op.segments)
            if buf:
                blocks.append((buf, None))
            anchor_el = pars[idx]._p
            for segs, align in blocks:
                anchor_el = self.insert_paragraph_after(Paragraph(anchor_el, pars[idx]._parent), segs, align)
            res.applied.extend(items)
        return res

    def _apply_set_field(self, op: SetField, res: ApplyResult):
        rx = re.compile(r"^\s*" + anchor_regex(op.label).pattern, re.I)
        for p in self.paragraphs:
            m = rx.search(p.text)
            if not m:
                continue
            current = p.text[m.end():].strip(" _.\t")
            if current and current != op.value:
                res.issues.append(Issue(level="warning", code="field_not_empty",
                                        message=f"Поле «{op.label}» уже заполнено («{current}»), продиктовано «{op.value}» — не перезаписано"))
                return
            if current == op.value:
                return
            self.replace_range(p, m.end(), len(p.text), " " + op.value)
            res.applied.append(op)
            return
        res.issues.append(Issue(level="error", code="field_not_found", message=f"Поле «{op.label}» не найдено в документе"))

    def _apply_fill_slot(self, op: FillSlot, start: int, res: ApplyResult):
        idx, _ = self.find_paragraph(op.anchors, start)
        if idx is None:
            res.issues.append(Issue(level="error", code="anchor_not_found", organ=op.organ, quote=op.quote,
                                    message=f"Не найдено предложение «{op.anchors[0]}» — значение не вписано"))
            return
        p = self.paragraphs[idx]
        text = p.text
        a = None
        for anchor in op.anchors:
            a = anchor_regex(anchor).search(text)
            if a:
                break
        if a is None:
            res.issues.append(Issue(level="error", code="anchor_not_found", organ=op.organ,
                                    message=f"Предложение «{op.anchors[0]}» не найдено дословно — значение не вписано"))
            return
        sent_end = re.search(r"\.\s+(?=[А-ЯЁA-Z])", text[a.start():])
        limit = a.start() + sent_end.start() + 1 if sent_end else len(text)
        b = anchor_regex(op.before).search(text, a.start(), limit)
        if not b:
            res.issues.append(Issue(level="error", code="slot_not_found", organ=op.organ,
                                    message=f"В предложении «{op.anchors[0]}…» нет места «{op.before} … {op.after}» — значение не вписано"))
            return
        c = anchor_regex(op.after).search(text, b.end(), limit)
        if not c:
            res.issues.append(Issue(level="error", code="slot_not_found", organ=op.organ,
                                    message=f"После «{op.before}» нет «{op.after}» — значение не вписано"))
            return
        self.replace_range(p, b.end(), c.start(), op.value)
        res.applied.append(op)

    def save(self, path: str | Path | None = None):
        self.doc.save(str(path or self.path))


_RPR_ORDER = [
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike",
    "w:dstrike", "w:outline", "w:shadow", "w:emboss", "w:imprint", "w:noProof", "w:snapToGrid",
    "w:vanish", "w:webHidden", "w:color", "w:spacing", "w:w", "w:kern", "w:position", "w:sz",
    "w:szCs", "w:highlight", "w:u", "w:effect", "w:bdr", "w:shd", "w:fitText", "w:vertAlign",
    "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath",
]
_RPR_RANK = {qn(t): i for i, t in enumerate(_RPR_ORDER)}


def _sort_rpr(rpr):
    """Порядок дочерних элементов w:rPr по схеме OOXML — иначе Word может счесть файл повреждённым."""
    children = list(rpr)
    children.sort(key=lambda el: _RPR_RANK.get(el.tag, len(_RPR_RANK)))
    for el in children:
        rpr.remove(el)
    for el in children:
        rpr.append(el)
