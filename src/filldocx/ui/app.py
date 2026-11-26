"""Окно проверки (этап 3).

Слева — очередь документов, справа — спорные места выбранного документа.
Каждое спорное место можно прослушать: проигрывается фрагмент исходного аудио.
Документ открывается в Word; после проверки — «Подтвердить». Подтверждение
привязано к сохранённой версии файла: если файл изменили после подтверждения,
статус снова требует проверки. Предупреждения живут только здесь, не в Word.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import Settings
from ..pipeline import Pipeline

STATUS_RU = {
    "queued": "в очереди", "transcribed": "распознано", "filled": "заполнено",
    "needs_review": "на проверке", "confirmed": "подтверждено", "error": "ошибка",
}
LEVEL_MARK = {"error": "✖", "warning": "?", "info": "i"}
LEVEL_COLOR = {"error": QColor("#b00020"), "warning": QColor("#a15c00"), "info": QColor("#555555")}


class QueueWorker(QObject):
    log = Signal(str)
    done = Signal()

    def __init__(self, settings: Settings, doctor: str | None):
        super().__init__()
        self.settings, self.doctor = settings, doctor

    def run(self):
        try:
            # своё подключение к SQLite в рабочем потоке
            Pipeline(self.settings).run_queue(self.doctor, log=self.log.emit)
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"ОШИБКА: {e}")
        self.done.emit()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.setWindowTitle("filldocx — проверка протоколов")
        self.resize(1300, 760)
        self.pipeline = Pipeline(settings)
        self.settings = settings
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.stop_timer = QTimer(self, singleShot=True, timeout=self.player.pause)
        self._thread: QThread | None = None

        # --- верхняя панель ---
        self.doctor = QComboBox()
        self.doctor.addItem("Все врачи", None)
        for key, d in settings["doctors"].items():
            self.doctor.addItem(d.get("display_name", key), key)
        self.doctor.currentIndexChanged.connect(self.refresh)
        btn_scan = QPushButton("Добавить папку…", clicked=self.add_folder)
        self.btn_run = QPushButton("Обработать очередь", clicked=self.run_queue)
        btn_refresh = QPushButton("Обновить", clicked=self.refresh)
        top = QHBoxLayout()
        for w in (QLabel("Врач:"), self.doctor, btn_scan, self.btn_run, btn_refresh):
            top.addWidget(w)
        top.addStretch(1)

        # --- очередь ---
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["№", "Статус", "Врач", "Спорных", "Документ"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_job)

        # --- спорные места ---
        self.job_title = QLabel("Выберите документ")
        self.job_title.setWordWrap(True)
        self.issues = QListWidget()
        self.issues.setWordWrap(True)
        self.issues.itemDoubleClicked.connect(self.play_issue)
        self.btn_play = QPushButton("▶ Прослушать фрагмент", clicked=lambda: self.play_issue(self.issues.currentItem()))
        self.btn_open = QPushButton("Открыть в Word", clicked=self.open_docx)
        self.btn_confirm = QPushButton("Подтвердить документ", clicked=self.confirm)
        btns = QHBoxLayout()
        for b in (self.btn_play, self.btn_open, self.btn_confirm):
            btns.addWidget(b)
        right = QVBoxLayout()
        right.addWidget(self.job_title)
        right.addWidget(self.issues, 1)
        right.addLayout(btns)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(right_w)
        split.setSizes([520, 780])

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumHeight(150)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(split, 1)
        root.addWidget(self.log)
        w = QWidget()
        w.setLayout(root)
        self.setCentralWidget(w)
        self.refresh()

    # --- данные ---
    def current_job(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.pipeline.store.job(int(self.table.item(rows[0].row(), 0).text()))

    def refresh(self):
        jobs = self.pipeline.store.jobs(doctor=self.doctor.currentData())
        self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            issues = json.loads(j["issues_json"] or "[]")
            n = sum(1 for i in issues if i["level"] != "info")
            status = STATUS_RU.get(j["status"], j["status"])
            if j["status"] == "confirmed" and not self.pipeline.is_confirmed_current(j["id"]):
                status = "изменён после подтверждения"
            vals = [str(j["id"]), status, j["doctor"], str(n), Path(j["docx_path"]).name]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 1 and j["status"] == "error":
                    item.setForeground(LEVEL_COLOR["error"])
                self.table.setItem(r, c, item)

    def show_job(self):
        j = self.current_job()
        self.issues.clear()
        if not j:
            return
        title = f"<b>{Path(j['docx_path']).name}</b> — {STATUS_RU.get(j['status'], j['status'])}"
        if j["error"]:
            title += f"<br><span style='color:#b00020'>{j['error']}</span>"
        self.job_title.setText(title)
        issues = json.loads(j["issues_json"] or "[]")
        issues.sort(key=lambda i: {"error": 0, "warning": 1, "info": 2}[i["level"]])
        for i in issues:
            t = f"  [{_fmt_time(i['t_start'])}]" if i.get("t_start") is not None else ""
            rule = f" (правило {i['rule']})" if i.get("rule") else ""
            text = f"{LEVEL_MARK[i['level']]}  {i['message']}{rule}{t}"
            if i.get("quote"):
                text += f"\n      «{i['quote']}»"
            item = QListWidgetItem(text)
            item.setForeground(LEVEL_COLOR[i["level"]])
            item.setData(Qt.UserRole, i)
            self.issues.addItem(item)
        if not issues:
            self.issues.addItem("Спорных мест нет.")

    # --- действия ---
    def play_issue(self, item):
        j = self.current_job()
        if not j or item is None:
            return
        i = item.data(Qt.UserRole) or {}
        if i.get("t_start") is None:
            self.log.appendPlainText("У этого пункта нет метки времени.")
            return
        start = max(0.0, i["t_start"] - 1.0)
        end = (i.get("t_end") or i["t_start"] + 5) + 1.0
        self.player.setSource(QUrl.fromLocalFile(str(Path(j["audio_path"]).resolve())))
        self.player.setPosition(int(start * 1000))
        self.player.play()
        self.stop_timer.start(int((end - start) * 1000))

    def open_docx(self):
        j = self.current_job()
        if j and Path(j["docx_path"]).exists():
            os.startfile(j["docx_path"])  # noqa: S606 — открыть в Word на этом компьютере

    def confirm(self):
        j = self.current_job()
        if not j:
            return
        issues = json.loads(j["issues_json"] or "[]")
        n_err = sum(1 for i in issues if i["level"] == "error")
        if n_err and QMessageBox.question(
            self, "Подтверждение", f"Есть {n_err} неподтверждённых фрагментов. Вы проверили документ вручную?"
        ) != QMessageBox.Yes:
            return
        self.pipeline.confirm(j["id"])
        self.log.appendPlainText(f"Документ {j['id']} подтверждён.")
        self.refresh()

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка с диктовками и подготовленными документами")
        doctor = self.doctor.currentData()
        if not folder:
            return
        if not doctor:
            QMessageBox.information(self, "Врач", "Сначала выберите врача.")
            return
        from ..cli import AUDIO_EXT
        docx = {f.stem.lower(): f for f in Path(folder).glob("*.docx") if not f.name.startswith("~$")}
        n = 0
        for a in Path(folder).iterdir():
            if a.suffix.lower() in AUDIO_EXT and a.stem.lower() in docx:
                self.pipeline.store.add_job(str(a.resolve()), str(docx[a.stem.lower()].resolve()), doctor)
                n += 1
        self.log.appendPlainText(f"Добавлено в очередь: {n}")
        self.refresh()

    def run_queue(self):
        if self._thread is not None:
            return
        self.btn_run.setEnabled(False)
        self._thread = QThread(self)
        self._worker = QueueWorker(self.settings, self.doctor.currentData())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self.log.appendPlainText)
        self._worker.done.connect(self._queue_done)
        self._thread.start()

    def _queue_done(self):
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self.btn_run.setEnabled(True)
        self.refresh()


def _fmt_time(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def main(config: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(Settings.load(config))
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
