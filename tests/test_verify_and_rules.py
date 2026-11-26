"""Проверка источника и движок правил: «не упомянуто — не вписывается»."""

from filldocx.rules import build_operations, conclusion_hints
from filldocx.schema import Extraction, FillSlot, InsertParagraph, SetField
from filldocx.verify import verify_conclusion, verify_extraction

BODY = (
    "на полученных кт изображениях печень обычной формы и размеров кранио-каудальный размер 173,4мм "
    "плотность паренхимы +63 ед.Н структура однородная. поджелудочная железа норма контуры сглажены. "
    "воротная вена 10,0, селезеночная вена 9,0мм. в костном окне поясничный лордоз выражен, "
    "краевые костные разрастания, артроз дугоотростчатых суставов."
)


def ext(**kw):
    return Extraction.model_validate({"findings": [], "conclusion": None, "offtopic": [], "unclear": [], **kw})


def test_valid_findings_pass(obp):
    e = ext(findings=[
        {"organ": "liver", "status": "norm", "text": "", "quote": "печень обычной формы и размеров кранио-каудальный размер 173,4мм плотность паренхимы +63 ед.Н",
         "slots": [{"slot": "cc_size", "values": ["173,4"]}, {"slot": "density", "values": ["63"]}]},
        {"organ": "pancreas", "status": "changed", "slots": [], "quote": "поджелудочная железа норма контуры сглажены",
         "text": "Поджелудочная железа обычной формы, не увеличена в размерах, контуры сглажены, структура однородная."},
    ])
    organs, issues = verify_extraction(e, obp, BODY)
    assert organs["liver"].slots == {"cc_size": ["173,4"], "density": ["63"]}
    assert organs["pancreas"].texts
    assert not [i for i in issues if i.level == "error"]


def test_hallucinated_quote_is_rejected(obp):
    e = ext(findings=[{"organ": "spleen", "status": "changed", "text": "Селезенка увеличена.", "slots": [],
                       "quote": "селезенка увеличена в размерах"}])
    organs, issues = verify_extraction(e, obp, BODY)
    assert "spleen" not in organs
    assert issues[0].code == "quote_not_found"


def test_number_not_in_quote_is_rejected(obp):
    e = ext(findings=[{"organ": "liver", "status": "norm", "text": "", "quote": "кранио-каудальный размер 173,4мм",
                       "slots": [{"slot": "cc_size", "values": ["174,3"]}]}])
    organs, issues = verify_extraction(e, obp, BODY)
    assert "liver" not in organs or not organs["liver"].slots
    assert any(i.code == "slot_value_not_in_quote" for i in issues)


def test_invented_words_are_rejected(obp):
    e = ext(findings=[{"organ": "pancreas", "status": "changed", "slots": [],
                       "quote": "поджелудочная железа норма контуры сглажены",
                       "text": "Поджелудочная железа увеличена, с кистозным образованием головки, контуры сглажены."}])
    organs, issues = verify_extraction(e, obp, BODY)
    assert "pancreas" not in organs
    assert any(i.code == "text_not_supported" for i in issues)


def test_late_correction_last_value_wins(obp):
    body = BODY + " дополнить описание печени кранио-каудальный размер 175,0мм."
    e = ext(findings=[
        {"organ": "liver", "status": "norm", "text": "", "quote": "кранио-каудальный размер 173,4мм", "slots": [{"slot": "cc_size", "values": ["173,4"]}]},
        {"organ": "liver", "status": "norm", "text": "", "quote": "кранио-каудальный размер 175,0мм", "slots": [{"slot": "cc_size", "values": ["175,0"]}]},
    ])
    organs, issues = verify_extraction(e, obp, body)
    assert organs["liver"].slots["cc_size"] == ["175,0"]
    assert any(i.code == "value_changed_later" for i in issues)


def test_not_mentioned_organ_produces_no_operations(obp):
    organs, _ = verify_extraction(ext(), obp, BODY)
    ops, _ = build_operations(obp, organs)
    assert ops == []


def test_operations_for_slots_formed_and_clause_rules(obp):
    e = ext(findings=[
        {"organ": "liver", "status": "norm", "text": "", "quote": "кранио-каудальный размер 173,4мм плотность паренхимы +63 ед.Н",
         "slots": [{"slot": "cc_size", "values": ["173,4"]}, {"slot": "density", "values": ["63"]}]},
        {"organ": "veins", "status": "changed", "text": "", "quote": "воротная вена 10,0, селезеночная вена 9,0мм",
         "slots": [{"slot": "portal", "values": ["10,0"]}, {"slot": "splenic", "values": ["9,0"]}]},
        {"organ": "bone_window", "status": "changed", "slots": [],
         "quote": "в костном окне поясничный лордоз выражен, краевые костные разрастания, артроз дугоотростчатых суставов",
         "text": "В костном окне поясничный лордоз выражен, краевые костные разрастания, артроз дугоотростчатых суставов."},
    ])
    organs, issues = verify_extraction(e, obp, BODY)
    ops, _ = build_operations(obp, organs, birth_date="01.04.1964")

    fills = [o for o in ops if isinstance(o, FillSlot)]
    assert {(o.before, o.value) for o in fills} == {("составляет", " 173,4"), ("плотность паренхимы", " +63 ")}
    inserts = {o.organ: "".join(s.text for s in o.segments) for o in ops if isinstance(o, InsertParagraph)}
    assert inserts["veins"] == "Воротная вена шириной 10,0мм, селезеночная вена 9,0мм."          # правило 21
    assert inserts["bone_window"] == (                                                           # правила 47–49
        "В костном окне поясничный лордоз выражен. "
        "По передним и боковым контурам тел поясничных позвонков определяются краевые костные разрастания. "
        "На исследуемых уровнях суставные щели дугоотростчатых суставов сужены, сочленяющиеся поверхности "
        "склерозированы с краевыми костными разрастаниями."
    )
    assert any(isinstance(o, SetField) and o.value == "01.04.1964" for o in ops)


def test_virtual_organ_goes_after_host_in_template_order(obp):
    e = ext(findings=[
        {"organ": "stomach", "status": "changed", "slots": [], "quote": "стенки желудка утолщены",
         "text": "Стенки желудка утолщены."},
        {"organ": "duodenum", "status": "changed", "slots": [], "quote": "двенадцатиперстная кишка расширена",
         "text": "Двенадцатиперстная кишка расширена."},
    ])
    body = "стенки желудка утолщены. двенадцатиперстная кишка расширена."
    organs, _ = verify_extraction(e, obp, body)
    ops, _ = build_operations(obp, organs)
    by_organ = {o.organ: o for o in ops}
    assert list(organs) == ["duodenum", "stomach"]                  # порядок шаблона, не диктовки
    assert by_organ["duodenum"].host_anchors[0] == "Поджелудочная железа"   # правило 31
    assert by_organ["stomach"].host_anchors[0] == "Селезенка"
    assert by_organ["duodenum"].order < by_organ["stomach"].order


def test_conclusion_and_recommendations(obp):
    raw = "кт-картина хронического холецистопанкреатита. не исключается заболевание (c-r?)"
    text, issues = verify_conclusion("КТ-картина хронического холецистопанкреатита. Не исключается заболевание (c-r?).", raw)
    assert text and not [i for i in issues if i.level == "error"]
    bad, issues = verify_conclusion("КТ-картина хронического холецистита. Метастазы в печени.", raw)
    assert bad is None and issues[0].code == "conclusion_not_supported"

    ops, _ = build_operations(obp, {}, conclusion=text, recommendations="консультация уролога, кт абдоминального сегмента")
    concl, reco = [o for o in ops if isinstance(o, InsertParagraph)]
    assert concl.segments[0].text == "Заключение: " and concl.segments[0].bold                # правило 45
    assert reco.align == "center" and reco.segments[0].italic                                 # правило 46
    assert reco.segments[0].text == "Рекомендовано: консультация уролога, КТ абдоминального сегмента."


def test_conclusion_hints_only_suggest(obp):
    hints = conclusion_hints(obp, "Структура тел поясничных позвонков поротически изменена.", "КТ-картина остеохондроза.")
    assert hints and hints[0].level == "info" and "остеопороз" in hints[0].message
