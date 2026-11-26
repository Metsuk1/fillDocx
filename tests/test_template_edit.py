"""Режим «правка шаблона»: красные варианты, точечная запись, проверки, разбор диктовки."""

from docx import Document
from docx.shared import Pt, RGBColor

from filldocx.docx_writer import DocxEditor
from filldocx.edit_fill import (add_organ_cross_refs, choose_fio, drop_unsupported_sentences,
                                fill_by_edit)
from filldocx.segment import split_dictation
from filldocx.template_edit import EditPar, apply_text, check_edit, cleanup, collect, kept_red_unsupported
from filldocx.textnorm import normalize_text

RED = RGBColor(0xFF, 0, 0)


def _run(p, text, red=False):
    r = p.add_run(text)
    r.font.name = "Times New Roman"
    r.font.size = Pt(14)
    if red:
        r.font.color.rgb = RED
    return r


def build_red_template(path):
    d = Document()
    _run(d.add_paragraph(), "Ф.И.О.: ")
    _run(d.add_paragraph(), "Дата рождения:")
    _run(d.add_paragraph(), "Контрастное вещество: не введено")
    _run(d.add_paragraph(), "Информированное добровольное согласие на обследование получено.")
    p = d.add_paragraph()
    _run(p, "\tНа полученных КТ изображениях печень обычной формы, не увеличена в размерах, ")
    _run(p, "кранио-каудальный размер правой доли печени составляет мм, ", red=True)
    _run(p, "плотность паренхимы +55 ед.Н. Желчный пузырь овальной формы, стенки не утолщены. ")
    _run(p, "Желчный пузырь в типичном месте не визуализируется (холецистэктомия).", red=True)
    p = d.add_paragraph()
    _run(p, "\tНадпочечники Y-образной формы, обычных размеров, ножки не утолщены.")
    p = d.add_paragraph()
    _run(p, "\tОпределяется дефект белой линии живота (пупочная грыжа).", red=True)
    p = d.add_paragraph()
    _run(p, "\tЗаключение: КТ-картина хронического холецистопанкреатита. Хронический калькулезный холецистит. "
            "Остеохондроз поясничного отдела позвоночника.")
    _run(d.add_paragraph(), "Рекомендовано: консультация уролога")
    _run(d.add_paragraph(), "Врач радиолог  Тестов Т.Т.")
    d.save(str(path))
    return path


def _colors(p):
    return [(r.text, bool(r.font.color and r.font.color.type and r.font.color.rgb == RED)) for r in p.runs]


# --- разбор шаблона ----------------------------------------------------------

def test_collect_marks_red_variants(tmp_path):
    ed = DocxEditor(build_red_template(tmp_path / "t.docx"))
    body, concl, reco, issues = collect(ed, "Информированное добровольное согласие")
    assert len(body) == 3 and not issues
    assert "⟦кранио-каудальный размер правой доли печени составляет мм,⟧" in body[0].marked()
    assert "⟦Желчный пузырь в типичном месте не визуализируется (холецистэктомия).⟧" in body[0].marked()
    assert body[2].all_red
    assert concl.text.startswith("КТ-картина")
    assert reco.text == "Рекомендовано: консультация уролога"


def test_cleanup_removes_variants_and_fixes_punctuation():
    text = "контуры четкие, ровные, структура однородная, структура неоднородная. Селезенка +44 ед.Н."
    s = text.index("структура неоднородная")
    e = s + len("структура неоднородная")
    assert cleanup(text, [(s, e)]) == "контуры четкие, ровные, структура однородная. Селезенка +44 ед.Н."


# --- запись ------------------------------------------------------------------

def test_apply_text_keeps_format_and_marks_new_red(tmp_path):
    ed = DocxEditor(build_red_template(tmp_path / "t.docx"))
    body, *_ = collect(ed, "Информированное добровольное согласие")
    ep = body[0]
    new = ("На полученных КТ изображениях печень обычной формы, не увеличена в размерах, "
           "кранио-каудальный размер правой доли печени составляет 173,4мм, плотность паренхимы +63 ед.Н. "
           "Желчный пузырь овальной формы, стенки утолщены.")
    apply_text(ed, ep, new)
    assert ep.p.text == "\t" + new
    runs = _colors(ep.p)
    red_text = "".join(t for t, red in runs if red)
    black_text = "".join(t for t, red in runs if not red)
    assert "173,4мм" in red_text and "63" in red_text
    assert "холецистэктомия" not in ep.p.text
    assert "Желчный пузырь овальной формы" in black_text     # нетронутое осталось чёрным


# --- проверки ----------------------------------------------------------------

def _ep(text, red=()):
    return EditPar(p=None, lead=0, text=text, red=list(red))


def test_check_rejects_invented_number_and_density_as_size():
    ep = _ep("Воротная вена шириной мм, селезеночная вена мм.")
    assert not check_edit(ep, ["Воротная вена шириной 14мм."], ["воротная вена 15мм"], "x").ok
    assert not check_edit(ep, ["Воротная вена шириной 63мм."], ["печени 63 селезенка 34"], "x").ok
    assert check_edit(ep, ["Воротная вена шириной 16,5мм."], ["воротная вена шириной 16,5мм"], "x").ok


def test_check_rejects_filler():
    ep = _ep("Поперечные размеры: головки мм, тела мм.")
    assert not check_edit(ep, ["Поперечные размеры: головки не указано."], ["поджелудочная норма"], "x").ok


def test_unsupported_red_variant_detected_even_if_reworded():
    text = ("В костном окне краевые костные разрастания. В телах поясничных позвонков определяются "
            "краевые дефекты замыкательных пластин (грыжи Шморля).")
    s = text.index("В телах")
    ep = _ep(text, [(s, len(text))])
    new = ("В костном окне краевые костные заострения. В телах поясничных и нижнегрудных позвонков "
           "определяются краевые дефекты замыкательных пластин (грыжи Шморля).")
    found = kept_red_unsupported(ep, new, ["в костном окне поясничных и нижнегрудных позвонков краевые костные заострения"])
    assert found and "Шморля" in found[0]


def test_conclusion_drops_undictated_items():
    text = "КТ-картина хронического холецистопанкреатита. Умеренная гепатоспленомегалия. Остеохондроз поясничного отдела позвоночника."
    kept, dropped, _ = drop_unsupported_sentences(text, "КТ-картина хронического холецистопанкреатита умеренная гепатомегалия")
    assert kept == "КТ-картина хронического холецистопанкреатита."
    assert len(dropped) == 2


def test_cross_ref_takes_only_organ_piece():
    body = [_ep("Печень обычной формы, плотность паренхимы +55 ед.Н."),
            _ep("Поджелудочная железа обычной формы. Селезенка обычной формы, +44 ед.Н.")]
    frags = {0: ["печени 198,5мм плотность паренхимы 52 55 селезенка 48 50 ед.Н"]}
    add_organ_cross_refs(body, frags)
    assert frags[1] == ["селезенка 48 50 ед.Н"]


# --- разбор диктовки ---------------------------------------------------------

def test_spoken_decimals():
    assert normalize_text("сто девяносто восемь и пять миллиметров") == "198,5мм"
    assert normalize_text("воротная вена шириной шестнадцать с половиной миллиметров") == "воротная вена шириной 16,5мм"
    assert normalize_text("до шестнадцать и четыре на пятьдесят три и один на двадцать семь и пять миллиметров") == "до 16,4х53,1х27,5мм"
    assert normalize_text("до семнадцати семь миллиметров") == "до 17,7мм"


def test_units_not_inside_words():
    assert normalize_text("по передним и боковым контурам") == "по передним и боковым контурам"
    assert normalize_text("до пятидесяти восьми единиц хан силы") == "до +58 ед.Н"


def test_dictated_punctuation():
    assert normalize_text("увеличены запятая размерами до 12 миллиметров точка в малом тазу") == \
        "увеличены, размерами до 12мм. в малом тазу"


def test_name_birth_contrast_from_dictation():
    t = normalize_text("пациент петрав иваннн дата рождения двенадцать ноль три шестьдесят восьмого года "
                       "коте доминального сегмента контраст котескан на полученных кт изображениях печень")
    p = split_dictation(t)
    assert p.fio_said == "петрав иваннн"
    assert p.birth_date.value == "12.03.1968"
    assert p.contrast_said == "контраст котескан"
    assert p.body.startswith("на полученных")


def test_late_addition_after_poka_vse():
    t = normalize_text("печень норма заключение кт-картина холецистита пока все а при кт урографии норма")
    p = split_dictation(t)
    assert p.conclusion.endswith("холецистита")
    assert p.late_additions and "урографии норма" in p.late_additions[0][1]


def test_fio_spelling_from_filename():
    fio, issue = choose_fio("петрав иваннн", "Петров Иван кт ас+контр Уральск.docx")
    assert fio == "Петров Иван" and issue is None
    fio, issue = choose_fio(None, "Сидоров Пётр кт обп С+++ Уральск.docx")
    assert fio == "Сидоров Пётр" and issue.code == "fio_from_filename"


# --- сквозной прогон на поддельной LLM ------------------------------------------

class FakeLLM:
    """Отвечает по имени схемы, как это делала бы модель."""

    def chat_json(self, messages, schema, name="answer", max_tokens=None):
        user = messages[-1]["content"]
        if name == "route":
            return {"items": [
                {"p": "1", "text": "печень обычной формы кранио-каудальный размер 173,4мм плотность паренхимы +63 ед.Н"},
                {"p": "2", "text": "надпочечники норма"},
            ]}, "{}"
        if name == "edit":
            return {"text": "На полученных КТ изображениях печень обычной формы, не увеличена в размерах, "
                            "кранио-каудальный размер правой доли печени составляет 173,4мм, плотность паренхимы "
                            "+63 ед.Н. Желчный пузырь овальной формы, стенки не утолщены. Желчный пузырь в типичном "
                            "месте не визуализируется (холецистэктомия).",
                    "new_paragraphs": []}, "{}"
        if name == "conclusion":
            return {"conclusion": "КТ-картина хронического холецистопанкреатита. Остеохондроз поясничного отдела позвоночника.",
                    "recommendations": ""}, "{}"
        raise AssertionError(name + user)


def test_fill_by_edit_end_to_end(tmp_path):
    path = build_red_template(tmp_path / "Тестов Тест кт обп Уральск.docx")
    ed = DocxEditor(path)
    text = normalize_text("пациент тестов тест дата рождения 12 07 1957 года кт абдоминального сегмента "
                          "контраст не введен печень обычной формы кранио-каудальный размер 173,4мм "
                          "плотность паренхимы +63 ед.Н надпочечники норма заключение кт-картина хронического "
                          "холецистопанкреатита все")
    parts = split_dictation(text)
    res = fill_by_edit(ed, FakeLLM(), parts, text, "Информированное добровольное согласие", path.name, workers=1)
    ed.save(path)
    doc = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "Ф.И.О.: Тестов Тест" in doc
    assert "Дата рождения: 12.07.1957" in doc
    assert "173,4мм" in doc and "+63 ед.Н" in doc
    assert "холецистэктомия" not in doc              # непродиктованный вариант удалён, хотя модель его оставила
    assert "пупочная грыжа" not in doc               # красный абзац без диктовки удалён
    assert "Надпочечники Y-образной формы, обычных размеров" in doc
    assert "Заключение: КТ-картина хронического холецистопанкреатита." in doc
    assert "Остеохондроз" not in doc and "калькулезный" not in doc
    assert "Рекомендовано" not in doc
    assert any(i.code == "conclusion_unsupported" for i in res.issues)


# --- случаи из диктовок У.Р. (WhatsApp, ноябрь 2026) ---------------------------

def test_bare_spoken_decimals_and_third_size():
    assert normalize_text("воротная четырнадцать и восемь селезеночная шесть и пять поджелудочная") == \
        "воротная 14,8 селезеночная 6,5 поджелудочная"
    assert normalize_text("воротные семнадцать ноль селезеночная") == "воротные 17,0 селезеночная"
    assert normalize_text("размеры правой почки тридцать восемь и девять на пятьдесят девять и девять "
                          "третий размер на девяносто девять и пять миллиметров") == "размеры правой почки 38,9х59,9х99,5мм"
    assert normalize_text("пятьдесят семь ноль на пятьдесят и семь третий размерная шестьдесят пять и восемь") == \
        "57,0х50,7х65,8мм"
    # даты цифрами и счёт позвонков не склеиваются
    assert normalize_text("ноль пять ноль восемь две тысячи восьмого года").startswith("0 5 0 8")
    assert normalize_text("деформация тела четыре и пять позвонков") == "деформация тела 4 и 5 позвонков"
    assert normalize_text("от полтора миллиметра") == "от 1,5мм"


def test_liver_spleen_densities_without_units():
    assert normalize_text("печени семьдесят два селезенка пятьдесят три печени однородная") == \
        "плотность печени +72 ед.Н селезенка +53 ед.Н печени однородная"
    assert normalize_text("плотностью девятнадцать единиц ханс") == "плотностью +19 ед.Н"


def test_fuzzy_terms_keep_word_forms():
    from filldocx.textnorm.terms import Lexicon, correct_terms
    lex = Lexicon(terms={"плотность", "признаки", "кальцинатов"})
    text, fixes = correct_terms("плотностью признаками кальцинов", lex)
    assert text == "плотностью признаками кальцинатов"


def test_conclusion_word_variants_and_late_insert():
    t = normalize_text("печень норма заключено хронический холецистит все")
    assert split_dictation(t).conclusion == "хронический холецистит"
    t = normalize_text("печень норма заключение кистозное образование печени так еще к левой почке надо будет "
                       "в среднем сегменте образование размером пять миллиметров продолжаем заключение "
                       "хронический панкреатит рекомендована консультация хирурга все на этом")
    p = split_dictation(t)
    assert p.late_additions and p.late_additions[0][1].startswith("к левой почке")
    assert "хронический панкреатит" in p.conclusion and "почке" not in p.conclusion
    assert p.recommendations == "консультация хирурга"


def test_check_rejects_flipped_negation_and_accepts_vein_width():
    ep = _ep("После внутривенного контрастного усиления патологического накопления не выявлено.", [(0, 79)])
    frag = ["в артериальной фазу патологических участков накопление контрастного препарата не выявлено"]
    assert not check_edit(ep, ["В артериальной фазе выявлено накопление контрастного препарата."], frag, "x").ok
    ep = _ep("Воротная вена шириной мм, селезеночная вена мм.")
    assert check_edit(ep, ["Воротная вена шириной 14,8мм, селезеночная вена 17,0мм."],
                      ["воротная 14,8 селезеночная 17"], "x").ok


def test_contradiction_warned():
    ep = _ep("Желчный пузырь овальной формы. В просвете рентген контрастных конкрементов не выявлено.")
    new = ("Желчный пузырь овальной формы. В просвете определяются множественные конкременты размерами до 9,7мм. "
           "В просвете рентген контрастных конкрементов не выявлено.")
    chk = check_edit(ep, [new], ["в просвете определяются множественные конкременты размерами до 9,7мм"], "x")
    assert chk.ok and any(i.code == "contradiction" for i in chk.issues)


def test_diff_keeps_black_norm_instead_of_red_copy():
    from filldocx.template_edit import diff_ops
    text = "Железа обычной формы, контуры четкие, ровные. Атрофически изменена, контуры четкие, ровные."
    s = text.index("Атрофически")
    ops = diff_ops(text, "Железа обычной формы, контуры четкие, ровные.", [(s, len(text))])
    # удаляется только красный вариант, чёрная норма не трогается
    assert all(start >= s - 1 for _, start, _, _ in ops)


def test_split_by_organ_pieces():
    from filldocx.edit_fill import split_by_organ
    assert split_by_organ("поджелудочная железа норма селезенка норма у переднего края селезенки долька 8,9мм") == [
        ("поджелудочная железа норма", "поджелудочная"),
        ("селезенка норма у переднего края селезенки долька 8,9мм", "селезенка")]
    pieces = split_by_organ("плотность печени +72 ед.Н селезенка +53 ед.Н печени однородная")
    assert [o for _, o in pieces] == [None, "селезенка", "печень"]


def test_lost_dictated_numbers_reported():
    from filldocx.edit_fill import lost_dictated_numbers
    iss = lost_dictated_numbers("в нижней чашечке правой почки камень 4,6мм плотностью +1200 ед.Н воротная 16,0",
                                "Воротная вена шириной 16мм.")
    assert len(iss) == 1 and "4,6" in iss[0].message and "1200" in iss[0].message
