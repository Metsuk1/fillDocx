"""Общие фикстуры. Синтетический шаблон КТ ОБП — без данных пациентов."""

from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from filldocx.templates import load_all_templates

ROOT = Path(__file__).resolve().parents[1]


def _run(p, text, bold=False, italic=False):
    r = p.add_run(text)
    r.font.name = "Times New Roman"
    r.font.size = Pt(14)
    r.bold = bold
    r.italic = italic
    return r


def build_obp_template(path: Path, birth_date: str = "") -> Path:
    """Шаблон в духе реального: шапка, поля этапа 1, абзацы органов, заключение, подпись.

    Текст специально разбит на несколько run-ов, как это бывает в настоящих документах.
    """
    d = Document()
    p = d.add_paragraph()
    _run(p, "Протокол компьютерной томографии абдоминального сегмента", bold=True)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(d.add_paragraph(), "Ф.И.О.: Тестов Тест Тестович")
    _run(d.add_paragraph(), "Дата рождения:" + (f" {birth_date}" if birth_date else ""))
    _run(d.add_paragraph(), "Дата исследования: 01.01.2026")
    _run(d.add_paragraph(), "Контрастное вещество: не введено")
    _run(d.add_paragraph(), "Доза: 9,0 мЗв")
    _run(d.add_paragraph(), "Информированное добровольное согласие на обследование получено.", italic=True)

    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _run(p, "На полученных КТ изображениях печень обычной формы, не увеличена в размерах, кранио-каудальный ")
    _run(p, "размер правой доли печени составляет мм, плотность паренхимы ")
    _run(p, "ед.Н, при нативном сканировании структура паренхимы однородная. ")
    _run(p, "Желчный пузырь овально-вытянутой формы, стенки не утолщены, в шейке и в теле определяется перегиб. "
            "В просвете рентген контрастных конкрементов не выявлено. ")
    _run(p, "Вне- внутрипеченочные протоки не расширены. Воротная, селезеночная вены не расширены.")

    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _run(p, "Поджелудочная железа обычной формы, не увеличена в размерах, контуры четкие, ровные, структура однородная. ")
    _run(p, "Селезенка обычной формы и размеров, контуры ровные, денситометрические показатели в пределах ед.Н.")

    _run(d.add_paragraph(), "Надпочечники Y-образной формы, обычных размеров, ножки не утолщены.")
    _run(d.add_paragraph(), "Почки «бобовидной» формы, обычных размеров, сосудистые ножки не изменены. Контуры почек ровные, четкие.")
    _run(d.add_paragraph(), "В костном окне данных за деструктивные и травматические повреждения не выявлено.")
    p = d.add_paragraph()
    _run(p, "Заключение:", bold=True)
    _run(p, " КТ-признаков патологии не выявлено.")
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(p, "Врач радиолог                  Тестовый В.В.", bold=True, italic=True)
    d.save(str(path))
    return path


@pytest.fixture
def templates():
    return load_all_templates(ROOT / "config" / "templates")


@pytest.fixture
def obp(templates):
    return templates["kt_obp"]


@pytest.fixture
def obp_docx(tmp_path):
    return build_obp_template(tmp_path / "Тестов Тест кт обп Город.docx")
