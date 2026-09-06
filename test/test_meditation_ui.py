from types import SimpleNamespace
import csv

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

from hnh.meditation_ui import DiaryForm, MeditationHistory, MeditationPanel
from hnh.session_artifacts import create_session_bundle
from test_meditation import Clock, make_session, sample


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class ModelSignals(QObject):
    rr_sample = Signal(object)


class Host(QWidget):
    def __init__(self):
        super().__init__()
        self.model = ModelSignals()
        self._session_profile_id = "Admin"
        self._session_state = "recording"
        self.statuses = []
        self.finalizations = 0
        self.panel = MeditationPanel(self)

    def show_status(self, text):
        self.statuses.append(text)

    def finalize_session(self, **kwargs):
        self.finalizations += 1
        self.panel.finish("finalized")
        self._session_state = "finalized"


def test_diary_distinguishes_missing_from_zero_and_keeps_notes(qapp):
    form = DiaryForm()
    assert form.values()["attention_before"] is None
    form.ratings["attention_before"].setValue(0)
    form.sleep.setValue(0)
    form.notes.setPlainText('Quiet, "sleepy"\nsecond line')
    result = form.values()
    assert result["attention_before"] == result["sleep_hours"] == 0
    assert result["notes"] == 'Quiet, "sleepy"\nsecond line'
    assert result["caffeine"] == ""
    form.close()


def test_recording_signal_to_sidecar_and_automatic_save(qapp, tmp_path):
    host = Host()
    bundle = create_session_bundle(tmp_path)
    assert host.panel.begin(bundle)
    recording = host.panel.recording
    clock = Clock()
    recording.clock = clock
    recording.origin = clock()
    recording.begin_protocol(5, True, {
        "technique": "breath", "posture": "seated", "breathing": "natural"
    })
    for i in range(900):
        clock.value = 1000 + i + 0.5
        host.model.rr_sample.emit(sample(1000, received_perf=clock.value))
        if i in (300, 600):
            host.panel.refresh()
    clock.value = 1900
    host.panel.refresh()
    assert host.finalizations == 1
    assert not recording.active
    assert (bundle.session_dir / "meditation_analysis.json").exists()
    assert host.panel.history_button.isEnabled()
    host.panel.refresh()
    assert host.finalizations == 1
    host.close()


def test_history_filters_and_shows_only_matching_trend(qapp, tmp_path):
    first = make_session(tmp_path / "one", technique="breath attention")
    second = make_session(tmp_path / "two", technique="body scan")
    rows = [{"state": "finalized", "session_dir": str(r.directory)} for r in (first, second)]
    store = SimpleNamespace(list_sessions=lambda *args, **kwargs: rows)
    dialog = MeditationHistory(store, "Admin")
    dialog.show()
    qapp.processEvents()
    assert not dialog.grab().isNull()
    assert dialog.table.rowCount() == 2
    assert dialog.selected() is not None
    assert dialog.table.selectionModel().selectedIndexes() == []
    dialog.table.setCurrentCell(1, 0, QItemSelectionModel.NoUpdate)
    assert dialog.selected() is dialog.visible_records[1]
    assert dialog.table.selectionModel().selectedIndexes() == []
    assert not dialog.plot.listDataItems()
    dialog.group.setCurrentIndex(1)
    assert dialog.table.rowCount() == 1
    assert len(dialog.plot.listDataItems()) == 1
    assert "coverage" in dialog.details.toPlainText()
    dialog.sleep.setValue(9)
    assert dialog.table.rowCount() == 0
    assert not dialog.plot.listDataItems()
    dialog.close()


def test_history_open_is_async_and_reuses_owned_dialog(qapp, tmp_path):
    recording = make_session(tmp_path / "history")
    host = Host()
    host._profile_store = SimpleNamespace(list_sessions=lambda *args, **kwargs: [
        {"state": "finalized", "session_dir": str(recording.directory)}
    ])
    host.panel.open_history()
    dialog = host.panel._history_dialog
    assert dialog.isVisible()
    assert dialog.parent() is host.panel
    assert dialog.table.selectionModel().selectedIndexes() == []
    dialog.close()
    host.panel.open_history()
    assert host.panel._history_dialog is dialog
    assert dialog.isVisible()
    dialog.close()
    host.close()


def test_new_profile_cannot_edit_previous_profiles_diary(qapp, tmp_path):
    host = Host()
    host.panel.begin(create_session_bundle(tmp_path))
    host.panel.recording.finish()
    host._session_profile_id = "Other"
    host.panel.refresh()
    assert "Connect" in host.panel.phase_label.text()
    host.close()


def test_real_view_starts_captures_and_finalizes_sidecars(qapp, tmp_path, monkeypatch):
    from hnh import settings
    from hnh.model import Model
    from hnh.view import View

    monkeypatch.setenv("HNH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(View, "_run_startup_flow", lambda self: None)
    monkeypatch.setattr(View, "_schedule_background_update_check", lambda self: None)
    monkeypatch.setattr(View, "_show_maximized_fit", lambda self: None)
    model = Model()
    view = View(model)
    view.settings.OPEN_SESSION_FOLDER_ON_SAVE = False
    monkeypatch.setattr(view, "_is_sensor_connected", lambda: True)
    view.start_session(auto=True)
    try:
        for _ in range(10):
            qapp.processEvents()
        model.update_ibis_buffer(999.0234375)
        model.update_ibis_buffer(1000.9765625)
        directory = view._session_bundle.session_dir
        assert view.meditation_panel.recording.index == 2
        view.finalize_session(show_message=False, build_final_report=False)
        assert (directory / "rr_intervals.csv").exists()
        assert (directory / "meditation_analysis.json").exists()
        assert not view.meditation_panel.recording.active
    finally:
        # Let the view's existing delayed startup callbacks expire while their
        # Python receivers are still alive (including the 3.5s update timer).
        QTest.qWait(3700)
        monkeypatch.setattr(view, "_is_sensor_connected", lambda: False)
        view.close()
        for popup in (view.ecg_window, view.qtc_window, view.poincare_window, view.psd_window):
            popup.deleteLater()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        model._qtc_executor.shutdown(wait=False, cancel_futures=True)
    with (directory / "session.csv").open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["event"] == "IBI"]
    assert [float(row["value"]) for row in rows] == [999.0234375, 1000.9765625]
