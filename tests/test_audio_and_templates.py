from filldocx.audio import dedupe_join, plan_chunks
from filldocx.templates import contrast_from_filename, detect_template


def test_chunks_cut_only_in_pauses():
    speech = [(0, 5), (6, 12), (13, 20), (21, 30), (31, 33)]
    chunks = plan_chunks(speech, max_len=22, overlap=0.3)
    assert chunks == [(0, 20, False), (21, 33, False)]


def test_long_speech_forced_split_with_overlap():
    chunks = plan_chunks([(0, 50)], max_len=22, overlap=0.3)
    assert chunks[0] == (0, 22, True)
    assert chunks[1][0] == 21.7 and chunks[1][2] is True
    assert chunks[-1][1] == 50


def test_dedupe_join():
    assert dedupe_join("размер сто семьдесят", "сто семьдесят три запятая") == "три запятая"
    assert dedupe_join("печень", "обычной формы") == "обычной формы"


def test_detect_template_variants(templates):
    for name in ["Иванов кт обп С+++ Уральск.docx", "Иванов кт ас+контр Уральск.docx", "Иванов КТ АС Атырау.docx",
                 "Иванов kt обп.docx"]:
        assert detect_template(templates, name).id == "kt_obp", name
    for name in ["Петров кт огк Павлодар.docx", "Петров КТ ГС+ Уральск.docx"]:
        assert detect_template(templates, name).id == "kt_ogk", name
    assert detect_template(templates, "файл.docx", "Протокол компьютерной томографии абдоминального сегмента").id == "kt_obp"
    assert detect_template(templates, "файл.docx") is None


def test_contrast_from_filename_cyrillic_and_latin():
    assert contrast_from_filename("Иванов кт обп С+++ Уральск")       # кириллица
    assert contrast_from_filename("Иванов кт обп C+ Уральск")         # латиница
    assert contrast_from_filename("Иванов кт ас+контр Уральск")
    assert not contrast_from_filename("Иванов кт обп Уральск")
