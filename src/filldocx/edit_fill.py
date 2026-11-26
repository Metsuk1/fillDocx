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
from .template_edit import (EditPar, apply_text, check_edit, collect, insert_after,
                            kept_red_unsupported, set_field, tidy)
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
    for tag, s, e, _txt in diff_ops(ep.text, new_text):
        if tag not in ("delete", "replace"):
            continue
        black = "".join(ch for k, ch in enumerate(ep.text[s:e], start=s)
                        if not any(rs <= k < re_ for rs, re_ in ep.red))
        words = [w for w in _words(black) if len(w) >= 4]
        if len(words) >= 2 and not any(_stem(w) in f_stems for w in words):
            out.append(" ".join(black.split()))
    return out


def add_organ_cross_refs(body: list[EditPar], frags: dict[int, list[str]]) -> None:
    """В абзац органа уходит только кусок фрагмента от названия органа до следующего органа:
    «…плотность паренхимы 52 55 селезенка 48 50 ед.Н» → селезенке достаётся «селезенка 48 50 ед.Н»."""
    any_organ = re.compile("|".join(_ORGANS.values()), re.I)
    for i in list(frags):
        for frag in list(frags[i]):
            for rx in _ORGANS.values():
                m = re.search(rx, frag, re.I)
                if not m or re.search(rx, body[i].text, re.I):
                    continue
                nxt = any_organ.search(frag, m.end())
                piece = frag[m.start():nxt.start() if nxt else len(frag)].strip(" ,.")
                if len(piece.split()) < 2:
                    continue
                for j, ep in enumerate(body):
                    if j != i and re.search(rx, ep.text, re.I):
                        if piece not in frags.setdefault(j, []):
                            frags[j].append(piece)
                        break


def lost_numbers(fragments: list[str], ans: dict) -> list[str]:
    """Продиктованные числа, которых нет в ответе. Пусто, если потеряно меньше половины."""
    want = list(dict.fromkeys(extract_numbers(" ".join(fragments))))
    if not want:
        return []
    got = set(extract_numbers(" ".join([ans.get("text") or "", *(ans.get("new_paragraphs") or [])])))
    lost = [n for n in want if n not in got]
    return lost if len(lost) * 2 > len(want) else []


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
        words = [w for w in re.findall(r"[а-яёa-z]+", sent.lower()) if len(w) >= 5]
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
    if parts.contrast_said:
        c = parts.contrast_said.lower()
        if re.search(r"не\s+введ|не\s+вводил", c):
            set_field(editor, "Контрастное вещество:", "не введено")
        elif re.search(r"скан|ктск|йогексол|омнипак|ультравист", c):
            set_field(editor, "Контрастное вещество:", "КТ-скан")

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
    route, raw_route = llm.chat_json(route_messages([b.summary() for b in body], dictation),
                                     route_schema(len(body)), "route")
    res.timings["llm_route"] = time.perf_counter() - t
    frags: dict[int, list[str]] = {}
    concl_extra, reco_extra = [], []
    for it in route.get("items", []):
        txt = (it.get("text") or "").strip()
        p = it.get("p")
        if not txt:
            continue
        score = quote_score(txt, tnorm)
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
    add_organ_cross_refs(body, frags)

    # «надпочечники норма» — абзац остаётся нормой шаблона, LLM не нужна
    norm_only = {i for i, fl in frags.items() if all(_NORM_ONLY_RE.match(f) for f in fl)}

    # --- шаг Б: правка абзацев ---
    t = time.perf_counter()
    jobs = {i: frags[i] for i in frags if i not in norm_only}

    def parse(ans: dict) -> tuple[str, list[str]]:
        text = tidy((ans.get("text") or "").replace("⟦", "").replace("⟧", ""))
        pars = [tidy(x.replace("⟦", "").replace("⟧", "")) for x in ans.get("new_paragraphs") or [] if x.strip()]
        pars = [x if x.endswith((".", "!", "?")) else x + "." for x in pars if x]
        return text, pars

    def run(i: int):
        msgs = edit_messages(body[i].marked(), jobs[i])
        ans, raw = llm.chat_json(msgs, EDIT_SCHEMA, "edit", max_tokens=1500)
        hint = None
        lost = lost_numbers(jobs[i], ans)
        if lost:
            hint = retry_hint(lost)
        else:
            text, pars = parse(ans)
            chk = check_edit(body[i], [text, *pars], jobs[i], "")
            if not chk.ok:
                hint = ("Ответ не прошёл проверку: " + chk.issues[0].message.lstrip(": ") +
                        ". Не добавляй слов и чисел, которых нет в диктовке или в абзаце. Ответь JSON заново.")
        if hint:
            # один повтор с подсказкой, что не так
            msgs = msgs + [{"role": "assistant", "content": raw}, {"role": "user", "content": hint}]
            ans2, raw2 = llm.chat_json(msgs, EDIT_SCHEMA, "edit", max_tokens=1500)
            t2, p2 = parse(ans2)
            better = (len(lost_numbers(jobs[i], ans2)) < len(lost)) if lost else \
                check_edit(body[i], [t2, *p2], jobs[i], "").ok
            if better:
                ans, raw = ans2, raw + "\n--- повтор ---\n" + raw2
        return i, (ans, raw)

    answers: dict[int, tuple[dict, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, ans in ex.map(run, list(jobs)):
            answers[i] = ans
    res.timings["llm_edit"] = time.perf_counter() - t

    log = []
    not_said = []
    for i, ep in enumerate(body):
        label = ep.summary(4).split(" | ")[0]
        if i not in answers:
            new_text = ep.black_text()
            if i in norm_only:
                log.append({"par": i + 1, "mode": "norm", "old": ep.marked(), "fragments": frags[i], "new": new_text})
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
        ans, raw = answers[i]
        new_text, new_pars = parse(ans)
        chk = check_edit(ep, [new_text, *new_pars], jobs[i], label)
        issues += chk.issues
        log.append({"par": i + 1, "mode": "llm", "old": ep.marked(), "fragments": jobs[i],
                    "new": new_text, "new_paragraphs": new_pars, "ok": chk.ok, "raw": raw})
        if not chk.ok:
            continue
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
        issues += chk.issues
        concl_log = {"old": concl.text, "dictated": dictated_c, "new": new_c, "ok": chk.ok, "raw": raw,
                     "reco_dictated": dictated_r, "reco": new_r}
        if chk.ok and new_c:
            apply_text(editor, concl, new_c)
        dictated_r_clean = new_r if new_r else ""
    else:
        dictated_r_clean = dictated_r
        if concl is not None:
            issues.append(Issue(level="warning", code="conclusion_not_dictated",
                                message="Заключение не продиктовано — оставлено как в шаблоне, проверьте"))
    if reco is not None:
        if dictated_r_clean:
            label = parts.recommendations_label or (re.match(r"\s*([^:]+:)", reco.text).group(1)
                                                    if ":" in reco.text else "Рекомендовано:")
            new_full = f"{label} {dictated_r_clean}"
            chk = check_edit(reco, [new_full], [label, dictated_r], "Рекомендации")
            issues += chk.issues
            if chk.ok:
                apply_text(editor, reco, new_full)
        else:
            editor.delete_paragraph(reco.p)
            issues.append(Issue(level="info", code="reco_removed",
                                message=f"Рекомендации не продиктованы — строка шаблона «{reco.text}» удалена"))
    res.timings["llm_conclusion"] = time.perf_counter() - t

    res.journal = {"route_raw": raw_route, "fragments": {str(k + 1): v for k, v in frags.items()},
                   "paragraphs": log, "conclusion": concl_log, "fio": fio,
                   "moved": moved, "norm_only": sorted(i + 1 for i in norm_only)}
    return res
