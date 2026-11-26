"""Запись в DOCX: точечные правки, красный цвет, сохранение исходного текста и формата."""

import zipfile

from docx import Document

from filldocx.docx_writer import DocxEditor
from filldocx.schema import FillSlot, InsertParagraph, Segment, SetField

RED = "FF0000"


def _red_texts(path):
    d = Document(str(path))
    out = []
    for p in d.paragraphs:
        for r in p.runs:
            if r.font.color is not None and r.font.color.rgb is not None and str(r.font.color.rgb) == RED:
                out.append(r.text)
    return out


def test_fill_slots_inline_keeps_text_and_marks_red(obp_docx, tmp_path):
    ed = DocxEditor(obp_docx)
    before = [p.text for p in ed.paragraphs]
    ops = [
        FillSlot(anchors=["На полученных КТ изображениях печень"], before="составляет", after="мм", value=" 173,4", organ="liver"),
        FillSlot(anchors=["На полученных КТ изображениях печень"], before="плотность паренхимы", after="ед.Н", value=" +60, +63 ", organ="liver"),
        FillSlot(anchors=["Селезенка"], before="в пределах", after="ед.Н", value=" +34 ", organ="spleen"),
    ]
    res = ed.apply(ops, content_marker="Информированное добровольное согласие")
    assert not res.issues
    out = tmp_path / "out.docx"
    ed.save(out)

    d = Document(str(out))
    texts = [p.text for p in d.paragraphs]
    assert "составляет 173,4мм, плотность паренхимы +60, +63 ед.Н, при нативном" in texts[7]
    assert "в пределах +34 ед.Н." in texts[8]
    assert _red_texts(out) == [" 173,4", " +60, +63 ", " +34 "]
    # остальные абзацы не изменились
    assert [t for i, t in enumerate(texts) if i not in (7, 8)] == [t for i, t in enumerate(before) if i not in (7, 8)]
    # новый текст в Times New Roman 14
    run = next(r for r in d.paragraphs[7].runs if r.text == " 173,4")
    assert run.font.name == "Times New Roman" and run.font.size.pt == 14


def test_insert_paragraphs_order_and_formatting(obp_docx, tmp_path):
    ed = DocxEditor(obp_docx)
    ops = [
        InsertParagraph(host_anchors=["Селезенка"], order=7, organ="stomach", own_paragraph=True,
                        segments=[Segment(text="Стенки желудка утолщены.")]),
        InsertParagraph(host_anchors=["Поджелудочная железа"], order=5, organ="duodenum", own_paragraph=True,
                        segments=[Segment(text="Двенадцатиперстная кишка расширена.")]),
        InsertParagraph(host_anchors=["Поджелудочная железа"], order=4, organ="pancreas", own_paragraph=False,
                        segments=[Segment(text="Поджелудочная железа обычной формы, контуры сглажены.")]),
        InsertParagraph(host_anchors=["Заключение:"], order=0, organ="conclusion",
                        segments=[Segment(text="Заключение: ", bold=True), Segment(text="КТ-картина холецистита.")]),
        InsertParagraph(host_anchors=["Заключение:"], order=1, organ="recommendations", align="center",
                        segments=[Segment(text="Рекомендовано: консультация хирурга.", italic=True)]),
    ]
    res = ed.apply(ops, content_marker="Информированное добровольное согласие")
    assert not res.issues
    out = tmp_path / "out.docx"
    ed.save(out)
    d = Document(str(out))
    texts = [p.text for p in d.paragraphs]
    i = texts.index(next(t for t in texts if t.startswith("Поджелудочная железа обычной формы, не увеличена")))
    assert texts[i + 1] == "Поджелудочная железа обычной формы, контуры сглажены."
    assert texts[i + 2] == "Двенадцатиперстная кишка расширена."
    assert texts[i + 3] == "Стенки желудка утолщены."            # всё под исходным абзацем, в порядке органов
    j = texts.index("Заключение: КТ-признаков патологии не выявлено.")
    assert texts[j + 1] == "Заключение: КТ-картина холецистита."  # стандартное заключение осталось на месте
    assert texts[j + 2] == "Рекомендовано: консультация хирурга."
    concl = d.paragraphs[j + 1]
    assert concl.runs[0].bold and not concl.runs[1].bold
    reco = d.paragraphs[j + 2]
    assert reco.alignment == 1 and reco.runs[0].italic           # 1 = CENTER


def test_set_field_birth_date_and_no_overwrite(obp_docx, tmp_path):
    ed = DocxEditor(obp_docx)
    res = ed.apply([SetField(label="Дата рождения:", value="01.04.1964")])
    assert not res.issues
    out = tmp_path / "out.docx"
    ed.save(out)
    assert "Дата рождения: 01.04.1964" in [p.text for p in Document(str(out)).paragraphs]

    ed2 = DocxEditor(out)
    res2 = ed2.apply([SetField(label="Дата рождения:", value="02.04.1964")])
    assert res2.issues and res2.issues[0].code == "field_not_empty"


def test_missing_anchor_writes_nothing(obp_docx, tmp_path):
    ed = DocxEditor(obp_docx)
    before = [p.text for p in ed.paragraphs]
    res = ed.apply([InsertParagraph(host_anchors=["Предстательная железа"], order=1, organ="x",
                                    segments=[Segment(text="текст")])], content_marker="Информированное")
    assert res.issues[0].code == "anchor_not_found"
    ed.save(tmp_path / "out.docx")
    assert [p.text for p in Document(str(tmp_path / "out.docx")).paragraphs] == before


def test_output_is_valid_docx_package(obp_docx, tmp_path):
    ed = DocxEditor(obp_docx)
    ed.apply([FillSlot(anchors=["На полученных КТ изображениях печень"], before="составляет", after="мм",
                       value=" 1,0", organ="liver")])
    out = tmp_path / "out.docx"
    ed.save(out)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        xml = z.read("word/document.xml").decode("utf8")
    assert 'w:color w:val="FF0000"' in xml
