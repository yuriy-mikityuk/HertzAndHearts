import csv
import hashlib
import json
import math
from pathlib import Path

import pytest

from hnh.meditation import (
    RRRecording, analyze_session, comparison_key, load_history, trend_points,
    window_metrics, write_json,
)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def sample(raw=1000.0, clean=None, correction="unchanged", received_perf=None):
    return {
        "raw_rr_ms": raw,
        "cleaned_rr_ms": raw if clean is None else clean,
        "correction": correction,
        "received_perf": received_perf,
    }


def alternating_rows():
    return [
        {"index": i, "time": i + 0.5, "raw": 900.0 if i % 2 == 0 else 1100.0,
         "clean": 900.0 if i % 2 == 0 else 1100.0,
         "correction": "unchanged", "segment": 0}
        for i in range(300)
    ]


def make_session(directory: Path, *, technique="Breath attention", practice=5):
    directory.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    recording = RRRecording(directory, directory.name, clock=clock)
    recording.begin_protocol(practice, True, {
        "technique": technique, "posture": "seated", "breathing": "natural",
        "sleep_hours": 8, "caffeine": "none",
    })
    # Three full five-minute phases. Deliberately preserve fractional RR.
    duration = 600 + practice * 60
    for i in range(duration):
        clock.value = 1000 + i + 0.5
        raw = 999.0234375 if i % 2 == 0 else 1000.9765625
        recording.add_sample(sample(raw, received_perf=clock.value))
        recording.tick()
    clock.value = 1000 + duration
    assert recording.tick()
    recording.finish()
    return recording


def test_exact_metrics_for_known_alternating_intervals():
    result = window_metrics(alternating_rows(), 0, 300)
    assert result["usable"]
    assert result["rmssd_ms"] == 200
    assert result["ln_rmssd"] == pytest.approx(math.log(200))
    assert result["sdnn_ms"] == pytest.approx(100 * math.sqrt(300 / 299))
    assert result["mean_hr_bpm"] == 60
    assert result["adjacent_pairs"] == 299


def test_pause_preserves_wall_time_raw_data_and_restarts_windows(tmp_path):
    clock = Clock()
    rec = RRRecording(tmp_path, "paused", clock=clock)
    rec.begin_protocol(5, True, {})
    for second in range(960):
        clock.value = 1000 + second
        if second in (150, 210):
            rec.set_paused(second == 150)
        rec.tick()
        clock.value += 0.5
        rec.add_sample(sample(900 if second % 2 else 1100, received_perf=clock()))
    clock.value = 1960
    assert rec.tick()
    result = rec.finish()
    assert rec.metadata["pauses"] == [{"start_sec": 150, "end_sec": 210}]
    assert rec.metadata["active_duration_seconds"] == 900
    assert [p["end_sec"] for p in rec.metadata["phases"]] == [360, 660, 960]
    assert [(w["start_sec"], w["end_sec"]) for w in result["windows"]] == [
        (0, 150), (210, 360), (360, 660), (660, 960)]
    assert result["phases"]["before"]["rmssd_ms"] is None
    assert result["phases"]["practice"]["rmssd_ms"] == 200
    assert not result["comparable"]
    rows = list(csv.DictReader((tmp_path / "rr_intervals.csv").open()))
    assert len(rows) == 960  # Even paused RR remain available for audit.
    assert float(rows[210]["elapsed_sec"]) == 210.5
    assert rows[149]["segment_id"] != rows[210]["segment_id"]


def test_stop_while_paused_without_a_protocol(tmp_path):
    clock = Clock()
    rec = RRRecording(tmp_path, "ordinary", clock=clock)
    clock.value += 12
    rec.set_paused(True)
    clock.value += 120
    assert not rec.tick()
    rec.finish()
    assert rec.metadata["pauses"][-1]["end_sec"] == 132
    assert rec.metadata["active_duration_seconds"] == 12
    assert not rec.active


def test_zero_variability_is_not_negative_infinity():
    rows = alternating_rows()
    for row in rows:
        row["raw"] = row["clean"] = 1000
    result = window_metrics(rows, 0, 300)
    assert result["usable"]
    assert result["rmssd_ms"] == result["sdnn_ms"] == 0
    assert result["ln_rmssd"] is None


def test_exclusion_does_not_join_nonadjacent_beats():
    rows = alternating_rows()
    rows[150].update(raw=3000, clean=1000, correction="out_of_range_replaced")
    result = window_metrics(rows, 0, 300)
    assert result["usable"]
    assert result["excluded_beats"] == 1
    assert result["adjacent_pairs"] == 297
    # All real neighboring differences remain 200, not a spurious zero pair
    # connecting the same parity on either side of the excluded beat.
    assert result["rmssd_ms"] == 200


@pytest.mark.parametrize("kind", ["short", "missing", "corrections", "gap"])
def test_poor_windows_are_explicitly_unusable(kind):
    rows, end = alternating_rows(), 300
    if kind == "short":
        end = 299
    elif kind == "missing":
        rows = rows[:200]
    elif kind == "corrections":
        for row in rows[:20]:
            row["correction"] = "out_of_range_replaced"
    else:
        for row in rows[150:]:
            row["segment"] = 1
    result = window_metrics(rows, 0, end)
    assert not result["usable"]
    assert result["reason"]
    assert result["rmssd_ms"] is None
    assert result["mean_hr_bpm"] is None


def test_boundaries_do_not_include_previous_window_pairs():
    rows = alternating_rows()
    for row in rows:
        row["raw"] = row["clean"] = 1000
    second = [
        dict(row, index=row["index"] + 300, time=row["time"] + 300, raw=900, clean=900)
        for row in rows
    ]
    # Exact 90% coverage in the second window is allowed by the stated policy.
    result = window_metrics(rows + second, 300, 600)
    assert result["beats"] == 300
    assert result["rmssd_ms"] == 0
    assert result["mean_hr_bpm"] == pytest.approx(60000 / 900)


def test_capture_preserves_original_fractional_and_corrected_values(tmp_path):
    clock = Clock()
    recording = RRRecording(tmp_path, "sample", clock=clock)
    clock.value += 0.2
    recording.add_sample(sample(999.0234375, received_perf=clock.value))
    clock.value += 1
    recording.add_sample(sample(4000.25, 999.0234375, "out_of_range_replaced", clock.value))
    recording.finish()
    rows = list(csv.DictReader((tmp_path / "rr_intervals.csv").open()))
    assert float(rows[0]["raw_rr_ms"]) == 999.0234375
    assert float(rows[0]["elapsed_sec"]) == pytest.approx(0.2)
    assert float(rows[1]["raw_rr_ms"]) == 4000.25
    assert float(rows[1]["cleaned_rr_ms"]) == 999.0234375
    assert rows[1]["correction"] == "out_of_range_replaced"
    assert [row["sample_index"] for row in rows] == ["0", "1"]


def test_arrival_gap_and_explicit_disconnect_break_segments(tmp_path):
    clock = Clock()
    recording = RRRecording(tmp_path, "sample", clock=clock)
    recording.add_sample(sample(received_perf=clock.value))
    clock.value += 8
    recording.add_sample(sample(received_perf=clock.value))
    recording.mark_gap()
    recording.mark_gap()
    clock.value += 1
    recording.add_sample(sample(received_perf=clock.value))
    recording.finish()
    rows = list(csv.DictReader((tmp_path / "rr_intervals.csv").open()))
    assert [row["segment_id"] for row in rows] == ["0", "1", "2"]


def test_queued_old_beat_and_post_stop_beat_are_not_recorded(tmp_path):
    clock = Clock()
    recording = RRRecording(tmp_path, "sample", clock=clock)
    recording.add_sample(sample(received_perf=999))
    recording.add_sample(sample(received_perf=1000.1))
    recording.finish()
    original = (tmp_path / "rr_intervals.csv").read_bytes()
    recording.add_sample(sample(received_perf=1001))
    assert (tmp_path / "rr_intervals.csv").read_bytes() == original
    assert len(list(csv.DictReader(original.decode().splitlines()))) == 1


def test_full_protocol_recalculation_is_reproducible_and_lossless(tmp_path):
    recording = make_session(tmp_path / "session")
    raw = (recording.directory / "rr_intervals.csv").read_bytes()
    result = analyze_session(recording.directory)
    assert [window["phase"] for window in result["windows"]] == ["before", "practice", "after"]
    assert all(window["usable"] for window in result["windows"])
    assert result["after_minus_before"]["rmssd_ms"] == 0
    assert result["rr_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["phases"]["before"]["rmssd_ms"] == 1.953125
    assert (recording.directory / "rr_intervals.csv").read_bytes() == raw
    assert len(list(csv.DictReader((recording.directory / "meditation_windows.csv").open()))) == 3


def test_timer_catches_up_using_planned_boundaries(tmp_path):
    clock = Clock()
    recording = RRRecording(tmp_path, "sample", clock=clock)
    clock.value += 30  # Settle first; baseline does not start at connection.
    recording.begin_protocol(15, True, {})
    clock.value = 1000 + 1600
    assert recording.tick()
    assert recording.metadata["phases"] == [
        {"name": "before", "start_sec": 30, "end_sec": 330},
        {"name": "practice", "start_sec": 330, "end_sec": 1230},
        {"name": "after", "start_sec": 1230, "end_sec": 1530},
    ]
    recording.finish()


def test_manual_early_stop_retains_partial_data_without_comparable_result(tmp_path):
    clock = Clock()
    recording = RRRecording(tmp_path, "short", clock=clock)
    recording.begin_protocol(5, False, {})
    for i in range(40):
        clock.value = 1000 + i
        recording.add_sample(sample(received_perf=clock.value))
    clock.value = 1040
    result = recording.finish()
    assert not recording.metadata["protocol_completed"]
    assert result["phases"]["before"]["rmssd_ms"] is None
    assert all(value is None for value in result["after_minus_before"].values())
    assert "shorter" in result["windows"][0]["reason"]


def test_malformed_rr_order_is_rejected_without_rewriting_source(tmp_path):
    recording = make_session(tmp_path / "sample")
    path = recording.directory / "rr_intervals.csv"
    lines = path.read_text().splitlines()
    lines[2], lines[3] = lines[3], lines[2]
    path.write_text("\n".join(lines) + "\n")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="order"):
        analyze_session(recording.directory)
    assert path.read_bytes() == original


def test_history_keeps_legacy_and_current_capture_separate(tmp_path):
    recording = make_session(tmp_path / "new")
    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "session.csv").write_text("event,value,timestamp,elapsed_sec\n")
    rows = [
        {"state": "finalized", "session_dir": str(recording.directory)},
        {"state": "finalized", "session_dir": str(legacy)},
        {"state": "recording", "session_dir": str(recording.directory)},
    ]
    history, old_count, errors = load_history(rows)
    assert len(history) == old_count == 1
    assert errors == 0
    assert not (legacy / "rr_intervals.csv").exists()


def test_trends_never_pool_different_conditions_or_incomplete_diaries(tmp_path):
    first = make_session(tmp_path / "one", technique="breath attention")
    second = make_session(tmp_path / "two", technique="body scan")
    rows = [{"state": "finalized", "session_dir": str(r.directory)} for r in (first, second)]
    records, _, _ = load_history(rows)
    group = records[0]["group"]
    assert group != records[1]["group"]
    assert len(trend_points(records, group, "before.rmssd_ms")) == 1
    assert trend_points(records, None, "before.rmssd_ms") == []
    incomplete = dict(first.metadata, diary={})
    assert comparison_key(incomplete) is None
    paced = dict(first.metadata, diary={
        "technique": "breath attention", "posture": "seated", "breathing": "paced"
    })
    assert comparison_key(paced) is None


def test_diary_edit_updates_provenance_not_rr(tmp_path):
    recording = make_session(tmp_path / "sample")
    before = analyze_session(recording.directory)
    recording.metadata["diary"]["notes"] = "Calm, but sleepy\nsecond line"
    write_json(recording.directory / "meditation.json", recording.metadata)
    after = analyze_session(recording.directory)
    assert before["rr_sha256"] == after["rr_sha256"]
    assert before["metadata_sha256"] != after["metadata_sha256"]
    assert before["windows"] == after["windows"]


def test_stale_analysis_does_not_enter_trends(tmp_path):
    recording = make_session(tmp_path / "sample")
    recording.metadata["diary"]["notes"] = "Changed outside the app"
    write_json(recording.directory / "meditation.json", recording.metadata)
    records, _, _ = load_history([{
        "state": "finalized", "session_dir": str(recording.directory)
    }])
    assert records[0]["stale"]
    assert trend_points(records, records[0]["group"], "before.rmssd_ms") == []


def test_incomplete_or_bad_quality_protocol_does_not_enter_trends(tmp_path):
    recording = make_session(tmp_path / "sample")
    recording.metadata["protocol_completed"] = False
    write_json(recording.directory / "meditation.json", recording.metadata)
    analyze_session(recording.directory)
    records, _, _ = load_history([{
        "state": "finalized", "session_dir": str(recording.directory)
    }])
    assert not records[0]["analysis"]["comparable"]
    assert trend_points(records, records[0]["group"], "before.rmssd_ms") == []


def test_crash_recovery_preserves_original_stream(tmp_path):
    from hnh.profile_store import ProfileStore
    from hnh.session_artifacts import create_session_bundle

    store = ProfileStore(tmp_path)
    bundle = create_session_bundle(tmp_path)
    store.record_session_started("Admin", bundle)
    clock = Clock()
    recording = RRRecording(bundle.session_dir, bundle.session_id, clock=clock)
    recording.begin_protocol(5, True, {"technique": "breath"})
    for i in range(30):
        clock.value += 1
        recording.add_sample(sample(received_perf=clock.value))
    recording.file.close()  # Simulate a process exit before finish().
    raw = (bundle.session_dir / "rr_intervals.csv").read_bytes()
    result = store.purge_recording_sessions()
    assert result["preserved_rr_sessions"] == 1
    assert result["deleted_dirs"] == 0
    assert (bundle.session_dir / "rr_intervals.csv").read_bytes() == raw
    assert store.list_sessions("Admin")[0]["state"] == "abandoned"
    recovered = json.loads((bundle.session_dir / "meditation.json").read_text())
    assert recovered["state"] == "interrupted"
    assert recovered["phases"][0]["end_sec"] == 30


def test_cannot_overwrite_an_existing_original_rr_file(tmp_path):
    recording = RRRecording(tmp_path, "one")
    recording.finish()
    with pytest.raises(FileExistsError):
        RRRecording(tmp_path, "two")
