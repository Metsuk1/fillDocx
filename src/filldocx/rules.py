"""Движок правил: подтверждённые факты → операции над шаблоном.

Принципы (§9, §11 правил):
- исходные абзацы шаблона не удаляются;
- числа подставляются в предусмотренные места шаблона (красным);
- изменённое описание органа — новым красным абзацем под исходным абзацем;
- порядок новых абзацев — по порядку органов в шаблоне, а не по порядку диктовки;
- второе заключение — красным под стандартным, «Заключение:» жирным (правило 45);
- рекомендации — отдельным абзацем, по центру, курсивом, только если продиктованы (правило 46).
"""

from __future__ import annotations

import re

from .schema import FillSlot, InsertParagraph, Issue, Operation, Segment, SetField, VerifiedOrgan
from .templates import Organ, TemplateMap
from .textnorm import normalize_text
from .textnorm.formatters import fmt_density, fmt_size
from .textnorm.pipeline import capitalize_sentences


def _slot_inline_value(kind: str, values: list[str]) -> str:
    """Что встаёт между «before» и «after» шаблона (after уже содержит «мм» / «ед.Н»)."""
    if kind == "size":
        return " " + fmt_size(values)[: -len("мм")]
    return " " + fmt_density(values)[: -len(" ед.Н")] + " "


def _slot_formatted(kind: str, values: list[str]) -> str:
    return fmt_size(values) if kind == "size" else fmt_density(values)


def _apply_clause_rules(org: Organ, text: str) -> tuple[str, list[int]]:
    """Короткие продиктованные части → полные формулировки (правила 1, 2, 5, 6, 47–49).

    Текст режется на предложения и части через запятую; правило срабатывает, только если
    часть совпадает с шаблоном правила целиком, поэтому уже развёрнутый текст не меняется.
    """
    if not org.clause_rules:
        return text, []
    used: list[int] = []
    out_sentences = []
    for sent in re.split(r"(?<=\.)\s+", text.strip()):
        clauses = [c.strip() for c in re.split(r",\s*", sent.rstrip(".")) if c.strip()]
        rebuilt: list[str] = []
        pending: list[str] = []
        for c in clauses:
            hit = None
            for cr in org.clause_rules:
                m = re.fullmatch(cr.match, c, flags=re.I)
                if m:
                    hit = cr.out
                    for gi, g in enumerate(m.groups(), start=1):
                        hit = hit.replace("{" + str(gi) + "}", g or "")
                    if cr.rule:
                        used.append(cr.rule)
                    break
            if hit:
                if pending:
                    rebuilt.append(", ".join(pending).rstrip(".") + ".")
                    pending = []
                rebuilt.append(hit)
            else:
                pending.append(c)
        if pending:
            rebuilt.append(", ".join(pending).rstrip(".") + ".")
        out_sentences.append(" ".join(rebuilt))
    return " ".join(out_sentences), used


def _postprocess_text(org: Organ, text: str, ventricle_dash: bool) -> tuple[str, list[int]]:
    rules: list[int] = []
    text = normalize_text(text, ventricle_dash=ventricle_dash)
    for rw in org.rewrites:
        new = re.sub(rw.pattern, rw.replace, text, flags=re.I)
        if new != text and rw.rule:
            rules.append(rw.rule)
        text = new
    text, used = _apply_clause_rules(org, text)
    rules += used
    return capitalize_sentences(text), rules


def _segments(org: Organ, text: str) -> list[Segment]:
    """Курсив только для вводной части (правило 44)."""
    for prefix in org.italic_prefixes:
        if text.lower().startswith(prefix.lower()):
            return [Segment(text=text[: len(prefix)], italic=True), Segment(text=text[len(prefix):])]
    return [Segment(text=text)]


def _formed_text(org: Organ, slots: dict[str, list[str]]) -> str | None:
    """Готовая фраза из шаблона formed, если продиктованы все её числа (правила 20, 21, 41)."""
    for pattern in org.formed:
        names = re.findall(r"\{(\w+)\}", pattern)
        if names and all(n in slots for n in names):
            return pattern.format(**{n: _slot_formatted(org.slots[n].kind, slots[n]) for n in names})
    return None


def build_operations(
    tmpl: TemplateMap,
    organs: dict[str, VerifiedOrgan],
    *,
    birth_date: str | None = None,
    conclusion: str | None = None,
    recommendations: str | None = None,
    recommendations_label: str | None = None,
) -> tuple[list[Operation], list[Issue]]:
    ops: list[Operation] = []
    issues: list[Issue] = []
    dash = tmpl.style.get("ventricle_dash", False)

    if birth_date and "birth_date" in tmpl.fields:
        ops.append(SetField(label=tmpl.fields["birth_date"], value=birth_date, rules=[8]))

    for org_id, vo in organs.items():
        org = tmpl.organ(org_id)
        if org is None:
            continue
        quote = " … ".join(vo.quotes)
        host = org if not org.virtual else tmpl.host_organ(org.id)
        if host is None:
            issues.append(Issue(level="error", code="no_place",
                                message=f"{org.name}: не найдено место в шаблоне — не вписано", organ=org.id, quote=quote))
            continue

        # 1. числа в предусмотренные места шаблона
        if vo.status != "absent":
            for slot_name, values in vo.slots.items():
                slot = org.slots[slot_name]
                if slot.inline and org.anchor:
                    ops.append(FillSlot(anchors=org.all_anchors(), before=slot.before, after=slot.after,
                                        value=_slot_inline_value(slot.kind, values), organ=org.id,
                                        rules=slot.rules, quote=quote))

        # 2. сформированный текст органа — новым красным абзацем
        text_parts: list[str] = []
        rules_used: list[int] = []
        formed = _formed_text(org, vo.slots)
        if formed and vo.status != "absent":
            text_parts.append(formed)
            for n in org.slots:
                rules_used += org.slots[n].rules
        if vo.status in ("changed", "absent"):
            for t in vo.texts:
                t2, r = _postprocess_text(org, t, dash)
                if formed and _same_subject(t2, formed):
                    continue  # фраза из formed уже описывает это
                text_parts.append(t2)
                rules_used += r
        if not text_parts:
            continue
        text = " ".join(text_parts)
        ops.append(InsertParagraph(
            host_anchors=host.all_anchors(),
            order=tmpl.order(org.id),
            segments=_segments(org, text),
            own_paragraph=org.own_paragraph,
            organ=org.id,
            rules=sorted(set(rules_used)),
            quote=quote,
        ))

    # заключение и рекомендации
    concl_anchor = [tmpl.conclusion.anchor]
    if conclusion:
        text = capitalize_sentences(normalize_text(conclusion, ventricle_dash=dash))
        ops.append(InsertParagraph(
            host_anchors=concl_anchor, order=0,
            segments=[Segment(text=tmpl.conclusion.label + " ", bold=True), Segment(text=text)],
            own_paragraph=True, organ="conclusion", rules=[35, 45]))
    if recommendations:
        label = recommendations_label or tmpl.recommendations.default_label
        text = capitalize_sentences(normalize_text(recommendations, ventricle_dash=dash))
        ops.append(InsertParagraph(
            host_anchors=concl_anchor, order=1,
            segments=[Segment(text=f"{label} {text[:1].lower() + text[1:]}", italic=True)],
            own_paragraph=True, align="center", organ="recommendations", rules=[46]))
    return ops, issues


def _same_subject(a: str, b: str) -> bool:
    wa = set(re.findall(r"[а-яё]{5,}", a.lower()))
    wb = set(re.findall(r"[а-яё]{5,}", b.lower()))
    return bool(wa) and len(wa & wb) / len(wa) > 0.6


def conclusion_hints(tmpl: TemplateMap, description: str, conclusion: str | None) -> list[Issue]:
    """Подсказки «описание → заключение» (правила 35–38). Только для интерфейса."""
    out = []
    concl = (conclusion or "").lower()
    for h in tmpl.conclusion.hints:
        m = re.search(h.when, description, flags=re.I)
        if not m:
            continue
        sugg = h.suggest
        for gi, g in enumerate(m.groups(), start=1):
            sugg = sugg.replace("{" + str(gi) + "}", g or "")
        if sugg.lower().rstrip(".") in concl:
            continue
        out.append(Issue(level="info", code="conclusion_hint", rule=h.rule,
                         message=f"В описании есть «{m.group(0)}», в заключении можно указать: «{sugg}»"))
    return out
