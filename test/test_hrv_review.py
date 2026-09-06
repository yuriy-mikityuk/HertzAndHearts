import copy
import csv
import json
import math
from pathlib import Path

import pytest

from hrv_review.core import (
    analyze, analyze_interval, comparison_rows, draft_for, latest_review,
    load_recording, save_review, validate_draft,
)


def recording(tmp_path, values=None, times=None, segments=None, indices=None):
    path = tmp_path / "sample"
    path.mkdir(parents=True, exist_ok=True)
    values = values if values is not None else [990, 1010] * 420
    times = times if times is not None else [i + 0.5 for i in range(len(values))]
    segments = segments if segments is not None else [0] * len(values)
    indices = indices if indices is not None else list(range(len(values)))
    duration = times[-1] + 0.5
    with (path / "rr_intervals.csv").open("w") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_index", "elapsed_sec", "raw_rr_ms", "cleaned_rr_ms", "correction", "segment_id"])
        writer.writerows([i, t, v, v, "unchanged", s] for i, t, v, s in zip(indices, times, values, segments))
    meta = {"schema_version": 1, "state": "finalized", "session_id": "sample", "profile_id": "Admin",
            "started_at": "2026-09-06T12:00:00+05:00", "duration_seconds": duration,
            "phases": [{"name": "session", "start_sec": 0, "end_sec": duration}],
            "diary": {"technique": "Coherence", "posture": "seated", "breathing": "other"}}
    (path / "meditation.json").write_text(json.dumps(meta))
    return load_recording(path)


def test_short_after_is_visible_and_does_not_hide_other_phases(tmp_path):
    rec = recording(tmp_path)
    draft = draft_for(rec)
    draft["phases"] = [{"name": "before", "start_sec": 0, "end_sec": 300},
                       {"name": "practice", "start_sec": 300, "end_sec": 600},
                       {"name": "after", "start_sec": 600, "end_sec": 840}]
    result = analyze(rec, draft)
    assert result["phases"][-1]["rmssd_ms"] == pytest.approx(20)
    assert result["windows"][-1]["duration_sec"] == 240
    assert not result["windows"][-1]["comparison_ok"]
    assert len(comparison_rows([(rec, draft, result)], "before")) == 1
    assert not comparison_rows([(rec, draft, result)], "after")
    draft["window_seconds"] = 240
    result = analyze(rec, draft)
    assert len(comparison_rows([(rec, draft, result)], "after")) == 1


@pytest.mark.parametrize("kind", ["manual", "recorder_correction", "range"])
def test_excluded_beat_never_joins_former_neighbors(tmp_path, kind):
    rec = recording(tmp_path, [800, 1000, 1200])
    draft = draft_for(rec)
    if kind == "manual":
        draft["excluded_indices"] = [1]
    elif kind == "recorder_correction":
        rec.rows[1]["correction"] = "out_of_range_replaced"
    else:
        rec.rows[1]["raw"] = 6000
    result = analyze_interval(rec, draft, 0, rec.duration)
    assert result["accepted_beats"] == 2
    assert result["adjacent_pairs"] == 0
    assert result["rmssd_ms"] is None
    assert result["sdnn_ms"] == pytest.approx(math.sqrt(80000))


@pytest.mark.parametrize("kind", ["segment", "gap", "missing_index"])
def test_no_pair_crosses_a_continuity_boundary(tmp_path, kind):
    args = {"segments": [0, 0, 1, 1]} if kind == "segment" else (
        {"times": [0.5, 1.5, 20.5, 21.5]} if kind == "gap" else {"indices": [0, 1, 3, 4]})
    rec = recording(tmp_path, [800, 900, 1200, 1300], **args)
    draft = draft_for(rec)
    result = analyze_interval(rec, draft, 0, rec.duration)
    assert result["adjacent_pairs"] == 2
    assert result["rmssd_ms"] == pytest.approx(100)
    if kind == "segment":
        assert result["recorder_marks"] == 1
        assert not any("Пропуск" in reason for reason in result["issues"])
        draft["reviewed_recorder_marks"] = True
        assert analyze_interval(rec, draft, 0, rec.duration)["quality_review_ok"]
    else:
        draft["reviewed_recorder_marks"] = True
        assert not analyze_interval(rec, draft, 0, rec.duration)["quality_review_ok"]


def test_recorder_packet_batch_times_are_allowed(tmp_path):
    rec = recording(tmp_path, [1000] * 4, times=[1, 1, 3, 3])
    result = analyze_interval(rec, draft_for(rec), 0, rec.duration)
    assert result["adjacent_pairs"] == 3
    assert result["rmssd_ms"] == 0
    assert result["ln_rmssd"] is None


def test_revisions_preserve_sources_and_detect_stale_edits(tmp_path):
    rec = recording(tmp_path)
    original = {p.name: p.read_bytes() for p in rec.directory.iterdir()}
    store = tmp_path / "reviews"
    draft = draft_for(rec)
    first = save_review(rec, draft, store, None)
    assert {p.name: p.read_bytes() for p in rec.directory.iterdir()} == original
    with pytest.raises(ValueError, match="другом окне"):
        save_review(rec, draft, store, None)
    draft["notes"] = "Review changed"
    second = save_review(rec, draft, store, first.stem)
    assert first.exists() and second.exists()
    assert latest_review(rec, store)["draft"]["notes"] == "Review changed"
    meta = rec.metadata.copy()
    meta["diary"] = {"technique": "updated"}
    (rec.directory / "meditation.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="Исходная запись изменилась"):
        save_review(rec, draft, store, second.stem)
    refreshed = load_recording(rec.directory)
    with pytest.raises(ValueError, match="Исходная запись изменилась"):
        latest_review(refreshed, store)
    third = save_review(refreshed, draft_for(refreshed), store, second.stem)
    assert third.exists() and latest_review(refreshed, store)["source"] == refreshed.source


def test_comparison_keeps_profiles_breathing_and_window_lengths_separate(tmp_path):
    rec = recording(tmp_path)
    draft = draft_for(rec)
    first = comparison_rows([(rec, draft, analyze(rec, draft))], "session")[0]
    for key in ("posture", "breathing", "practice"):
        changed = copy.deepcopy(draft)
        changed["conditions"][key] += " different"
        other = comparison_rows([(rec, changed, analyze(rec, changed))], "session")[0]
        assert first["group"] != other["group"]
    changed = copy.deepcopy(draft)
    changed["window_seconds"] = 240
    assert first["group"] != comparison_rows([(rec, changed, analyze(rec, changed))], "session")[0]["group"]
    rec.metadata["profile_id"] = "Other user"
    assert first["group"] != comparison_rows([(rec, draft, analyze(rec, draft))], "session")[0]["group"]


@pytest.mark.parametrize("phases", [
    [{"name": "a", "start_sec": 0, "end_sec": 400}, {"name": "b", "start_sec": 300, "end_sec": 500}],
    [{"name": "a", "start_sec": 0, "end_sec": 900}],
    [{"name": "a", "start_sec": float("nan"), "end_sec": 300}],
])
def test_bad_phase_boundaries_are_rejected(tmp_path, phases):
    rec = recording(tmp_path)
    draft = draft_for(rec)
    draft["phases"] = phases
    with pytest.raises(ValueError):
        validate_draft(rec, draft)


def test_active_recording_is_not_imported(tmp_path):
    rec = recording(tmp_path)
    rec.metadata["state"] = "recording"
    (rec.directory / "meditation.json").write_text(json.dumps(rec.metadata))
    with pytest.raises(ValueError, match="ещё идёт"):
        load_recording(rec.directory)
