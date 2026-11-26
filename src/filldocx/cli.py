"""Командная строка: python -m filldocx <команда>.

  check                         — проверить окружение (ffmpeg, GPU, модели, LLM-сервер)
  process AUDIO DOCX -d ur      — обработать одну диктовку
  transcribe AUDIO -d ur        — только распознать (для подбора модели и словаря)
  add AUDIO DOCX -d ur          — поставить в очередь
  scan FOLDER -d ur             — поставить в очередь все пары «аудио + docx» из папки
  run [-d ur]                   — обработать очередь
  status                        — состояние очереди
  confirm JOB_ID                — подтвердить проверенный документ
  ui                            — окно проверки
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import Settings

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac"}


def _pipeline(args):
    from .pipeline import Pipeline
    s = Settings.load(args.config)
    if getattr(args, "asr", None):
        s.data["asr"]["engine"] = args.asr
    return Pipeline(s)


def _print_issues(issues):
    for i in issues:
        mark = {"error": "[!]", "warning": "[?]", "info": "[i]"}[i.level]
        t = f" @{i.t_start:.0f}с" if i.t_start is not None else ""
        print(f"  {mark} {i.message}{t}")


def cmd_check(args):
    s = Settings.load(args.config)
    ok = True
    print(f"Настройки: {s.root / 'config' / 'settings.yaml'}")
    ff = shutil.which("ffmpeg")
    print(f"ffmpeg: {ff or 'НЕ НАЙДЕН'}")
    ok &= bool(ff)
    try:
        import torch
        cuda = torch.cuda.is_available()
        name = torch.cuda.get_device_name(0) if cuda else "нет"
        print(f"torch {torch.__version__}, CUDA: {name}")
    except ImportError:
        print("torch: не установлен (нужен для GigaAM и Silero VAD)")
        ok = False
    for mod in ("gigaam", "silero_vad", "soundfile"):
        try:
            __import__(mod)
            print(f"{mod}: установлен")
        except ImportError:
            print(f"{mod}: НЕ установлен")
            ok = False
    from .llm import LocalLLM
    c = s["llm"]
    try:
        llm = LocalLLM(c["base_url"], c["model"], allow_remote=c.get("allow_remote", False))
        alive = llm.ping()
    except Exception as e:  # noqa: BLE001
        alive = False
        print(f"LLM: {e}")
    print(f"LLM-сервер {c['base_url']}: {'отвечает' if alive else 'НЕ отвечает (запустите scripts/start_llm.bat)'}")
    ok &= alive
    from .templates import load_all_templates
    print("Шаблоны: " + ", ".join(load_all_templates(s.path("templates_dir"))))
    print("Окружение готово." if ok else "Есть проблемы — см. выше и README.")
    return 0 if ok else 1


def cmd_process(args):
    p = _pipeline(args)
    res = p.process(args.audio, args.docx, args.doctor, args.template, force=args.force)
    print(f"Документ: {res.output_docx}")
    print("Время: " + ", ".join(f"{k} {v:.1f} с" for k, v in res.timings.items()))
    print(f"Журнал: {res.journal}")
    _print_issues(res.issues)
    return 0


def cmd_transcribe(args):
    p = _pipeline(args)
    jd = p.s.work_dir / "transcribe" / Path(args.audio).stem
    jd.mkdir(parents=True, exist_ok=True)
    tr = p.transcribe(args.audio, args.doctor, jd, args.force)
    for c in tr.chunks:
        print(f"[{c.start:7.1f}–{c.end:7.1f}] {c.text}")
    return 0


def cmd_add(args):
    p = _pipeline(args)
    jid = p.store.add_job(str(Path(args.audio).resolve()), str(Path(args.docx).resolve()), args.doctor, args.template)
    print(f"Задание {jid} в очереди")
    return 0


def cmd_scan(args):
    """Пары «аудио + docx» с одинаковым именем (без расширения) в папке."""
    p = _pipeline(args)
    folder = Path(args.folder)
    docx = {f.stem.lower(): f for f in folder.glob("*.docx") if not f.name.startswith("~$")}
    n = 0
    for a in folder.iterdir():
        if a.suffix.lower() in AUDIO_EXT and a.stem.lower() in docx:
            p.store.add_job(str(a.resolve()), str(docx[a.stem.lower()].resolve()), args.doctor, args.template)
            n += 1
    print(f"Добавлено в очередь: {n}")
    return 0


def cmd_run(args):
    _pipeline(args).run_queue(args.doctor, force=args.force)
    return 0


def cmd_status(args):
    p = _pipeline(args)
    for j in p.store.jobs():
        issues = json.loads(j["issues_json"] or "[]")
        n = sum(1 for i in issues if i["level"] != "info")
        print(f"{j['id']:>5}  {j['status']:<13} {j['doctor']:<6} спорных: {n:<3} {Path(j['docx_path']).name}"
              + (f"  ОШИБКА: {j['error']}" if j["error"] else ""))
    return 0


def cmd_confirm(args):
    p = _pipeline(args)
    h = p.confirm(args.job_id)
    print(f"Задание {args.job_id} подтверждено (версия файла {h[:12]})")
    return 0


def cmd_ui(args):
    from .ui.app import main as ui_main
    return ui_main(args.config)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="filldocx", description="Заполнение Word-протоколов по аудиодиктовке (офлайн)")
    ap.add_argument("--config", help="путь к settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)

    def common(sp, doctor=True):
        if doctor:
            sp.add_argument("-d", "--doctor", required=True, help="профиль врача (ключ из settings.yaml: ur, artem)")
        sp.add_argument("-t", "--template", help="id шаблона (kt_obp, kt_ogk, ...); по умолчанию — по имени файла")
        sp.add_argument("--asr", choices=["gigaam", "text"], help="переопределить движок распознавания")
        sp.add_argument("--force", action="store_true", help="не использовать кеш, обработать заново")

    sp = sub.add_parser("process")
    sp.add_argument("audio")
    sp.add_argument("docx")
    common(sp)
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("transcribe")
    sp.add_argument("audio")
    common(sp)
    sp.set_defaults(fn=cmd_transcribe)

    sp = sub.add_parser("add")
    sp.add_argument("audio")
    sp.add_argument("docx")
    common(sp)
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("scan")
    sp.add_argument("folder")
    common(sp)
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("run")
    sp.add_argument("-d", "--doctor")
    sp.add_argument("--asr", choices=["gigaam", "text"])
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_run)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    sp = sub.add_parser("confirm")
    sp.add_argument("job_id", type=int)
    sp.set_defaults(fn=cmd_confirm)

    sub.add_parser("ui").set_defaults(fn=cmd_ui)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
