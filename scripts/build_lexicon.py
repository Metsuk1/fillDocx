"""Пополнение словаря терминов из архива проверенных протоколов.

    python scripts/build_lexicon.py "D:\\архив\\ур гот" "D:\\архив\\арт гот" --min-count 5

Читает все .docx в папках (рекурсивно), собирает слова длиной от 7 букв, которые
встречаются не реже --min-count раз, и записывает их в config/lexicon/terms_archive.yaml.
Тексты протоколов никуда не отправляются и в репозиторий не попадают: в файл пишутся
только отдельные частые слова (медицинская лексика), без Ф.И.О. — слова с заглавной
буквы в середине предложения отбрасываются.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

import yaml
from docx import Document


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--min-count", type=int, default=5)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "config" / "lexicon" / "terms_archive.yaml"))
    args = ap.parse_args()

    counter: collections.Counter[str] = collections.Counter()
    n_docs = 0
    for folder in args.folders:
        for path in Path(folder).rglob("*.docx"):
            if path.name.startswith("~$"):
                continue
            try:
                doc = Document(str(path))
            except Exception:  # noqa: BLE001 — повреждённые файлы пропускаем
                continue
            n_docs += 1
            for p in doc.paragraphs:
                if re.match(r"\s*(Ф\.И\.О|Дата|Врач)", p.text):
                    continue
                for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([А-ЯЁ]?[а-яё-]{6,})\b", p.text):
                    w = m.group(1)
                    if w[0].isupper():
                        continue  # возможная фамилия
                    counter[w.lower()] += 1
    terms = sorted(w for w, c in counter.items() if c >= args.min_count)
    Path(args.out).write_text(
        "# Сгенерировано scripts/build_lexicon.py из архива проверенных протоколов.\n"
        + yaml.safe_dump({"terms": terms}, allow_unicode=True, width=120),
        encoding="utf-8",
    )
    print(f"Документов: {n_docs}, терминов: {len(terms)} → {args.out}")
    print("Файл подключается автоматически при следующем запуске.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
