"""Режим «правка шаблона»: точечная правка абзацев готового шаблона врача.

Шаблон врача — полный нормальный протокол, в котором красным выделены варианты
(холецистэктомия, стент, грыжи Шморля, «увеличена в размерах за счёт правой доли»…).
Ревьюер, слушая диктовку, раньше оставлял продиктованный красный текст (меняя в нём
только значения) и удалял непродиктованный. Здесь это делается так:

  1. LLM раскладывает диктовку по абзацам шаблона (дословные фрагменты, проверяются);
  2. абзац с фрагментами правит LLM, абзац без фрагментов — код (красное удаляется);
  3. проверка: каждое число нового текста есть в диктовке или в самом абзаце,
     новые слова подтверждаются диктовкой; иначе абзац не трогается и попадает в спорные;
  4. запись: разница «было → стало» по словам, точечно по run-ам. Нетронутый текст
     сохраняет свой цвет и формат, вписанное — красным, удалённое — удаляется.
"""

from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from rapidfuzz import fuzz

from .docx_writer import DocxEditor, anchor_regex
from .llm.edit_prompt import L, R
from .schema import Issue, Segment
from .textnorm import extract_numbers
from .verify import _stem, _words

_CONCL_RE = re.compile(r"^\s*Заключение\s*:", re.I)
_RECO_RE = re.compile(r"^\s*(Рекомендовано|Рекомендуется|Рекомендации|Необходимо|Показано)\s*:", re.I)


def _is_red(run) -> bool:
    rpr = run._r.rPr
    if rpr is None:
        return False
    c = rpr.find(qn("w:color"))
    if c is None:
        return False
    v = (c.get(qn("w:val")) or "").upper()
    if len(v) != 6 or v == "AUTO":
        return False
    try:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        return False
    return r >= 0xA0 and g <= 0x70 and b <= 0x70


@dataclass
class EditPar:
    """Абзац шаблона, подлежащий правке. Смещения — относительно ``text``."""
    p: Paragraph
    lead: int                                  # где ``text`` начинается в p.text
    text: str                                  # текст без ведущих/хвостовых пробелов (и без метки)
    red: list[tuple[int, int]] = field(default_factory=list)
    kind: str = "body"                         # body | conclusion | reco

    def marked(self) -> str:
        out, pos = [], 0
        for s, e in self.red:
            out.append(self.text[pos:s])
            out.append(L + self.text[s:e] + R)
            pos = e
        out.append(self.text[pos:])
        return "".join(out)

    def black_text(self) -> str:
        return cleanup(self.text, self.red)

    def summary(self, words: int = 6) -> str:
        """Начала предложений абзаца — по ним LLM узнаёт, о чём абзац."""
        sents = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«(])", self.text)
        heads = []
        for s in sents:
            w = s.split()
            if w:
                heads.append(" ".join(w[:words]) + ("…" if len(w) > words else ""))
        return " | ".join(heads)

    @property
    def all_red(self) -> bool:
        red_len = sum(e - s for s, e in self.red)
        return red_len >= len(re.sub(r"\s", "", self.text)) * 0.95 if self.text else False


def _red_spans(p: Paragraph, lead: int, text: str) -> list[tuple[int, int]]:
    """Красные участки текста. Красные run-ы подряд — один вариант; через чёрный пробел — разные."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for r in p.runs:
        t = r.text
        s, e = pos - lead, pos - lead + len(t)
        pos += len(t)
        if not _is_red(r):
            continue
        s, e = max(s, 0), min(e, len(text))
        if not t.strip():
            if spans and spans[-1][1] == s and e > s:
                spans[-1] = (spans[-1][0], e)  # красный пробел внутри варианта
            continue
        if e <= s:
            continue
        if spans and spans[-1][1] == s:
            spans[-1] = (spans[-1][0], e)
        else:
            spans.append((s, e))
    # границы без пробелов по краям
    out = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def collect(editor: DocxEditor, content_marker: str | None) -> tuple[list[EditPar], EditPar | None, EditPar | None, list[Issue]]:
    """Абзацы описания, абзац заключения и абзац рекомендаций."""
    issues: list[Issue] = []
    start = editor.content_start(content_marker)
    body: list[EditPar] = []
    concl = reco = None
    for p in editor.paragraphs[start:]:
        full = p.text
        if not full.strip():
            continue
        if "".join(r.text for r in p.runs) != full:
            issues.append(Issue(level="warning", code="paragraph_complex",
                                message=f"Абзац «{full.strip()[:50]}…» содержит поля/ссылки — не правится автоматически"))
            continue
        kind = "body"
        lead = len(full) - len(full.lstrip())
        if concl is None and _CONCL_RE.match(full):
            kind = "conclusion"
            lead = _CONCL_RE.match(full).end()
            lead += len(full[lead:]) - len(full[lead:].lstrip())
        elif concl is not None and reco is None and _RECO_RE.match(full):
            kind = "reco"
        elif concl is not None:
            break  # после заключения и рекомендаций — подпись врача и служебный текст
        text = full[lead:].rstrip()
        ep = EditPar(p, lead, text, _red_spans(p, lead, text), kind)
        if kind == "conclusion":
            concl = ep
        elif kind == "reco":
            reco = ep
        else:
            body.append(ep)
    return body, concl, reco, issues


# --- очистка текста после удаления вариантов -------------------------------------

def tidy(s: str) -> str:
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([.,;:])", r"\1", s)
    s = re.sub(r",(\s*,)+", ",", s)
    s = re.sub(r",\s*\.", ".", s)
    s = re.sub(r"\.(\s*\.)+", ".", s)
    s = re.sub(r"^[\s,.;:]+", "", s)
    s = re.sub(r"(?<!ед)([.!?])(?=[А-ЯЁA-Z])", r"\1 ", s)  # «ед.Н» не разрывать
    return s.strip()


def cleanup(text: str, red: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for s, e in red:
        out.append(text[pos:s])
        pos = e
    out.append(text[pos:])
    return tidy("".join(out))


# --- проверка новой редакции абзаца ----------------------------------------------

@dataclass
class EditCheck:
    ok: bool
    issues: list[Issue]


def canon_num(n: str) -> str:
    """«16,0» и «16» — одно число; «16,40» и «16,4» тоже."""
    n = n.lstrip("+-")
    if "," in n:
        n = n.rstrip("0").rstrip(",")
    return n


_NEG_STEMS = ("выявл", "определя", "визуализ", "отмеча", "расшир", "утолщ", "увелич", "изменен",
              "наруш", "деформир", "смещ", "накоплен", "сужен")
_SIDE_RE = {"правая": re.compile(r"\b(?:прав(?:ая|ой|ого|ому|ом|ую|ые|ых|ый)|справа)\b", re.I),
            "левая": re.compile(r"\b(?:лев(?:ая|ой|ого|ому|ом|ую|ые|ых|ый)|слева)\b", re.I)}


_FINDING_VERBS = ("выявл", "определя", "визуализ", "отмеча")
# находка, которую нельзя добавить, оставив в том же абзаце «… не выявлено» о ней же
_CONTRADICT_STEMS = ("конкремент", "утолщ", "расшир", "увелич", "кист")


def contradictions(old: EditPar, new_all: str) -> list[str]:
    """Предложения шаблона с отрицанием, оставшиеся рядом с новой находкой о том же:
    «…множественные конкременты…» и «В просвете рентген контрастных конкрементов не выявлено»."""
    out = []
    sents = sentences(new_all)
    for stem in _CONTRADICT_STEMS:
        added = [s for s in sents if s not in old.text and _stem_uses(s.lower(), stem)[1]]
        if not added:
            continue
        for s in sents:
            # «стенки не утолщены», «конкрементов не выявлено» — отрицание до или после слова
            if s in old.text and (re.search(rf"\b(?:не|нет)\s+{stem}", s, re.I) or
                                  re.search(rf"{stem}[а-яё]*\s+(?:[а-яё]+\s+)?(?:не\s+(?:выявлен|определя|визуализ)|нет)", s, re.I)):
                out.append(s)
    return list(dict.fromkeys(out))


def _stem_uses(text: str, stem: str) -> tuple[int, int]:
    """(с отрицанием, без отрицания) — сколько раз слово с этой основой стоит после «не»/«нет»."""
    neg = pos = 0
    for m in re.finditer(rf"(?<![а-яё]){stem}[а-яё]*", text, re.I):
        before = text[max(0, m.start() - 12):m.start()].lower()
        if re.search(r"(?:^|[^а-яё])(?:не|нет|без)\s+(?:[а-яё]+\s+)?$", before):
            neg += 1
        else:
            pos += 1
    return neg, pos


def negation_problems(old: EditPar, new_all: str, frag: str) -> tuple[list[str], list[str]]:
    """(перевёрнутые отрицания — ошибка, снятые отрицания шаблона — проверить).

    «патологического накопления … не выявлено» → «выявлено накопление …» — ошибка: в диктовке
    это слово только с «не». «стенки не утолщены» → «стенки утолщены», когда слова «утолщены»
    в диктовке нет вовсе («стенки толще нет»), — на проверку.
    """
    flips, dropped = [], []
    frag_l = frag.lower()
    old_sents = sentences(old.text)
    for sent in sentences(new_all):
        if sent in old.text:
            continue
        # предложение шаблона, из которого получилось новое
        best = max(old_sents, key=lambda o: fuzz.ratio(o.lower(), sent.lower()), default="")
        score = fuzz.ratio(best.lower(), sent.lower()) if best else 0
        for stem in _NEG_STEMS:
            n_neg, n_pos = _stem_uses(sent, stem)
            if not n_pos:
                continue
            b_neg, b_pos = _stem_uses(best, stem) if score >= 60 else (0, 0)
            if b_pos and score >= 75:
                continue        # утверждение есть в шаблоне («выявлено одинаковое контрастирование»)
            # «визуализируется микроконкремент» → «выявлен микроконкремент»: глаголы находки —
            # синонимы, утверждение любым из них в диктовке разрешает утверждение в правке
            group = _FINDING_VERBS if stem in _FINDING_VERBS else (stem,)
            uses = [_stem_uses(frag_l, g) for g in group]
            f_neg, f_pos = sum(u[0] for u in uses), sum(u[1] for u in uses)
            if f_neg and not f_pos:
                flips.append(sent)
            elif not f_pos and not f_neg and b_neg:
                dropped.append(sent)
    return list(dict.fromkeys(flips)), list(dict.fromkeys(dropped))


def side_problems(old: EditPar, new_all: str, frag: str) -> list[str]:
    """Предложения (новые или из красных вариантов) со стороной, которой нет в диктовке,
    когда врач назвал другую: «В лоханке левой почки … стент» при «…правой… правом…»."""
    said = {k for k, rx in _SIDE_RE.items() if rx.search(frag)}
    if not said:
        return []
    black = old.black_text()
    out = []
    for sent in sentences(new_all):
        if sent in black:
            continue
        sides = {k for k, rx in _SIDE_RE.items() if rx.search(sent)}
        if sides - said:
            out.append(sent)
    return out


def check_edit(old: EditPar, new_texts: list[str], fragments: list[str], label: str) -> EditCheck:
    issues: list[Issue] = []
    frag = " ".join(fragments)
    allowed = {canon_num(n) for n in extract_numbers(old.text) + extract_numbers(frag)}
    new_all = " ".join(new_texts)
    filler = re.search(r"не\s+указан\w*|не\s+продиктован\w*|\?\?|\bN/A\b", new_all, re.I)
    if filler:
        issues.append(Issue(level="error", code="filler", quote=frag[:200],
                            message=f"{label}: в правке служебная вставка «{filler.group(0)}» — абзац не изменён, проверьте вручную"))
        return EditCheck(False, issues)
    extra = [n for n in extract_numbers(new_all) if canon_num(n) not in allowed]
    if extra:
        issues.append(Issue(level="error", code="number_not_dictated", quote=frag[:200],
                            message=f"{label}: в правке числа {extra}, которых нет в диктовке — абзац не изменён, проверьте вручную"))
        return EditCheck(False, issues)

    # число в мм должно быть продиктовано как размер, а не как плотность
    # («печени 63 селезенка 34» ≠ «воротная вена шириной 63мм»)
    old_nums = {canon_num(n) for n in extract_numbers(old.text)}
    for m in re.finditer(r"(\d+(?:,\d+)?)(?=\s*мм|х\d)", new_all):
        n = m.group(1)
        if canon_num(n) in old_nums:
            continue
        alts = "|".join(re.escape(x) for x in dict.fromkeys([n, canon_num(n), canon_num(n) + ",0"]))
        num = rf"(?<![\d,])(?:{alts})(?![\d,]*\d)"
        rx = rf"{num}\s*(?:мм|х|миллиметр|см)|(?:х|на)\s*{num}"
        # «воротная 14,8 селезеночная 6,5» — ширина вен и протоков продиктована без «мм»
        ctx = (rf"(?:размер\w*|ширин\w*|диаметр\w*|толщин\w*|до|воротн\w*|селезеночн\w*|"
               rf"холедох\w*|вен[аы]|проток\w*)(?:\s+(?:вен[аы]|шириной|около|до))*\s+{num}")
        if not re.search(rx, frag) and not re.search(ctx, frag, re.I):
            issues.append(Issue(level="error", code="unit_mismatch", quote=frag[:200],
                                message=f"{label}: число {n} вписано как размер (мм), а в диктовке оно не размер — абзац не изменён, проверьте вручную"))
            return EditCheck(False, issues)

    flips, dropped_neg = negation_problems(old, new_all, frag)
    if flips:
        issues.append(Issue(level="error", code="negation_flipped", quote=frag[:200],
                            message=f"{label}: в правке пропало отрицание, которое врач продиктовал («{flips[0][:90]}») — абзац не изменён, проверьте вручную"))
        return EditCheck(False, issues)
    for sent in dropped_neg:
        issues.append(Issue(level="warning", code="negation_removed", quote=frag[:200],
                            message=f"{label}: снято отрицание шаблона, а в диктовке этого слова нет — проверьте по аудио: «{sent[:90]}»"))
    for sent in contradictions(old, new_all):
        issues.append(Issue(level="warning", code="contradiction", quote=frag[:200],
                            message=f"{label}: норма шаблона противоречит продиктованной находке — исправьте или удалите: «{sent[:90]}»"))
    for sent in side_problems(old, new_all, frag):
        issues.append(Issue(level="warning", code="side_check", quote=frag[:200],
                            message=f"{label}: сторона не совпадает с диктовкой — проверьте: «{sent[:90]}»"))

    base = {_stem(w) for w in _words(old.text)}
    f_words = _words(frag)
    f_stems = {_stem(w) for w in f_words}
    # «вне-внутрипеченочные» из «Вне- внутрипеченочные» шаблона — не новое слово
    new_words = [w for w in _words(new_all) if _stem(w) not in base
                 and not ("-" in w and all(_stem(x) in base for x in w.split("-") if x))]
    bad = [w for w in new_words
           if _stem(w) not in f_stems and not any(fuzz.ratio(w, fw) >= 75 for fw in f_words)]
    if bad and (len(bad) > 4 or len(bad) > 0.3 * max(len(new_words), 1)):
        issues.append(Issue(level="error", code="words_not_dictated", quote=frag[:200],
                            message=f"{label}: в правке слова, которых врач не говорил ({', '.join(bad[:8])}) — абзац не изменён, проверьте вручную"))
        return EditCheck(False, issues)
    if bad:
        issues.append(Issue(level="warning", code="words_check", quote=frag[:200],
                            message=f"{label}: проверьте слова, которых нет в диктовке: {', '.join(bad)}"))
    return EditCheck(True, issues)


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«(])")


def sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text) if s.strip()]


def kept_red_unsupported(old: EditPar, new_text: str, fragments: list[str]) -> list[str]:
    """Предложения новой редакции, взятые из красных вариантов, о которых врач не говорил.

    Сравнение по «отличительным» словам варианта — тем, которых нет в чёрном тексте абзаца
    («грыжи Шморля», «холецистэктомия»): если врач не произнёс ни одного из них, вариант
    не продиктован, даже если модель вписала в него слова из диктовки.
    """
    frag_words = _words(" ".join(fragments))
    f_stems = {_stem(w) for w in frag_words}
    black_stems = {_stem(w) for w in _words(old.black_text())}

    def said(w: str) -> bool:
        return _stem(w) in f_stems or any(fuzz.ratio(w, fw) >= 80 for fw in frag_words)

    out = []
    new_sents = sentences(new_text)
    frag_nums = {canon_num(n) for n in extract_numbers(" ".join(fragments))}
    old_nums = {canon_num(n) for n in extract_numbers(old.text)}

    def slot_filled(red_sent: str) -> bool:
        """«кранио-каудальный размер … составляет мм» + продиктованный размер — вариант продиктован."""
        if not re.search(r"(?<![\d,])\s*мм\b|ед\.Н", red_sent) or re.search(r"\d", red_sent):
            return False
        al = fuzz.partial_ratio_alignment(red_sent.lower(), new_text.lower())
        if al is None or al.score < 60:
            return False
        piece = new_text[al.dest_start:al.dest_end + 12]
        return any(canon_num(n) in frag_nums and canon_num(n) not in old_nums for n in extract_numbers(piece))

    for s, e in old.red:
        for red_sent in sentences(old.text[s:e]):
            distinct = [w for w in _words(red_sent) if _stem(w) not in black_stems and len(w) >= 5]
            if len(distinct) < 2:
                continue
            n_said = sum(1 for w in distinct if said(w))
            support = n_said / len(distinct)
            # абзац-вариант целиком (контрастирование): врач говорит его коротко —
            # «патологических участков накопления контрастного препарата не выявлено»
            if support >= 0.34 or slot_filled(red_sent) or (old.all_red and n_said >= 3):
                continue
            core = red_sent.strip(" ,;")
            if core and core in new_text:
                out.append(core)        # вариант остался дословно (в т.ч. внутри предложения)
                continue
            # какое предложение новой редакции из него получилось
            best = max(new_sents, key=lambda ns: fuzz.ratio(ns.lower(), red_sent.lower()), default=None)
            if best is not None and fuzz.ratio(best.lower(), red_sent.lower()) >= 70:
                out.append(best)
                continue
            # остаток варианта: «В ложе удаленного желчного пузыря определяются металлические клипсы»
            for ns in new_sents:
                if len(ns) >= 25 and ns not in old.black_text() and \
                        fuzz.partial_ratio(ns.lower().strip(" ."), red_sent.lower()) >= 92:
                    out.append(ns)
    return list(dict.fromkeys(out))


# --- запись: разница по словам → точечные правки run-ов ---------------------------

_TOK_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _tokens(s: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOK_RE.finditer(s)]


def _pairs(sm: difflib.SequenceMatcher, idx: list[int] | None = None) -> list[tuple[int, int]]:
    out = []
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            out.append((idx[i + k] if idx is not None else i + k, j + k))
    return out


def _ops_from_pairs(old: str, a, b, pairs) -> list[tuple[str, int, int, str]]:
    ops = []
    pi = pj = 0
    for i, j in pairs + [(len(a), len(b))]:
        if i > pi or j > pj:
            s = a[pi][1] if pi < len(a) else len(old)
            e = a[i - 1][2] if i > pi else s
            txt = "".join(t[0] for t in b[pj:j])
            tag = "insert" if i == pi else ("delete" if j == pj else "replace")
            ops.append((tag, s, e, txt))
        pi, pj = i + 1, j + 1
    return ops


def diff_ops(old: str, new: str, red: list[tuple[int, int]] | None = None) -> list[tuple[str, int, int, str]]:
    """Операции (тип, начало, конец в old, новый текст) для превращения old в new.

    Если в абзаце есть красные варианты, сравниваются два выравнивания: обычное и такое, где
    все красные варианты сначала удаляются. Берётся то, где больше чёрного текста шаблона
    остаётся нетронутым: «…контуры четкие, ровные, структура однородная» из нормы не должна
    уступить такой же фразе внутри удалённого красного варианта.
    """
    a, b = _tokens(old), _tokens(new)
    sm = difflib.SequenceMatcher(None, [t[0] for t in a], [t[0] for t in b], autojunk=False)
    pairs = _pairs(sm)
    if red:
        def is_red(t) -> bool:
            return any(s <= t[1] < e for s, e in red)
        black_idx = [k for k, t in enumerate(a) if not is_red(t)]
        sm_b = difflib.SequenceMatcher(None, [a[k][0] for k in black_idx], [t[0] for t in b], autojunk=False)
        pairs_b = _pairs(sm_b, black_idx)

        def kept_black(ps) -> int:
            return sum(len(a[i][0]) for i, _ in ps if a[i][0].strip() and not is_red(a[i]))
        if kept_black(pairs_b) > kept_black(pairs):
            pairs = pairs_b
    return _ops_from_pairs(old, a, b, pairs)


def apply_text(editor: DocxEditor, ep: EditPar, new_text: str) -> int:
    """Переписывает текст абзаца в new_text минимальными правками. Возвращает число правок."""
    ops = diff_ops(ep.text, new_text, ep.red)
    for tag, s, e, txt in reversed(ops):
        if tag == "delete" or not txt:
            editor.delete_range(ep.p, ep.lead + s, ep.lead + e)
        else:
            editor.replace_range(ep.p, ep.lead + s, ep.lead + e, txt, red=True)
    return len(ops)


def insert_after(editor: DocxEditor, host: Paragraph, text: str) -> Paragraph:
    new_p = editor.insert_paragraph_after(host, [Segment(text=text)])
    if host.text.startswith("\t"):
        first = new_p.find(qn("w:r"))
        if first is not None:
            tab_r = copy.deepcopy(first)
            for child in list(tab_r):
                if child.tag != qn("w:rPr"):
                    tab_r.remove(child)
            tab_r.append(OxmlElement("w:tab"))
            first.addprevious(tab_r)
    return Paragraph(new_p, host._parent)


def set_field(editor: DocxEditor, label: str, value: str) -> str | None:
    """Ставит значение поля шапки («Ф.И.О.:», «Контрастное вещество:»). Возвращает прежнее значение."""
    rx = re.compile(r"^\s*" + anchor_regex(label).pattern, re.I)
    for p in editor.paragraphs:
        m = rx.search(p.text)
        if not m:
            continue
        rest = p.text[m.end():]
        cur = rest.strip(" _.\t")
        if cur == value:
            return cur
        end = len(p.text.rstrip())
        editor.replace_range(p, m.end(), max(end, m.end()), " " + value, red=True)
        return cur
    return None
