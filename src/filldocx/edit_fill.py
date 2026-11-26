"""Заполнение шаблона врача в режиме «правка шаблона» (см. template_edit.py)."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from .docx_writer import DocxEditor
from .llm.edit_prompt import (CONCL_SCHEMA, EDIT_SCHEMA, conclusion_messages, edit_messages,
                              route_messages, route_schema)
from .schema import Issue
from .segment import DictationParts
from .template_edit import (EditPar, apply_text, canon_num, check_edit, collect, insert_after,
                            kept_red_unsupported, sentences, set_field, tidy)
from .textnorm import extract_numbers
from .verify import _norm, _stem, _words, quote_score

_STUDY_IN_NAME = re.compile(r"\s(?:кт|мрт|мскт|kt|mrt)\b", re.I)

# Органы, по которым фрагмент диктовки дублируется в «свой» абзац, если LLM отнесла
# его к соседнему («плотность печени 52 55 селезенка 48 50» — и печени, и селезенке).
_ORGANS = {
    "селезенка": r"\bселез[её]нк\w*",
    "печень": r"\bпечен[ьи]\b",
    "поджелудочная": r"\bподжелудочн\w*",
    "желчный пузырь": r"\bжелчн\w*\s+пузыр\w*",
    "надпочечники": r"\bнадпочечн\w*",
    "почки": r"\bпоч(?:ки|ек|ка|ке|кам|ках)\b",
    "мочевой пузырь": r"\bмочев\w*\s+пузыр\w*",
    "предстательная": r"\bпредстательн\w*",
    "матка": r"\bматк\w*",
}


_CONTRAST_DESCRIBED = re.compile(
    r"(?:внутривенного\s+)?контрастного\s+усилени\w*|после\s+контрастировани\w*|"
    r"артериальн\w+\s+фаз\w*|венозн\w+\s+фаз\w*|контрастного\s+препарата", re.I)
# «убери отсюда», «малый таз отсюда вытащим», «отдельно напишем» — указание по оформлению
_COMMAND_RE = re.compile(r"\b(?:убери|уберите|удали|удалите|вытащим|вытащи|отдельно\s+(?:буду\s+диктовать|напишем))\b", re.I)

# слова, которые в «…норма» ничего не добавляют к норме абзаца
_NORM_FILLER = {"так", "тоже", "также", "после", "фаза", "фазы", "фазу", "фазе", "сканирования", "артериальная",
                "артериальной", "артериальную", "венозная", "венозной", "венозную", "отсроченная", "отсроченной",
                "контрастирования", "нормально", "норма", "норме", "норм", "нормы", "все", "всё", "окне"}


def is_norm_fragment(frag: str, paragraph: str, tolerance: float = 0.0) -> bool:
    """«внутривенного контрастного усиления норма артериальная фаза тоже норма» — только «норма»:
    слова фрагмента — из самого абзаца или служебные (кроме доли ``tolerance``). Чисел нет."""
    if not re.search(r"\bнорм", frag, re.I) or re.search(r"\d", frag):
        return False
    p_stems = {_stem(w) for w in _words(paragraph)}
    words = [w for w in re.findall(r"[а-яё]+", frag.lower()) if len(w) >= 4]
    bad = [w for w in words if w not in _NORM_FILLER and _stem(w) not in p_stems]
    return len(bad) <= tolerance * len(words)


_NORM_ONLY_RE = re.compile(r"^[\s.,]*[а-яё-]+(?:\s+[а-яё-]+){0,2}\s+(?:в\s+)?норм[аеы]?[\s.,]*$", re.I)


def _sim(a: str, b: str) -> float:
    sa = {_stem(w) for w in _words(a) if len(w) >= 4}
    sb = {_stem(w) for w in _words(b) if len(w) >= 4}
    return len(sa & sb) / len(sa) if sa else 0.0


def reassign_by_similarity(body: list[EditPar], frags: dict[int, list[str]]) -> list[tuple[str, int, int]]:
    """Фрагмент, почти дословно совпадающий с другим абзацем, переносится туда.

    «после внутривенного контрастного усиления очагов патологического накопления…» —
    это абзац о контрастировании, даже если LLM отнесла его к костям.
    """
    moved = []
    for i in list(frags):
        for frag in list(frags[i]):
            scores = [_sim(frag, ep.text) for ep in body]
            j = max(range(len(body)), key=lambda k: scores[k])
            if j != i and scores[j] >= 0.6 and scores[j] - scores[i] >= 0.3:
                frags[i].remove(frag)
                frags.setdefault(j, []).append(frag)
                moved.append((frag, i + 1, j + 1))
        if not frags[i]:
            del frags[i]
    return moved


def deleted_black(ep: EditPar, new_text: str, fragments: list[str]) -> list[str]:
    """Куски чёрного текста шаблона (от 2 слов), которые модель удалила без повода в диктовке."""
    from .template_edit import diff_ops
    f_stems = {_stem(w) for w in _words(" ".join(fragments))}
    out = []
    for tag, s, e, _txt in diff_ops(ep.text, new_text, ep.red):
        if tag not in ("delete", "replace"):
            continue
        black = "".join(ch for k, ch in enumerate(ep.text[s:e], start=s)
                        if not any(rs <= k < re_ for rs, re_ in ep.red))
        words = [w for w in _words(black) if len(w) >= 4]
        if len(words) >= 2 and not any(_stem(w) in f_stems for w in words):
            out.append(" ".join(black.split()))
    return out


# Где в быстрой диктовке начинается описание органа: именительный падеж («селезенка 53»,
# «почки почек четкие…», «в малом тазу мочевой пузырь…»). Родительный («края селезенки»,
# «правой почки») — это уточнение внутри описания другого органа, по нему не режем.
_ORGAN_START = {
    "селезенка": r"\bселез[её]нка\b",
    # «…селезенка 53 печени однородная…», «структура паренхима печени за счёт…» — снова о печени
    "печень": r"\bпечень\b|\bпечени\b(?=\s+(?:не)?однородн)|\bструктур\w*\s+(?:паренхим\w*\s+|по\s+)?печени\b",
    "поджелудочная": r"\bподжелудочн(?:ая|о)\b",
    "желчный пузырь": r"\bжелчн(?:ый|ого)\s+пузыр[ья]\b",
    "надпочечники": r"\bнадпочечник(?:и|ов)?\b",
    "почки": r"\bпочки\b|\bпоч(?:ек|еки)\b(?=\s+(?:четк|ровн|контур|бобов|обычн))",
    "мочевой пузырь": r"\b(?:в\s+малом\s+тазу\s+)?мочевой\s+пузырь\b",
    "предстательная": r"\bпредстательн(?:ая|ой)\s+желез\w*",
    "матка": r"\bматка\b",
}
_ORGAN_GUARD = set("""
в во на к у до от из по с со над под за при как чем ворот края крае проекции уровне области стенке
стенки ложе шейке шейки дне теле тела правой левой обеих обоих нижнего верхнего среднего сегмента
полюса ткани контур контуры синусы размеры размер паренхима паренхимы структура функция чашечек
лоханок конкрементов
""".split())


def _organ_home(body: list[EditPar], rx: str) -> int | None:
    """Абзац органа: тот, где название органа стоит в начале одного из предложений."""
    for j, ep in enumerate(body):
        for sent in re.split(r"(?<=[.!?])\s+", ep.text):
            m = re.search(rx, sent, re.I)
            if m and m.start() <= 40:
                return j
    return None


def split_by_organ(frag: str) -> list[tuple[str, str | None]]:
    """Режет фрагмент по началам описаний органов: [(кусок, орган или None)]."""
    cuts = []
    for name, rx in _ORGAN_START.items():
        for m in re.finditer(rx, frag, re.I):
            prev = re.findall(r"[\w,.+-]+", frag[:m.start()].lower())
            if prev and prev[-1] in _ORGAN_GUARD:
                continue
            cuts.append((m.start(), name))
    cuts.sort()
    pieces, start, organ = [], 0, None
    for pos, name in cuts:
        if name == organ:
            continue                      # «почки почек четкие…» — тот же орган
        if frag[start:pos].strip(" ,."):
            pieces.append((frag[start:pos].strip(" ,."), organ))
        start, organ = pos, name
    if frag[start:].strip(" ,."):
        pieces.append((frag[start:].strip(" ,."), organ))
    return pieces


# Фразы, у которых в протоколе свой абзац, даже если LLM отнесла их к соседнему:
# (фраза в диктовке, текст абзаца-адресата). Порядок абзацев контрастирования модель путает.
_PHRASE_HOMES = [
    (r"патологическ\w*\s+(?:[а-яё]+\s+){0,3}накоплени\w*", r"патологического\s+накопления"),
    (r"(?:функци\w*\s+)?почек\s+не\s+нарушен\w*", r"функция\s+почек\s+не\s+нарушена"),
    (r"урографи\w*", r"урографи"),
]
_DISCOURSE_SPLIT = re.compile(r"\s+(?:так|далее|дальше)\s+", re.I)


def _phrase_route(body: list[EditPar], piece: str, target: int) -> list[tuple[str, int]]:
    """Делит кусок по «так/далее» и переносит части с «фразой своего абзаца» в этот абзац."""
    uro = re.compile(r"урографи", re.I)

    def fits(k: int, prx: str, text: str) -> bool:
        # абзац КТ-урографии — только если врач сказал «урография»
        return bool(re.search(prx, body[k].text, re.I)) and (not uro.search(body[k].text) or bool(uro.search(text)))

    def wrong_place(frx: str, prx: str, j, text: str) -> bool:
        return j is not None and bool(re.search(frx, text, re.I)) and not fits(target, prx, text)

    homes = []
    for frx, prx in _PHRASE_HOMES:
        j = next((k for k in range(len(body)) if fits(k, prx, piece)), None)
        homes.append((frx, prx, j))
    if not any(wrong_place(frx, prx, j, piece) for frx, prx, j in homes):
        return [(piece, target)]
    out = []
    for sub in _DISCOURSE_SPLIT.split(piece):
        dest = target
        for frx, prx, j in homes:
            if wrong_place(frx, prx, j, sub):
                dest = next((k for k in range(len(body)) if fits(k, prx, sub)), j)
                break
        if sub.strip(" ,."):
            out.append((sub.strip(" ,."), dest))
    return out


def add_organ_cross_refs(body: list[EditPar], frags: dict[int, list[str]]) -> list[tuple[str, int, int]]:
    """Кусок фрагмента про другой орган переносится в абзац этого органа:
    «…плотность паренхимы 52 55 селезенка 48 50 ед.Н» → селезенке достаётся «селезенка 48 50 ед.Н»,
    печени остаётся своё. Возвращает перенесённые куски (кусок, из абзаца, в абзац), нумерация с 1."""
    homes = {name: _organ_home(body, _ORGANS.get(name, rx)) for name, rx in _ORGAN_START.items()}
    out: dict[int, list[str]] = {}
    moved = []
    for i in sorted(frags):
        for frag in frags[i]:
            for piece, organ in split_by_organ(frag):
                j = homes.get(organ) if organ else None
                routed = _phrase_route(body, piece, j if j is not None else i)
                if organ is None and len(routed) == 1 and routed[0][1] == i:
                    # «у переднего края селезенки … добавочная долька» в абзаце печени: кусок в первых
                    # словах называет ровно один орган, у которого свой абзац, а этот абзац о нём молчит
                    head = " ".join(piece.split()[:6])
                    named = [n for n, rx in _ORGANS.items()
                             if re.search(rx, head, re.I) and homes.get(n) is not None]
                    if len(named) == 1 and homes[named[0]] != i and not re.search(_ORGANS[named[0]], body[i].text, re.I):
                        routed = [(piece, homes[named[0]])]
                for sub, target in routed:
                    lst = out.setdefault(target, [])
                    if sub not in lst:
                        lst.append(sub)
                    if target != i:
                        moved.append((sub, i + 1, target + 1))
    frags.clear()
    frags.update({k: v for k, v in out.items() if v})
    return moved


def lost_numbers(fragments: list[str], ans: dict) -> list[str]:
    """Продиктованные числа, которых нет в ответе. Пусто, если потеряно меньше половины."""
    want = list(dict.fromkeys(canon_num(n) for n in extract_numbers(" ".join(fragments))))
    if not want:
        return []
    got = _answer_numbers(ans)
    lost = [n for n in want if n not in got]
    # находка целиком пропала: у фрагмента были числа, и ни одного нет в ответе
    whole = any(nums and not any(n in got for n in nums)
                for nums in ({canon_num(x) for x in extract_numbers(f)} for f in fragments))
    return lost if (len(lost) * 2 > len(want) or whole) else []


def _answer_numbers(ans: dict) -> set[str]:
    return {canon_num(n) for n in extract_numbers(" ".join([ans.get("text") or "", *(ans.get("new_paragraphs") or [])]))}


def all_numbers_lost(fragments: list[str], ans: dict) -> bool:
    want = {canon_num(n) for n in extract_numbers(" ".join(fragments))}
    return bool(want) and not (want & _answer_numbers(ans))


def retry_hint(lost: list[str]) -> str:
    return ("В ответе нет продиктованных чисел: " + ", ".join(lost) + ". Перечитай фрагменты диктовки: "
            "впиши продиктованные находки с этими числами (если для них нет места в абзаце — "
            "в new_paragraphs), а варианты, о которых врач не говорил, удали. Ответь JSON заново.")


def remove_fragment(text: str, frag: str) -> str | None:
    """Убирает из текста непродиктованный вариант, если он стоит там дословно."""
    pos = text.find(frag)
    if pos < 0:
        f2 = frag.rstrip(" .,;")
        pos = text.find(f2)
        if pos < 0:
            return None
        frag = f2
    return tidy(text[:pos] + " " + text[pos + len(frag):])


def stale_template_numbers(ep: EditPar, new_text: str, fragments: list[str]) -> list[str]:
    said = set(extract_numbers(" ".join(fragments)))
    old = set(extract_numbers(ep.text))
    return [n for n in dict.fromkeys(extract_numbers(new_text)) if n in old and n not in said]


def _supported(word: str, dictated_flat: str, dictated_words: list[str]) -> bool:
    w = word.lower().replace("ё", "е")
    # по основе — только короткие слова: у длинных терминов общее начало ничего не значит
    # («гепатоспленомегалия» и «гепатомегалия»)
    if len(w) <= 8 and _stem(w) in {_stem(x) for x in dictated_words}:
        return True
    # отдельные слова и склейки соседних: «холецистопанкреатита» ~ «хулициста панкреотита»,
    # но «гепатоспленомегалия» ≠ «гепатомегалия»
    pairs = [a + b for a, b in zip(dictated_words, dictated_words[1:])]
    def close(x: str, need: float) -> bool:
        return fuzz.ratio(w, x) >= need and abs(len(w) - len(x)) <= max(3, len(w) // 5)
    return any(close(x, 78) for x in dictated_words) or any(close(x, 85) for x in pairs)


_CONCL_BOILERPLATE = {"картина", "картины"}


def drop_unsupported_sentences(text: str, dictated: str) -> tuple[str, list[str], list[str]]:
    """Пункты заключения, которых врач не произносил, отбрасываются.

    Пункт отбрасывается, если не подтверждается его главное (первое) слово или половина
    значимых слов («Остеохондроз…», «Умеренная гепатоспленомегалия» при продиктованной
    «гепатомегалии»). Если не подтверждается отдельное слово — пункт остаётся, слово в спорных.
    Возвращает (текст, отброшенные пункты, сомнительные слова).
    """
    d_words = [w.replace("ё", "е") for w in re.findall(r"[а-яёa-z]+", dictated.lower())]
    flat = "".join(d_words)
    keep, dropped, doubtful = [], [], []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        # «КТ-картина …» — оформление пункта, врач может его не произносить
        words = [w for w in re.findall(r"[а-яёa-z]+", sent.lower()) if len(w) >= 5 and w not in _CONCL_BOILERPLATE]
        bad = [w for w in words if not _supported(w, flat, d_words)]
        if words and (words[0] in bad or len(bad) * 2 >= len(words)):
            dropped.append(sent.strip())
        else:
            keep.append(sent)
            doubtful += bad
    return tidy(" ".join(keep)), dropped, doubtful


def fio_from_filename(name: str) -> str | None:
    stem = Path(name).stem
    head = _STUDY_IN_NAME.split(stem, maxsplit=1)[0].strip()
    words = re.findall(r"[А-ЯЁа-яё-]{2,}", head)
    return " ".join(words) if words and len(words) <= 4 else None


def choose_fio(said: str | None, filename: str) -> tuple[str | None, Issue | None]:
    """Ф.И.О. из диктовки; написание — из имени файла, если это тот же человек."""
    from_file = fio_from_filename(filename)
    if said:
        said_t = " ".join(w.capitalize() for w in said.split())
        if from_file:
            a, b = said_t.split()[0].lower(), from_file.split()[0].lower()
            if fuzz.ratio(a, b) >= 70:
                return from_file, None
        return said_t, Issue(level="warning", code="fio_spelling",
                             message=f"Ф.И.О. «{said_t}» записано по распознанной диктовке — проверьте написание")
    if from_file:
        return from_file, Issue(level="warning", code="fio_from_filename",
                                message=f"Ф.И.О. не продиктовано — взято из имени файла: «{from_file}»")
    return None, Issue(level="warning", code="fio_missing", message="Ф.И.О. не продиктовано и не найдено в имени файла")


def recover_quote(quote: str, dictation_norm: str, min_score: float = 70.0) -> str | None:
    """Место диктовки, которое модель процитировала неточно, — по границам слов."""
    al = fuzz.partial_ratio_alignment(_norm(quote), dictation_norm)
    if al is None or al.score < min_score:
        return None
    s, e = al.dest_start, al.dest_end
    while s > 0 and not dictation_norm[s - 1].isspace():
        s -= 1
    while e < len(dictation_norm) and not dictation_norm[e].isspace():
        e += 1
    real = dictation_norm[s:e].strip(" ,.")
    # не больше, чем процитировала модель: иначе захватим соседние куски
    if len(real) > len(quote) * 1.3 + 10:
        return None
    return real


def house_style(text: str) -> str:
    """Оформление чисел как в протоколах: «20,9×19,2 мм» → «20,9х19,2мм», «Вне-внутри…» → «Вне- внутри…»."""
    text = re.sub(r"(\d)\s*[×xX*]\s*(?=\d)", r"\1х", text)
    text = re.sub(r"(\d)\s+мм\b", r"\1мм", text)
    text = re.sub(r"\b([Вв]не)-(?=внутри)", r"\1- ", text)
    return text


_SENTENCE_CODES = {"number_not_dictated", "unit_mismatch", "negation_flipped"}


def salvage_sentences(ep: EditPar, text: str, pars: list[str], fragments: list[str]) -> tuple[str, list[str], list[str]]:
    """Предложения, не прошедшие проверку чисел/отрицаний, заменяются ближайшим предложением
    нормы шаблона (или убираются). Возвращает (текст, новые абзацы, отброшенные предложения)."""
    black_sents = sentences(ep.black_text())

    def bad(sent: str) -> bool:
        c = check_edit(ep, [sent], fragments, "")
        return any(x.code in _SENTENCE_CODES for x in c.issues if x.level == "error")

    dropped: list[str] = []
    out = []
    for sent in sentences(text):
        if not bad(sent):
            out.append(sent)
            continue
        dropped.append(sent)
        best = max(black_sents, key=lambda b: fuzz.ratio(b.lower(), sent.lower()), default=None)
        if best and fuzz.ratio(best.lower(), sent.lower()) >= 50 and best not in out and not bad(best):
            out.append(best)
    kept_pars = []
    for par in pars:
        if bad(par):
            dropped.append(par)
        else:
            kept_pars.append(par)
    return tidy(" ".join(out)), kept_pars, dropped


def lost_dictated_numbers(dictation: str, doc_text: str) -> list[Issue]:
    have = {canon_num(n) for n in extract_numbers(doc_text)}
    spans: list[list] = []          # [начало, конец, числа]
    for m in re.finditer(r"(?<![\w,])\d+(?:,\d+)?", dictation):
        n = canon_num(m.group(0))
        if n in have:
            continue
        if spans and m.start() - spans[-1][1] <= 30:
            spans[-1][1] = m.end()
            spans[-1][2].append(m.group(0))
        else:
            spans.append([m.start(), m.end(), [m.group(0)]])
    out = []
    for s, e, nums in spans:
        a = dictation.rfind(" ", 0, max(0, s - 50)) + 1
        b = dictation.find(" ", e + 25)
        ctx = dictation[a:b if b > 0 else len(dictation)].strip()
        out.append(Issue(level="warning", code="dictated_not_written", quote=ctx,
                         message=f"Продиктовано, но не попало в документ: {', '.join(nums)} — «…{ctx}…», впишите вручную"))
    return out


@dataclass
class EditFillResult:
    issues: list[Issue] = field(default_factory=list)
    journal: dict = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


def fill_by_edit(editor: DocxEditor, llm, parts: DictationParts, transcript_norm: str,
                 content_marker: str | None, filename: str, quote_min_score: float = 85.0,
                 workers: int = 4) -> EditFillResult:
    res = EditFillResult()
    issues = res.issues
    tnorm = _norm(transcript_norm)

    # --- шапка ---
    fio, iss = choose_fio(parts.fio_said, filename)
    if iss:
        issues.append(iss)
    if fio:
        set_field(editor, "Ф.И.О.:", fio)
    bd = parts.birth_date
    if bd and bd.value and not bd.ambiguous:
        set_field(editor, "Дата рождения:", bd.value)
    elif bd and bd.ambiguous:
        issues.append(Issue(level="warning", code="birth_date", rule=8,
                            message="Дата рождения не вписана: " + "; ".join(bd.notes or ["не распознана"])))
    contrast_field = editor.read_field("Контрастное вещество:") or ""
    if parts.contrast_said:
        c = parts.contrast_said.lower()
        if re.search(r"не\s+введ|не\s+вводил", c):
            set_field(editor, "Контрастное вещество:", "не введено")
        elif re.search(r"скан|ктск|йогексол|омнипак|ультравист", c):
            set_field(editor, "Контрастное вещество:", "КТ-скан")
        elif re.search(r"не\s+введ", contrast_field, re.I):
            # «…года, контраст, печень…» — контраст введён, препарат не назван
            set_field(editor, "Контрастное вещество:", "КТ-скан")
            issues.append(Issue(level="warning", code="contrast_agent", quote=parts.contrast_said,
                                message="Продиктовано «контраст» без названия препарата — вписано «КТ-скан», проверьте"))
    elif re.search(r"не\s+введ", contrast_field, re.I) and _CONTRAST_DESCRIBED.search(parts.body):
        set_field(editor, "Контрастное вещество:", "КТ-скан")
        issues.append(Issue(level="warning", code="contrast_agent",
                            quote=_CONTRAST_DESCRIBED.search(parts.body).group(0),
                            message="Контраст в начале не назван, но продиктованы фазы контрастирования — "
                                    "вписано «КТ-скан», проверьте"))
    dictation_all = parts.body + " " + " ".join(t for _, t in parts.late_additions)
    last_end = -1
    for m in _COMMAND_RE.finditer(dictation_all):
        if m.start() < last_end:
            continue                    # то же указание, сказанное в одной фразе
        a, b = max(0, m.start() - 60), min(len(dictation_all), m.end() + 30)
        last_end = b + 40
        ctx = dictation_all[a:b].strip()
        issues.append(Issue(level="warning", code="doctor_command", quote=ctx,
                            message=f"Врач дал указание по оформлению — выполните вручную: «…{ctx}…»"))

    body, concl, reco, c_issues = collect(editor, content_marker)
    issues += c_issues
    if not body:
        issues.append(Issue(level="error", code="no_body", message="Не найдены абзацы описания — документ не изменён"))
        return res

    # --- шаг А: раскладка диктовки по абзацам ---
    t = time.perf_counter()
    dictation = parts.body + "".join(" " + txt for _, txt in parts.late_additions)
    if not parts.conclusion:
        issues.append(Issue(level="info", code="conclusion_by_llm",
                            message="Слово «заключение» не найдено в диктовке — границу заключения определила LLM"))
    # позднее дополнение, где назван орган («ещё к левой почке надо будет …»), идёт прямо в абзац
    # этого органа: склеенное с концом описания, оно уходит у модели в чужой абзац
    frags: dict[int, list[str]] = {}
    route_text = parts.body
    for _, txt in parts.late_additions:
        head = " ".join(txt.split()[:6])
        named = [n for n, rx in _ORGANS.items() if re.search(rx, head, re.I)]
        home = _organ_home(body, _ORGANS[named[0]]) if len(named) == 1 else None
        if home is not None:
            frags.setdefault(home, []).append(txt)
            issues.append(Issue(level="info", code="late_addition", quote=txt,
                                message=f"Дополнение к описанию — в абзац {home + 1}: «{txt[:60]}»"))
        else:
            route_text += " " + txt
    route, raw_route = llm.chat_json(route_messages([b.summary() for b in body], route_text),
                                     route_schema(len(body)), "route")
    res.timings["llm_route"] = time.perf_counter() - t
    dnorm = _norm(route_text)
    concl_extra, reco_extra = [], []
    for it in route.get("items", []):
        txt = (it.get("text") or "").strip()
        p = it.get("p")
        if not txt:
            continue
        score = quote_score(txt, tnorm)
        if score < quote_min_score and max(_sim(txt, b.text) for b in body) >= 0.6:
            # модель переписала в ответ текст абзаца шаблона, а не диктовку — это не потеря
            issues.append(Issue(level="info", code="template_copied", quote=txt,
                                message=f"Раскладка: модель вернула текст шаблона вместо диктовки — пропущено: «{txt[:60]}…»"))
            continue
        if score < 100 and (score >= quote_min_score or len(txt.split()) >= 6):
            # модель чуть переписала кусок («селезинка», «исправила» слова) — берём настоящий текст
            # диктовки на этом месте: проверка дальше идёт по нему, а не по словам модели
            real = recover_quote(txt, dnorm)
            if real and len(real.split()) >= min(4, len(txt.split())):
                if score < quote_min_score:
                    issues.append(Issue(level="info", code="quote_recovered", quote=real,
                                        message=f"Раскладка: фрагмент модели заменён текстом диктовки «{real[:60]}…»"))
                txt, score = real, 100.0
        if score < quote_min_score:
            issues.append(Issue(level="warning", code="quote_not_found", quote=txt,
                                message=f"Фрагмент «{txt[:60]}…» не найден в расшифровке ({score:.0f}%) — не использован"))
            continue
        if p == "X":
            issues.append(Issue(level="info", code="offtopic", quote=txt,
                                message=f"Посторонний разговор — в Word не попал: «{txt[:80]}»"))
        elif p == "C":
            concl_extra.append(txt)
        elif p == "R":
            reco_extra.append(txt)
        elif p and p.isdigit() and 1 <= int(p) <= len(body):
            frags.setdefault(int(p) - 1, []).append(txt)
    covered = " ".join(" ".join(v) for v in frags.values()) + " " + " ".join(concl_extra + reco_extra)
    for piece in re.split(r"(?<=[.!?])\s+", dictation):
        if len(piece.split()) >= 4 and fuzz.partial_ratio(_norm(piece), _norm(covered)) < 70:
            issues.append(Issue(level="warning", code="not_routed", quote=piece,
                                message=f"Часть диктовки не разложена по абзацам — проверьте: «{piece[:80]}»"))

    moved = reassign_by_similarity(body, frags)
    for frag, a, b in moved:
        issues.append(Issue(level="info", code="fragment_moved", quote=frag,
                            message=f"Фрагмент перенесён из абзаца {a} в абзац {b} (совпадает с его текстом): «{frag[:60]}»"))
    for frag, a, b in add_organ_cross_refs(body, frags):
        issues.append(Issue(level="info", code="fragment_moved", quote=frag,
                            message=f"Кусок диктовки про другой орган перенесён из абзаца {a} в абзац {b}: «{frag[:60]}»"))

    # «надпочечники норма» — абзац остаётся нормой шаблона, LLM не нужна
    norm_only = {i for i, fl in frags.items()
                 if all(_NORM_ONLY_RE.match(f) or
                        # абзац-вариант целиком («После внутривенного контрастного усиления…»):
                        # «…тоже норма», даже с парой слов о соседней фазе, — вариант выбран как есть
                        is_norm_fragment(f, body[i].text, 0.2 if body[i].all_red else 0.0) for f in fl)}

    # --- шаг Б: правка абзацев ---
    t = time.perf_counter()
    jobs = {i: frags[i] for i in frags if i not in norm_only}

    def parse(ans: dict) -> tuple[str, list[str]]:
        text = house_style(tidy((ans.get("text") or "").replace("⟦", "").replace("⟧", "")))
        pars = [house_style(tidy(x.replace("⟦", "").replace("⟧", ""))) for x in ans.get("new_paragraphs") or [] if x.strip()]
        pars = [x if x.endswith((".", "!", "?")) else x + "." for x in pars if x]
        return text, pars

    def judge(i: int, ans: dict):
        text, pars = parse(ans)
        chk = check_edit(body[i], [text, *pars], jobs[i], "")
        return chk, lost_numbers(jobs[i], ans), all_numbers_lost(jobs[i], ans)

    def run(i: int):
        msgs = edit_messages(body[i].marked(), jobs[i])
        ans, raw = llm.chat_json(msgs, EDIT_SCHEMA, "edit", max_tokens=1500)
        chk, lost, lost_all = judge(i, ans)
        if not lost and chk.ok:
            return i, (ans, raw, None)
        # один повтор с подсказкой, что не так
        hint = retry_hint(lost) if lost else (
            "Ответ не прошёл проверку: " + chk.issues[0].message.lstrip(": ") +
            ". Не добавляй слов и чисел, которых нет в диктовке или в абзаце. Ответь JSON заново.")
        msgs = msgs + [{"role": "assistant", "content": raw}, {"role": "user", "content": hint}]
        ans2, raw2 = llm.chat_json(msgs, EDIT_SCHEMA, "edit", max_tokens=1500)
        chk2, lost2, lost_all2 = judge(i, ans2)
        both = raw + "\n--- повтор ---\n" + raw2
        # годится ответ, который прошёл проверку и не потерял все продиктованные числа;
        # из двух годных — тот, где потеряно меньше
        good = [(len(l), k) for k, (c, l, la) in enumerate([(chk, lost, lost_all), (chk2, lost2, lost_all2)])
                if c.ok and not la]
        if not good:
            if (chk.ok and lost_all) or (chk2.ok and lost_all2):
                return i, (ans2 if chk2.ok else ans, both,
                           "модель не вписала продиктованные числа: " + ", ".join(lost2 or lost))
            # обе правки не прошли проверку — дальше спасаем по предложениям ту, где больше чисел
            return i, (ans if len(lost) <= len(lost2) else ans2, both, None)
        best = min(good)[1]
        return i, ((ans, ans2)[best], both, None)

    answers: dict[int, tuple[dict, str, str | None]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, ans in ex.map(run, list(jobs)):
            answers[i] = ans
    res.timings["llm_edit"] = time.perf_counter() - t

    # абзацы органов, которые описаны своими фрагментами: модель не должна дублировать их
    # отдельными абзацами в чужом («Селезенка плотность +47 ед.Н.» после абзаца печени)
    organ_homes = {name: _organ_home(body, _ORGANS[name]) for name in _ORGAN_START}

    log = []
    not_said = []
    for i, ep in enumerate(body):
        label = ep.summary(4).split(" | ")[0]
        if i not in answers:
            new_text = ep.black_text()
            if i in norm_only:
                if ep.all_red:
                    # весь абзац — вариант («После внутривенного контрастного усиления … не выявлено»),
                    # и врач сказал о нём «норма» — вариант выбран, остаётся как есть
                    new_text = ep.text
                log.append({"par": i + 1, "mode": "norm", "old": ep.marked(), "fragments": frags[i], "new": new_text})
                if ep.all_red:
                    continue
                if ep.red and new_text:
                    apply_text(editor, ep, new_text)
                elif ep.red:
                    editor.delete_paragraph(ep.p)
                continue
            nums = extract_numbers(new_text)
            if nums:
                issues.append(Issue(level="warning", code="template_numbers",
                                    message=f"{label}: не продиктовано, но в норме шаблона есть числа {', '.join(nums)} — проверьте"))
            if ep.red:
                if new_text:
                    apply_text(editor, ep, new_text)
                else:
                    editor.delete_paragraph(ep.p)
            not_said.append(label)
            log.append({"par": i + 1, "mode": "not_dictated", "old": ep.marked(), "new": new_text})
            continue
        ans, raw, reject = answers[i]
        new_text, new_pars = parse(ans)
        chk = check_edit(ep, [new_text, *new_pars], jobs[i], label)
        if not chk.ok and not reject and chk.issues and chk.issues[-1].code in _SENTENCE_CODES:
            # ошибка в одном предложении («селезеночная вена 5мм» из «селезеночная и 5») не должна
            # стоить всего абзаца: это предложение берётся из шаблона, остальное вписывается
            s_text, s_pars, bad = salvage_sentences(ep, new_text, new_pars, jobs[i])
            chk2 = check_edit(ep, [s_text, *s_pars], jobs[i], label)
            if chk2.ok and bad:
                new_text, new_pars, chk = s_text, s_pars, chk2
                for sent in bad:
                    issues.append(Issue(level="warning", code="sentence_rejected", quote=" ".join(jobs[i])[:200],
                                        message=f"{label}: предложение не прошло проверку и не вписано — проверьте по аудио: «{sent[:100]}»"))
        issues += chk.issues
        log.append({"par": i + 1, "mode": "llm", "old": ep.marked(), "fragments": jobs[i],
                    "new": new_text, "new_paragraphs": new_pars, "ok": chk.ok and not reject, "raw": raw})
        if chk.ok and reject:
            issues.append(Issue(level="error", code="edit_rejected", quote=" ".join(jobs[i])[:200],
                                message=f"{label}: {reject} — абзац не изменён, впишите вручную"))
        if not chk.ok or reject:
            # абзац не правится, но непродиктованные красные варианты («холецистэктомия»)
            # в документе оставлять нельзя: остаётся норма шаблона, вписать надо вручную
            if ep.red and not ep.all_red and ep.black_text():
                apply_text(editor, ep, ep.black_text())
                log[-1]["fallback"] = "норма шаблона"
            continue
        if not new_text and ep.black_text() and not ep.all_red:
            # модель стёрла абзац с нормой шаблона («в костном окне норма» → пусто) — так не бывает
            new_text = ep.black_text()
            issues.append(Issue(level="warning", code="empty_edit", quote=" ".join(jobs[i])[:200],
                                message=f"{label}: модель удалила абзац целиком — оставлена норма шаблона, проверьте"))
            log[-1]["new"] = new_text
        dup = []
        for np_text in new_pars:
            for name, rx in _ORGAN_START.items():
                j = organ_homes.get(name)
                m = re.search(rx, np_text, re.I)
                if m and m.start() <= 3 and j is not None and j != i and (j in frags):
                    dup.append(np_text)
                    break
        if dup:
            new_pars = [x for x in new_pars if x not in dup]
            log[-1]["dropped_duplicates"] = dup
            issues.append(Issue(level="info", code="duplicate_organ", quote=" ".join(jobs[i])[:200],
                                message=f"{label}: модель повторила описание другого органа — не вписано: «{'», «'.join(d[:60] for d in dup)}»"))
        removed = []
        for frag in kept_red_unsupported(ep, new_text, jobs[i]):
            cut = remove_fragment(new_text, frag)
            if cut is not None:
                new_text = cut
                removed.append(frag)
            else:
                issues.append(Issue(level="warning", code="red_kept_unsupported", quote=" ".join(jobs[i])[:200],
                                    message=f"{label}: оставлен вариант, которого нет в диктовке — проверьте: «{frag[:80]}»"))
        if removed:
            log[-1]["removed_unsupported"] = removed
            log[-1]["new"] = new_text
        lost_black = deleted_black(ep, new_text, jobs[i])
        if lost_black:
            issues.append(Issue(level="warning", code="template_text_removed", quote=" ".join(jobs[i])[:200],
                                message=f"{label}: удалён текст шаблона, о котором врач не говорил — проверьте: «{'», «'.join(lost_black)}»"))
        template_nums = stale_template_numbers(ep, new_text, jobs[i])
        if template_nums:
            issues.append(Issue(level="warning", code="template_numbers", quote=" ".join(jobs[i])[:200],
                                message=f"{label}: остались числа из шаблона, которых нет в диктовке: {', '.join(template_nums)} — проверьте"))
        # непродиктованные варианты, которые модель вынесла в отдельный абзац
        kept_pars = []
        for np_text in new_pars:
            for frag in kept_red_unsupported(ep, np_text, jobs[i]):
                cut = remove_fragment(np_text, frag)
                if cut is not None:
                    np_text = cut
                    removed.append(frag)
            if np_text.strip(" .,"):
                kept_pars.append(np_text)
        new_pars = kept_pars
        if removed:
            log[-1]["removed_unsupported"] = removed
            log[-1]["new_paragraphs"] = new_pars
        host = ep.p
        if new_text:
            apply_text(editor, ep, new_text)
        for np_text in reversed(new_pars):
            insert_after(editor, host, np_text)
        if not new_text:
            editor.delete_paragraph(ep.p)
    if not_said:
        issues.append(Issue(level="info", code="not_dictated",
                            message="Не продиктовано — оставлена норма шаблона, красные варианты удалены: " + "; ".join(not_said)))

    # --- заключение и рекомендации ---
    t = time.perf_counter()
    dictated_c = " ".join(x for x in [parts.conclusion or "", *concl_extra] if x).strip()
    dictated_r = " ".join(x for x in [parts.recommendations or "", *reco_extra] if x).strip()
    concl_log = {}
    if concl is not None and dictated_c:
        ans, raw = llm.chat_json(conclusion_messages(concl.text, dictated_c, dictated_r), CONCL_SCHEMA, "conclusion")
        new_c = tidy(ans.get("conclusion") or "")
        new_r = tidy(ans.get("recommendations") or "")
        new_c, dropped, doubtful = drop_unsupported_sentences(new_c, dictated_c)
        for sent in dropped:
            issues.append(Issue(level="warning", code="conclusion_unsupported", quote=dictated_c[:200],
                                message=f"Заключение: пункт «{sent}» не подтверждается диктовкой — не вписан, проверьте"))
        if doubtful:
            issues.append(Issue(level="warning", code="conclusion_words", quote=dictated_c[:200],
                                message=f"Заключение: проверьте слова, которых не слышно в диктовке: {', '.join(doubtful)}"))
        chk = check_edit(concl, [new_c], [dictated_c], "Заключение")
        if not chk.ok:
            # не выбрасывать всё заключение из-за одного пункта: остаются пункты, прошедшие проверку
            keep, bad = [], []
            for sent in sentences(new_c):
                (keep if check_edit(concl, [sent], [dictated_c], "").ok else bad).append(sent)
            if keep:
                for sent in bad:
                    issues.append(Issue(level="warning", code="conclusion_unsupported", quote=dictated_c[:200],
                                        message=f"Заключение: пункт «{sent}» не прошёл проверку по диктовке — не вписан, проверьте"))
                new_c = tidy(" ".join(keep))
                chk = check_edit(concl, [new_c], [dictated_c], "Заключение")
        issues += chk.issues
        concl_log = {"old": concl.text, "dictated": dictated_c, "new": new_c, "ok": chk.ok, "raw": raw,
                     "reco_dictated": dictated_r, "reco": new_r}
        if chk.ok and new_c:
            apply_text(editor, concl, new_c)
        else:
            # список шаблона — это не норма, а набор типичных диагнозов: оставить его нельзя
            apply_text(editor, concl, "")
            issues.append(Issue(level="error", code="conclusion_empty", quote=dictated_c[:200],
                                message=f"Заключение не собрано из диктовки — оставлено пустым, впишите вручную: «{dictated_c[:150]}»"))
        dictated_r_clean = new_r or dictated_r
    else:
        dictated_r_clean = dictated_r
        if concl is not None:
            apply_text(editor, concl, "")
            issues.append(Issue(level="error", code="conclusion_not_dictated",
                                message="Заключение не продиктовано — список диагнозов шаблона удалён, впишите заключение вручную"))
    if reco is not None:
        if dictated_r_clean:
            tmpl_label = re.match(r"\s*([^:]+:)", reco.text).group(1) if ":" in reco.text else "Рекомендовано:"
            label = parts.recommendations_label or tmpl_label
            if re.match(r"рекомендован", label, re.I):
                label = tmpl_label      # «рекомендована консультация» → «Рекомендовано: консультация»
            text_r = dictated_r_clean[:1].lower() + dictated_r_clean[1:] if not dictated_r_clean[:2].isupper() else dictated_r_clean
            new_full = f"{label} {text_r}"
            chk = check_edit(reco, [new_full], [label, dictated_r], "Рекомендации")
            issues += chk.issues
            if chk.ok:
                apply_text(editor, reco, new_full)
        else:
            editor.delete_paragraph(reco.p)
            issues.append(Issue(level="info", code="reco_removed",
                                message=f"Рекомендации не продиктованы — строка шаблона «{reco.text}» удалена"))
    res.timings["llm_conclusion"] = time.perf_counter() - t

    # продиктованные числа, которых нет в документе: находка потерялась (ошибка распознавания,
    # модель её пропустила, абзац не прошёл проверку) — проверяющий должен это увидеть
    issues += lost_dictated_numbers(dictation, "\n".join(p.text for p in editor.paragraphs))

    res.journal = {"route_raw": raw_route, "fragments": {str(k + 1): v for k, v in frags.items()},
                   "paragraphs": log, "conclusion": concl_log, "fio": fio,
                   "moved": moved, "norm_only": sorted(i + 1 for i in norm_only)}
    return res
