"""Сквозной тест конвейера без моделей: готовая расшифровка + фиксированный ответ LLM."""

import datetime as dt
import json

from docx import Document

from filldocx.asr import TimedChunk, Transcript
from filldocx.config import Settings
from filldocx.llm import FixedExtractor
from filldocx.pipeline import Pipeline
from filldocx.segment import split_dictation
from filldocx.storage import Store

from conftest import ROOT

# Диктовка в стиле вывода GigaAM: строчные буквы, числа словами, без пунктуации.
CHUNKS = [
    (0.0, 9.5, "пациент тестов тест нуль один нуль четыре тысяча девятьсот шестьдесят четыре "
               "кт абдоминального сегмента контраст не введен"),
    (9.5, 24.0, "на полученных кт изображениях печень обычной формы и размеров окно кодальный размер "
                "сто семьдесят три запятая четыре плотность паренхимы шестьдесят три структура однородная"),
    (24.0, 31.0, "вы завтра сможете забрать да вот сюда зайдите"),
    (31.0, 44.0, "воротная вена десять запятая ноль селезеночная вена девять запятая ноль "
                 "поджелудочная железа норма контуры сглажены"),
    (44.0, 58.0, "заключение кт картина хронического панкреатита рекомендовано консультация гастроэнтеролога "
                 "всё на этом"),
]

LLM_ANSWER = {
    "findings": [
        {"organ": "liver", "status": "norm", "text": "",
         "slots": [{"slot": "cc_size", "values": ["173,4"]}, {"slot": "density", "values": ["63"]}],
         "quote": "печень обычной формы и размеров кранио-каудальный размер 173,4мм плотность паренхимы +63 ед.Н"},
        {"organ": "veins", "status": "changed", "text": "",
         "slots": [{"slot": "portal", "values": ["10,0"]}, {"slot": "splenic", "values": ["9,0"]}],
         "quote": "воротная вена 10,0мм селезеночная вена 9,0мм"},
        {"organ": "pancreas", "status": "changed",
         "text": "Поджелудочная железа обычной формы, не увеличена в размерах, контуры сглажены, структура однородная.",
         "slots": [], "quote": "поджелудочная железа норма контуры сглажены"},
        # галлюцинация: селезенку врач не диктовал — должна быть отброшена проверкой
        {"organ": "spleen", "status": "changed", "text": "Селезенка увеличена.", "slots": [],
         "quote": "селезенка увеличена"},
    ],
    "conclusion": "КТ-картина хронического панкреатита.",
    "offtopic": ["вы завтра сможете забрать да вот сюда зайдите"],
    "unclear": [],
}


def make_pipeline(tmp_path, extraction=LLM_ANSWER):
    s = Settings.load(ROOT / "config" / "settings.yaml")
    s.data["work_dir"] = str(tmp_path / "work")
    s.data["fill_mode"] = "facts"  # эти тесты — про режим извлечения фактов
    s.data["asr"]["engine"] = "text"
    return Pipeline(s, store=Store(tmp_path / "db.sqlite"), extractor=FixedExtractor(extraction))


def test_end_to_end_fill(obp_docx, tmp_path):
    p = make_pipeline(tmp_path)
    tr = Transcript([TimedChunk(a, b, t) for a, b, t in CHUNKS], "test", "test")
    jd = tmp_path / "job"
    jd.mkdir()
    res = p.fill(tr, obp_docx, "ur", jd, audio_name=obp_docx.name, today=dt.date(2026, 11, 26))

    texts = [par.text for par in Document(str(res.output_docx)).paragraphs]
    full = "\n".join(texts)
    assert "Дата рождения: 01.04.1964" in texts                                     # правило 8
    assert "составляет 173,4мм, плотность паренхимы +63 ед.Н" in full                # правила 17, 18, 23
    assert "Воротная вена шириной 10,0мм, селезеночная вена 9,0мм." in texts         # правило 21
    assert "Поджелудочная железа обычной формы, не увеличена в размерах, контуры сглажены, структура однородная." in texts
    assert "Селезенка увеличена." not in full                                       # не продиктовано — не вписано
    assert "Заключение: КТ-картина хронического панкреатита." in texts               # правило 45
    assert "Рекомендовано: консультация гастроэнтеролога." in texts                 # правило 46
    assert "Заключение: КТ-признаков патологии не выявлено." in texts               # стандартное осталось
    assert "забрать" not in full and "всё на этом" not in full.lower()              # правило 15, §15
    assert "тестов тест" not in "\n".join(texts[2:]).lower()                       # Ф.И.О. не попало в описание
    assert "не введено" in full                                                     # поле контраста не тронуто

    codes = {i.code for i in res.issues}
    assert "quote_not_found" in codes                                               # галлюцинация на проверке
    assert "offtopic" in codes and "not_dictated" in codes and "term_corrected" in codes
    spleen = next(i for i in res.issues if i.code == "quote_not_found")
    assert spleen.organ == "spleen"
    offtopic = next(i for i in res.issues if i.code == "offtopic")
    assert offtopic.t_start == 24.0                                                 # можно прослушать фрагмент

    journal = json.loads(res.journal.read_text("utf-8"))
    assert journal["parts"]["birth_date"] == "01.04.1964"
    assert journal["template"] == "kt_obp"


def test_process_and_reprocess_does_not_duplicate(obp_docx, tmp_path):
    audio = tmp_path / "Тестов Тест кт обп Город.mp3"
    audio.write_bytes(b"fake")
    audio.with_suffix(".txt").write_text(" ".join(c[2] for c in CHUNKS), encoding="utf-8")
    p = make_pipeline(tmp_path)
    p.process(audio, obp_docx, "ur")
    first = [par.text for par in Document(str(obp_docx)).paragraphs]
    p.process(audio, obp_docx, "ur", force=True)                                   # повтор берёт исходную копию
    second = [par.text for par in Document(str(obp_docx)).paragraphs]
    assert first == second
    job = p.store.jobs()[0]
    assert job["status"] == "needs_review"
    p.confirm(job["id"])
    assert p.is_confirmed_current(job["id"])
    doc = Document(str(obp_docx))
    doc.add_paragraph("правка после подтверждения")
    doc.save(str(obp_docx))
    assert not p.is_confirmed_current(job["id"])                                    # нужна повторная проверка


def test_split_dictation_parts():
    text = ("пациент тестов тест 12 07 57 года КТ абдоминального сегмента контраст КТ-скан "
            "печень обычной формы. заключение КТ-картина холецистита. рекомендовано консультация хирурга. пока все")
    parts = split_dictation(text, "Тестов Тест Тестович", today=dt.date(2026, 11, 26))
    assert parts.birth_date.value == "12.07.1957"
    assert parts.contrast_said.lower().startswith("контраст")
    assert parts.study_said.lower().startswith("кт абдоминального")
    assert parts.body.strip().startswith("печень обычной формы")
    assert parts.conclusion == "КТ-картина холецистита"
    assert parts.recommendations_label == "Рекомендовано:" and parts.recommendations == "консультация хирурга"


def test_split_keeps_vse_inside_medical_phrase():
    parts = split_dictation("субарахноидальные пространства умеренно всё расширены. всё на этом")
    assert "умеренно всё расширены" in parts.body                                  # правило 15


def test_late_addition_after_conclusion_goes_back_to_body():
    parts = split_dictation("печень норма. заключение холецистит. дополнить описание печени в V сегменте киста")
    assert parts.conclusion == "холецистит"
    assert parts.late_additions and "V сегменте киста" in parts.late_additions[0][1]
