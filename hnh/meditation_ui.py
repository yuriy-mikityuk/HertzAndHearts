"""Meditation workflow, kept separate from the live biofeedback dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import os

import pyqtgraph as pg
from PySide6.QtCore import QItemSelectionModel, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from hnh.meditation import (
    RRRecording, analyze_session, load_history, metric_value, number,
    trend_points, write_json,
)
from hnh.practice_templates import load_templates
from hnh.session_audio import SessionAudio

PHASE_LABELS = {"before": "Before", "practice": "Meditation", "after": "After"}


def display(value, digits=2):
    parsed = number(value)
    return "—" if parsed is None else f"{parsed:.{digits}f}"


def optional_spin(maximum, *, decimals=0, suffix=""):
    widget = QDoubleSpinBox() if decimals else QSpinBox()
    if decimals:
        widget.setDecimals(decimals)
    widget.setRange(-1, maximum)
    widget.setSpecialValueText("Not recorded")
    widget.setValue(-1)
    widget.setSuffix(suffix)
    return widget


def choice(options):
    widget = QComboBox()
    for label, value in options:
        widget.addItem(label, value)
    return widget


class DiaryForm(QWidget):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        data = data or {}
        layout = QFormLayout(self)
        self.technique = QLineEdit(data.get("technique", ""))
        self.technique.setPlaceholderText("e.g. breath attention, body scan")
        self.category = choice([("Other practice", "other"), ("Breathing exercise", "breathing"),
                                ("Attention meditation", "attention")])
        self.category.setCurrentIndex(max(0, self.category.findData(data.get("category", "other"))))
        self.posture = choice([
            ("Not recorded", ""), ("Seated", "seated"), ("Lying down", "lying"),
            ("Standing", "standing"), ("Other", "other"),
        ])
        self.breathing = choice([
            ("Not recorded", ""), ("Natural breathing", "natural"),
            ("Paced breathing", "paced"), ("Other", "other"),
        ])
        self.rate = optional_spin(60, decimals=1, suffix=" breaths/min")
        self.sleep = optional_spin(24, decimals=1, suffix=" h")
        self.caffeine = choice([
            ("Not recorded", ""), ("No caffeine before this session", "none"),
            ("Caffeine before this session", "yes"),
        ])
        self.caffeine_minutes = optional_spin(1440, suffix=" min ago")
        for widget, key in ((self.posture, "posture"), (self.breathing, "breathing"),
                            (self.caffeine, "caffeine")):
            widget.setCurrentIndex(max(0, widget.findData(data.get(key, ""))))
        for widget, key in ((self.rate, "breathing_rate"), (self.sleep, "sleep_hours"),
                            (self.caffeine_minutes, "caffeine_minutes")):
            value = number(data.get(key))
            if value is not None:
                widget.setValue(value if isinstance(widget, QDoubleSpinBox) else int(value))
        layout.addRow("Practice", self.technique)
        layout.addRow("Practice type", self.category)
        layout.addRow("Posture", self.posture)
        layout.addRow("Breathing during practice", self.breathing)
        layout.addRow("Paced breathing rate", self.rate)
        self.breathing.currentIndexChanged.connect(
            lambda: self.rate.setEnabled(self.breathing.currentData() == "paced")
        )
        self.rate.setEnabled(self.breathing.currentData() == "paced")
        layout.addRow("Last night's sleep", self.sleep)
        layout.addRow("Caffeine", self.caffeine)
        layout.addRow("Time since caffeine", self.caffeine_minutes)
        self.caffeine.currentIndexChanged.connect(
            lambda: self.caffeine_minutes.setEnabled(self.caffeine.currentData() == "yes")
        )
        self.caffeine_minutes.setEnabled(self.caffeine.currentData() == "yes")
        self.ratings = {}
        for phase in ("before", "after"):
            layout.addRow(QLabel(f"<b>{phase.title()} the session · optional ratings 0–10</b>"))
            for key, label in (
                ("attention", "Ease of keeping / returning attention"),
                ("tension", "Tension"), ("sleepiness", "Sleepiness"),
            ):
                field = f"{key}_{phase}"
                widget = optional_spin(10)
                value = number(data.get(field))
                if value is not None:
                    widget.setValue(int(value))
                self.ratings[field] = widget
                layout.addRow(label, widget)
        self.notes = QTextEdit()
        self.notes.setPlainText(data.get("notes", ""))
        self.notes.setMaximumHeight(80)
        layout.addRow("Notes", self.notes)
        hint = QLabel(
            "HRV describes cardiac variability, not a meditation score. "
            "Keep the before/after posture and breathing conditions consistent."
        )
        hint.setWordWrap(True)
        layout.addRow(hint)

    def values(self):
        def value(widget):
            return widget.value() if widget.value() >= 0 else None
        return {
            "technique": self.technique.text().strip(),
            "category": self.category.currentData(),
            "posture": self.posture.currentData(),
            "breathing": self.breathing.currentData(),
            "breathing_rate": value(self.rate) if self.breathing.currentData() == "paced" else None,
            "sleep_hours": value(self.sleep),
            "caffeine": self.caffeine.currentData(),
            "caffeine_minutes": value(self.caffeine_minutes) if self.caffeine.currentData() == "yes" else None,
            **{key: value(widget) for key, widget in self.ratings.items()},
            "notes": self.notes.toPlainText().strip(),
        }


def scroll_form(form):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(form)
    return area


class MeditationPanel(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.recording = None
        self.draft = {}
        self._draft_profile = host._session_profile_id
        self.cues = False
        self.audio = SessionAudio(self, status=host.show_status)
        self._audio_token = None
        self.draft_template = None
        self.draft_plan = None
        self._capture_error = None
        self._history_dialog = None
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        self.diary_button = QPushButton("Meditation…")
        self.diary_button.setToolTip("Start a before / meditation / after protocol or edit its diary.")
        self.diary_button.clicked.connect(self.open_diary)
        self.phase_label = QLabel("Connect and start a recording to begin a meditation protocol.")
        self.phase_label.setWordWrap(True)
        self.next_button = QPushButton("Next phase")
        self.next_button.setEnabled(False)
        self.next_button.clicked.connect(self.next_phase)
        self.history_button = QPushButton("Meditation history")
        self.history_button.clicked.connect(self.open_history)
        row.addWidget(self.diary_button)
        row.addWidget(self.phase_label, 1)
        row.addWidget(self.next_button)
        row.addWidget(self.history_button)
        host.model.rr_sample.connect(self.capture_sample)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def begin(self, bundle):
        self._capture_error = None
        self.audio.stop()
        self._audio_token = None
        try:
            self.recording = RRRecording(bundle.session_dir, bundle.session_id)
            self.recording.metadata["profile_id"] = self.host._session_profile_id
            self.recording.persist()
            self.refresh()
            return True
        except OSError as exc:
            self._capture_error = str(exc)
            if self.recording is not None and self.recording.active:
                self.recording.file.close()
                self.recording.active = False
            self.host.show_status(f"Cannot record original RR data: {exc}")
            return False

    @Slot(object)
    def capture_sample(self, sample):
        if self.recording is None or not self.recording.active:
            return
        try:
            self.recording.add_sample(sample)
        except OSError as exc:
            self._capture_error = str(exc)
            # Stop the session on a write failure; never display it as a
            # successful continuous recording.
            self.host.show_status(f"Original RR recording failed: {exc}")
            self.host.finalize_session(show_message=False, build_final_report=False)

    def gap(self):
        if self.recording is not None and self.recording.active:
            self.recording.mark_gap()

    def finish(self, state):
        if self.recording is not None and self.recording.active:
            try:
                self.recording.finish("capture_error" if self._capture_error else state)
                if not self._capture_error and self.recording.metadata.get("protocol"):
                    self._announce(self.recording, "completed" if self.recording.metadata.get("protocol_completed") else "stopped")
            except (OSError, ValueError) as exc:
                self._capture_error = str(exc)
                self.host.show_status(f"Meditation analysis could not be saved: {exc}")
        self.refresh()

    def _announce(self, record, event):
        token = (id(record), event)
        if token != self._audio_token:
            self._audio_token = token
            self.audio.play(event, (record.metadata.get("protocol") or {}).get("audio_mode", "off"))

    def refresh(self):
        record = self.recording
        same_profile = record and record.metadata.get("profile_id") == self.host._session_profile_id
        phase = record.phase if same_profile else None
        if record and record.active:
            try:
                done = record.tick()
            except OSError as exc:
                self._capture_error = str(exc)
                self.host.finalize_session(show_message=False, build_final_report=False)
                return
            phase = record.phase
            if done:
                self.host.finalize_session(show_message=False, build_final_report=False)
                return
            if same_profile and phase:
                self._announce(record, phase["name"])
        active = bool(same_profile and record.active)
        self.next_button.setEnabled(active and phase is not None)
        self.history_button.setEnabled(not active)
        if self._capture_error:
            self.phase_label.setText(f"Recording/analysis error: {self._capture_error}")
        elif active and phase:
            elapsed = max(0, int(record.elapsed - phase["start_sec"]))
            target = int(record.metadata["protocol"][phase["name"] + "_seconds"])
            self.phase_label.setText(
                f"{PHASE_LABELS[phase['name']]} · {elapsed // 60:02d}:{elapsed % 60:02d}"
                f" / {target // 60:02d}:{target % 60:02d}"
            )
            self.next_button.setText("Finish & save" if phase["name"] == "after" else "Next phase")
        elif active:
            self.phase_label.setText("RR recording active. Settle comfortably, then begin baseline.")
        elif same_profile and record.metadata.get("protocol"):
            self.phase_label.setText("Saved. Add after-session ratings in Meditation…")
        else:
            self.phase_label.setText("Connect and start a recording to begin a meditation protocol.")

    def next_phase(self):
        if not self.recording or not self.recording.active:
            return
        try:
            done = self.recording.advance()
        except OSError as exc:
            self._capture_error = str(exc)
            self.host.finalize_session(show_message=False, build_final_report=False)
            return
        if done:
            self.host.finalize_session(show_message=False, build_final_report=False)
        self.refresh()

    def open_diary(self):
        if self._draft_profile != self.host._session_profile_id:
            self.draft = {}
            self.cues = False
            self.draft_template = None
            self.draft_plan = None
            self._draft_profile = self.host._session_profile_id
        record = self.recording
        if record and record.metadata.get("profile_id") != self.host._session_profile_id:
            record = None
        has_protocol = bool(record and record.metadata.get("protocol"))
        dialog = QDialog(self)
        dialog.setWindowTitle("Meditation session")
        dialog.resize(590, 720)
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Before → practice → after. Choose a saved practice template or set the durations.\n"
            "Begin after settling into your usual posture. Early/poor-quality "
            "windows are shown but excluded from comparisons."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = DiaryForm(record.metadata["diary"] if has_protocol else self.draft)
        template_row = QHBoxLayout()
        template_combo = QComboBox()
        template_apply = QPushButton("Применить шаблон")
        template_reload = QPushButton("Обновить шаблоны")
        template_notice = QLabel()
        template_notice.setWordWrap(True)
        template_row.addWidget(template_combo, 1)
        template_row.addWidget(template_apply)
        template_row.addWidget(template_reload)
        layout.addLayout(template_row)
        layout.addWidget(template_notice)
        layout.addWidget(scroll_form(form), 1)
        plan = QHBoxLayout()
        durations = {}
        for name, suffix, default in (("before", " мин до", 5), ("practice", " мин практика", 16), ("after", " мин после", 5)):
            field = QSpinBox()
            field.setRange(1, 120)
            field.setValue(int(record.metadata["protocol"][name + "_seconds"] / 60) if has_protocol
                           else (self.draft_plan or {}).get(name, default))
            field.setSuffix(suffix)
            field.setEnabled(not has_protocol)
            durations[name] = field
            plan.addWidget(field)
        automatic = QCheckBox("Automatic transitions and stop")
        automatic.setChecked(record.metadata["protocol"]["automatic"] if has_protocol else True)
        automatic.setEnabled(not has_protocol)
        layout.addLayout(plan)
        layout.addWidget(automatic)
        sound_row = QHBoxLayout()
        audio_mode = choice([("Голосовые сообщения", "voice"), ("Звуковые сигналы", "tone"), ("Без звука", "off")])
        if has_protocol:
            audio_mode.setCurrentIndex(max(0, audio_mode.findData(record.metadata["protocol"].get("audio_mode", "off"))))
        elif self.draft_plan:
            audio_mode.setCurrentIndex(max(0, audio_mode.findData(self.draft_plan["audio_mode"])))
            automatic.setChecked(self.draft_plan["automatic"])
        sound_test = QPushButton("Проверить звук")
        sound_test.clicked.connect(lambda: self.audio.play("test", audio_mode.currentData()))
        sound_test.setEnabled(audio_mode.currentData() != "off")
        audio_mode.currentIndexChanged.connect(lambda: sound_test.setEnabled(audio_mode.currentData() != "off"))
        audio_mode.currentIndexChanged.connect(lambda: self.audio.stop() if audio_mode.currentData() == "off" else None)
        sound_row.addWidget(audio_mode)
        sound_row.addWidget(sound_test)
        layout.addLayout(sound_row)

        def reload_templates():
            selected = (template_combo.currentData() or {}).get("id")
            store = Path(os.environ.get("HRV_REVIEW_STORE", Path.home() / ".local/share/hrv-review"))
            items, errors = load_templates(store)
            template_combo.clear()
            template_combo.addItem("Без шаблона", None)
            for item in items:
                template_combo.addItem(item["name"], item)
                if item["id"] == (selected or (self.draft_template or {}).get("id") or ("coherence" if not self.draft else "")):
                    template_combo.setCurrentIndex(template_combo.count() - 1)
            template_notice.setText("; ".join(errors) if errors else "Шаблоны сохраняются в HRV Review → Шаблоны практик.")

        def apply_template():
            item = template_combo.currentData()
            if not item or has_protocol:
                self.draft_template = None if not has_protocol else self.draft_template
                return
            self.draft_template = dict(item)
            for name, field in durations.items():
                field.setValue(item[name + "_minutes"])
            form.technique.setText(item["technique"])
            for field, name in ((form.category, "category"), (form.posture, "posture"), (form.breathing, "breathing")):
                field.setCurrentIndex(max(0, field.findData(item.get(name))))
            form.rate.setValue(item.get("breathing_rate") or -1)
            if not form.notes.toPlainText().strip():
                form.notes.setPlainText(item.get("description", ""))
            audio_mode.setCurrentIndex(max(0, audio_mode.findData(item["audio_mode"])))
            automatic.setChecked(item["automatic"])
        template_reload.clicked.connect(reload_templates)
        template_apply.clicked.connect(apply_template)
        reload_templates()
        for widget in (template_combo, template_apply, template_reload):
            widget.setEnabled(not has_protocol)
        if not has_protocol and not self.draft:
            apply_template()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        action = buttons.addButton(
            "Save diary" if has_protocol else "Begin baseline",
            QDialogButtonBox.ActionRole,
        )
        action.setEnabled(has_protocol or bool(record and record.active))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def apply():
            values = form.values()
            if values["breathing"] == "paced" and not values["breathing_rate"]:
                QMessageBox.information(dialog, "Breathing rate", "Enter the paced breathing rate.")
                return
            try:
                if has_protocol:
                    record.metadata["diary"] = values
                    record.metadata["protocol"]["audio_mode"] = audio_mode.currentData()
                    record.persist()
                    if not record.active:
                        analyze_session(record.directory)
                else:
                    record.begin_protocol(durations["practice"].value(), automatic.isChecked(), values,
                        before_minutes=durations["before"].value(), after_minutes=durations["after"].value(),
                        audio_mode=audio_mode.currentData(), template=self.draft_template)
                self.cues = audio_mode.currentData() != "off"
                self.draft = {}
                dialog.accept()
                self.refresh()
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, "Could not save", str(exc))
        action.clicked.connect(apply)
        if not record:
            layout.addWidget(QLabel("Connect the sensor and start a recording before beginning baseline."))
        dialog.exec()
        if not has_protocol and (not record or not record.metadata.get("protocol")):
            self.draft = form.values()
            self.draft_plan = {name: field.value() for name, field in durations.items()}
            self.draft_plan.update(audio_mode=audio_mode.currentData(), automatic=automatic.isChecked())

    def open_history(self):
        # Keep the dialog alive across opens. A nested exec() loop and transient
        # accessible table children are fragile under macOS AX traversal.
        if self._history_dialog is None:
            self._history_dialog = MeditationHistory(
                self.host._profile_store, self.host._session_profile_id, parent=self
            )
        else:
            self._history_dialog.profile = self.host._session_profile_id
            self._history_dialog.setWindowTitle(f"Meditation history · {self.host._session_profile_id}")
            self._history_dialog.reload()
        self._history_dialog.open()


class MeditationHistory(QDialog):
    def __init__(self, store, profile, parent=None):
        super().__init__(parent)
        self.store, self.profile = store, profile
        self.records = []
        self.visible_records = []
        self.setWindowTitle(f"Meditation history · {profile}")
        self.resize(1020, 760)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        filters = QHBoxLayout()
        self.group = QComboBox()
        self.group.setMinimumWidth(350)
        self.days = choice([("All dates", 0), ("Last 30 days", 30), ("Last 90 days", 90)])
        self.caffeine = choice([
            ("Any caffeine status", None), ("No caffeine", "none"),
            ("Caffeine", "yes"), ("Caffeine not recorded", ""),
        ])
        self.sleep = optional_spin(24, decimals=1, suffix=" h minimum sleep")
        self.sleep.setSpecialValueText("Any sleep duration")
        filters.addWidget(self.group, 1)
        filters.addWidget(self.days)
        filters.addWidget(self.caffeine)
        filters.addWidget(self.sleep)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Started", "Practice", "Before RMSSD", "Practice RMSSD", "After RMSSD", "Δ RMSSD",
            "Δ lnRMSSD", "Sleep / caffeine", "Usable 5-min windows",
        ])
        # Use a current cell, not a native selected-cell collection. Qt/Cocoa
        # can dereference stale cell interfaces in accessibilitySelectedChildren
        # while an accessibility client enumerates a selected table row.
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setToolTip("Click a row or use the arrow keys to choose the session shown below.")
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        plot_controls = QHBoxLayout()
        self.metric_combo = choice([
            ("Before RMSSD (ms)", "before.rmssd_ms"),
            ("Before lnRMSSD", "before.ln_rmssd"),
            ("Meditation RMSSD (ms)", "practice.rmssd_ms"),
            ("After RMSSD (ms)", "after.rmssd_ms"),
            ("After − before RMSSD (ms)", "delta.rmssd_ms"),
            ("After − before lnRMSSD", "delta.ln_rmssd"),
            ("Before SDNN (ms)", "before.sdnn_ms"),
            ("Before mean HR (bpm)", "before.mean_hr_bpm"),
        ])
        self.weekly = QCheckBox("Weekly medians")
        plot_controls.addWidget(self.metric_combo)
        plot_controls.addWidget(self.weekly)
        plot_controls.addStretch()
        layout.addLayout(plot_controls)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()}, background="w")
        self.plot.setMinimumHeight(190)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        layout.addWidget(self.plot, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        for text, slot in (
            ("Edit diary", self.edit_diary), ("Recalculate", self.recalculate),
            ("Open session folder", self.open_folder),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        for widget in (self.group, self.days, self.caffeine, self.metric_combo):
            widget.currentIndexChanged.connect(self.apply_filters)
        self.sleep.valueChanged.connect(self.apply_filters)
        self.weekly.toggled.connect(self.apply_filters)
        self.table.currentCellChanged.connect(lambda *_: self.show_details())
        self.reload()

    def reload(self):
        old_group = self.group.currentData()
        self.records, legacy, errors = load_history(
            self.store.list_sessions(self.profile, include_hidden=False, limit=10000)
        )
        groups = sorted({record["group"] for record in self.records if record["group"]}, key=str)
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem("All groups · choose one to plot", None)
        for group in groups:
            technique, posture, breathing, rate, period, phase_counts = group
            label = f"{technique} / {posture} / {breathing}"
            if rate is not None:
                label += f" {rate:g}/min"
            label += f" / {period} / {'–'.join(str(n * 5) for n in phase_counts)} min analyzed"
            self.group.addItem(label, group)
        index = next((i for i in range(self.group.count())
                      if self.group.itemData(i) == old_group and old_group is not None), -1)
        self.group.setCurrentIndex(index if index >= 0 else (1 if len(groups) == 1 else 0))
        self.group.blockSignals(False)
        self.summary.setText(
            f"{len(self.records)} meditation sessions. {legacy} older sessions have no original RR capture; "
            f"{errors} sessions could not be read.\n"
            "Compare one practice, posture, breathing mode/rate, time of day and equal analyzed phase durations. "
            "Use sleep/caffeine filters as needed. Trends use completed protocols with usable full windows; "
            "they are physiological observations, not a meditation score."
        )
        self.apply_filters()

    def apply_filters(self, *_):
        group = self.group.currentData()
        days = self.days.currentData() or 0
        cutoff = datetime.now().astimezone() - timedelta(days=days) if days else None
        records = []
        for record in self.records:
            metadata = record["metadata"]
            diary = metadata.get("diary") or {}
            started = datetime.fromisoformat(metadata["started_at"])
            if started.tzinfo is None:
                started = started.astimezone()
            if group is not None and record["group"] != group:
                continue
            if cutoff and started < cutoff:
                continue
            if self.caffeine.currentData() is not None and diary.get("caffeine", "") != self.caffeine.currentData():
                continue
            sleep = number(diary.get("sleep_hours"))
            if self.sleep.value() >= 0 and (sleep is None or sleep < self.sleep.value()):
                continue
            records.append(record)
        records.sort(key=lambda r: r["metadata"]["started_at"], reverse=True)
        self.visible_records = records
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            metadata, analysis = record["metadata"], record["analysis"]
            diary = metadata.get("diary") or {}
            usable = [f"{name}: {analysis.get('phases', {}).get(name, {}).get('usable_windows', 0)}"
                      for name in ("before", "practice", "after")]
            values = [
                metadata["started_at"][:19].replace("T", " "),
                diary.get("technique") or "Not recorded",
                display(metric_value(record, "before.rmssd_ms")),
                display(metric_value(record, "practice.rmssd_ms")),
                display(metric_value(record, "after.rmssd_ms")),
                display(metric_value(record, "delta.rmssd_ms")),
                display(metric_value(record, "delta.ln_rmssd")),
                f"{display(diary.get('sleep_hours'), 1)} h / {diary.get('caffeine') or 'unknown'}",
                "; ".join(usable),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.plot.clear()
        points = trend_points(records, group, self.metric_combo.currentData(), self.weekly.isChecked())
        if points:
            x, y = zip(*points)
            self.plot.plot(x, y, pen=pg.mkPen("#287f8d", width=2), symbol="o", symbolSize=7)
        self.plot.setTitle(
            self.metric_combo.currentText() if group is not None
            else "Choose a comparison group; different conditions are not pooled"
        )
        if records:
            self.table.setCurrentCell(0, 0, QItemSelectionModel.NoUpdate)
            self.show_details()
        else:
            self.details.setPlainText("No sessions match these filters.")

    def selected(self):
        row = self.table.currentRow()
        return self.visible_records[row] if 0 <= row < len(self.visible_records) else None

    def show_details(self):
        record = self.selected()
        if not record:
            return
        metadata, analysis = record["metadata"], record["analysis"]
        diary = metadata.get("diary") or {}
        lines = [
            f"Session {metadata['session_id']} · {analysis.get('analysis_version')}",
            "Phase values are medians of complete usable 5-minute windows.",
        ]
        if not metadata.get("protocol_completed"):
            lines.append("Protocol stopped early; missing/short phases have no comparable value.")
        if record.get("stale"):
            lines.append("Analysis is out of date. Recalculate before using this session in trends.")
        elif not analysis.get("comparable"):
            lines.append("Excluded from trends: incomplete protocol or an unusable full window.")
        for window in analysis.get("windows", []):
            lines.append(
                f"{PHASE_LABELS.get(window['phase'], window['phase'])} "
                f"{window['start_sec'] / 60:.1f}–{window['end_sec'] / 60:.1f} min: "
                f"{'usable' if window['usable'] else window['reason']}; "
                f"coverage {window['coverage']:.0%}, excluded beats {window['excluded_beats']}."
            )
        for key in ("attention", "tension", "sleepiness"):
            lines.append(f"{key.title()}: {display(diary.get(key + '_before'), 0)} → "
                         f"{display(diary.get(key + '_after'), 0)}")
        if diary.get("notes"):
            lines.append("Notes: " + diary["notes"])
        self.details.setPlainText("\n".join(lines))

    def edit_diary(self):
        record = self.selected()
        if not record:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit meditation diary")
        dialog.resize(570, 680)
        layout = QVBoxLayout(dialog)
        form = DiaryForm(record["metadata"].get("diary"), dialog)
        layout.addWidget(scroll_form(form))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        metadata = dict(record["metadata"])
        metadata["diary"] = form.values()
        try:
            write_json(record["directory"] / "meditation.json", metadata)
            analyze_session(record["directory"])
            self.reload()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not save diary", str(exc))

    def recalculate(self):
        record = self.selected()
        if record:
            try:
                analyze_session(record["directory"])
                self.reload()
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Could not recalculate", str(exc))

    def open_folder(self):
        record = self.selected()
        if record:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record["directory"])))
