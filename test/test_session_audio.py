from types import SimpleNamespace

from PySide6.QtWidgets import QApplication
import pytest

from hnh.session_audio import SessionAudio
from hnh.meditation import RRRecording
from hnh.practice_templates import load_templates
from hnh.session_artifacts import create_session_bundle
from test_meditation import Clock
from test_meditation_ui import Host


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def audio_log_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HNH_DATA_DIR", str(tmp_path))


def prepare(tmp_path, qapp, automatic=True):
    host = Host()
    host.panel.begin(create_session_bundle(tmp_path))
    rec = host.panel.recording
    clock = Clock()
    rec.clock = clock
    rec.origin = clock()
    events = []
    host.panel.audio.play = lambda event, mode: events.append((event, mode))
    rec.begin_protocol(16, automatic, {}, audio_mode="voice")
    return host, rec, clock, events


def test_5_16_5_automatic_audio_exactly_once(tmp_path, qapp):
    host, rec, clock, events = prepare(tmp_path, qapp)
    host.panel.refresh()
    host.panel.refresh()
    clock.value = 1000 + 300
    host.panel.refresh()
    host.panel.refresh()
    clock.value = 1000 + 1260
    host.panel.refresh()
    clock.value = 1000 + 1560
    host.panel.refresh()
    host.panel.refresh()
    assert events == [(event, "voice") for event in ("before", "practice", "after", "completed")]
    assert not rec.active and host.finalizations == 1
    assert [p["end_sec"] - p["start_sec"] for p in rec.metadata["phases"]] == [300, 960, 300]
    host.close()


def test_manual_transitions_and_early_stop_do_not_announce_completion(tmp_path, qapp):
    host, rec, clock, events = prepare(tmp_path, qapp, automatic=False)
    host.panel.refresh()
    clock.value += 20
    host.panel.next_phase()
    host.panel.finish("finalized")
    host.panel.finish("finalized")
    assert events == [(event, "voice") for event in ("before", "practice", "stopped")]
    host.close()


def test_delayed_timer_announces_current_phase_only(tmp_path, qapp):
    host, rec, clock, events = prepare(tmp_path, qapp)
    host.panel.refresh()
    clock.value += 1265
    host.panel.refresh()
    assert [event for event, _ in events] == ["before", "after"]
    host.panel.finish("finalized")
    host.close()


def test_voice_failure_falls_back_to_tone_and_new_cue_cancels_old(monkeypatch, qapp):
    from hnh import session_audio
    launched, stopped = [], []
    class Process:
        def __init__(self, args, **kwargs):
            self.args = args
            self.result = None
            launched.append(self)
        def poll(self):return self.result
        def terminate(self):
            stopped.append(self)
            self.result = -15
        def wait(self, timeout=None):
            return self.result
    monkeypatch.setattr(session_audio.sys, "platform", "darwin")
    monkeypatch.setattr(session_audio.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session_audio.subprocess, "Popen", Process)
    audio = SessionAudio()
    audio.play("practice", "voice")
    assert launched[-1].args[:3] == ["/usr/bin/say", "-v", "Milena"]
    audio.play("after", "voice")
    assert len(stopped) == 1
    launched[-1].result = 1
    audio._poll()
    assert launched[-1].args[0] == "/usr/bin/afplay"
    audio.play("completed", "off")
    assert audio.process is None


def test_pause_freezes_phase_and_resume_cue_is_not_cancelled(tmp_path, qapp):
    host, rec, clock, events = prepare(tmp_path, qapp)
    host.panel.refresh()
    clock.value += 50
    rec.set_paused(True)
    host.panel._announce(rec, "paused")
    host.panel.refresh()
    assert "00:50" in host.panel.phase_label.text()
    assert not host.panel.next_button.isEnabled()
    clock.value += 900
    host.panel.refresh()
    assert "00:50" in host.panel.phase_label.text()
    assert rec.phase["name"] == "before"
    rec.set_paused(False)
    host.panel._announce(rec, "resumed")
    host.panel.refresh()
    host.panel.refresh()
    assert [event for event, _ in events] == ["before", "paused", "resumed"]
    clock.value += 250
    host.panel.refresh()
    assert events[-1] == ("practice", "voice")
    rec.finish()
    host.close()


def test_templates_use_snapshot_and_custom_baseline_lengths(tmp_path):
    templates, errors = load_templates(tmp_path)
    assert not errors
    template = next(item for item in templates if item["id"] == "coherence")
    clock = Clock()
    rec = RRRecording(tmp_path, "test", clock=clock)
    rec.begin_protocol(16, True, {}, before_minutes=3, after_minutes=4, template=template)
    template["practice_minutes"] = 99
    assert rec.metadata["practice_template"]["practice_minutes"] == 16
    assert rec.metadata["protocol"]["before_seconds"] == 180
    rec.finish()


def test_start_new_announces_ordinary_recording(tmp_path, qapp):
    host = Host()
    events = []
    host.panel.audio.play = lambda *args: events.append(args)
    host.panel.begin(create_session_bundle(tmp_path))
    assert events == [("started", "voice")]
    host.panel.finish("finalized")
    assert events[-1] == ("stopped", "voice")
    host.close()


def test_start_new_applies_prepared_template_and_announces_baseline(tmp_path, qapp):
    host = Host()
    events = []
    host.panel.audio.play = lambda *args: events.append(args)
    host.panel.draft = {"technique": "Coherence"}
    host.panel.draft_plan = dict(before=5, practice=16, after=5, automatic=True, audio_mode="voice")
    host.panel.begin(create_session_bundle(tmp_path))
    rec = host.panel.recording
    assert rec.phase["name"] == "before"
    assert rec.phase["start_sec"] < 1
    assert rec.metadata["protocol"]["practice_seconds"] == 960
    assert events == [("before", "voice")]
    assert host.panel.draft_plan is None
    host.panel.finish("finalized")
    host.close()
