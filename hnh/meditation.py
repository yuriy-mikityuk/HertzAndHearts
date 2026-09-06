"""Lossless RR capture and reproducible, phase-based meditation analysis.

No Qt, sensor access or profile database writes. Analysis can be repeated with
    python -m hnh.meditation /path/to/session
without modifying rr_intervals.csv or the original event CSV.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

WINDOW_SECONDS = 300.0
ANALYSIS_VERSION = "meditation-1"
PHASES = ("before", "practice", "after")
METRICS = ("rmssd_ms", "ln_rmssd", "sdnn_ms", "mean_hr_bpm")
RR_FIELDS = (
    "sample_index", "received_at", "elapsed_sec", "raw_rr_ms",
    "cleaned_rr_ms", "correction", "segment_id",
)


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def number(value) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


class RRRecording:
    """Append received/filtered pairs; phase boundaries use the same clock.

    Times describe receipt, not hardware acquisition. A multi-RR BLE packet can
    deliver several beats at once. We retain that fact instead of inventing
    sensor timestamps. Explicit reconnects and arrival gaps break adjacency.
    """

    def __init__(self, directory: Path, session_id: str, *, clock=time.perf_counter):
        self.directory = Path(directory)
        self.clock = clock
        self.origin = clock()
        self.active = True
        self.index = 0
        self.segment = 0
        self.last_arrival = None
        self._in_gap = False
        self.file = (self.directory / "rr_intervals.csv").open(
            "x", encoding="utf-8", newline=""
        )
        self.writer = csv.DictWriter(self.file, fieldnames=RR_FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.metadata = {
            "schema_version": 1,
            "session_id": session_id,
            "started_at": datetime.now().astimezone().isoformat(),
            "state": "recording",
            "timing": "monotonic receipt time; elapsed_sec is seconds",
            "protocol": None,
            "phases": [],
            "diary": {},
            "protocol_completed": False,
        }
        try:
            self.persist()
        except Exception:
            self.file.close()
            self.active = False
            raise

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.origin)

    @property
    def phase(self) -> dict | None:
        phases = self.metadata["phases"]
        return phases[-1] if phases and phases[-1]["end_sec"] is None else None

    def persist(self):
        write_json(self.directory / "meditation.json", self.metadata)

    def add_sample(self, sample: dict):
        if not self.active:
            return
        arrived = number(sample.get("received_perf"))
        arrived = self.clock() if arrived is None else arrived
        elapsed = arrived - self.origin
        if elapsed < 0:  # A queued beat from before this recording.
            return
        if self.last_arrival is not None:
            # More than 5 seconds between notifications is a timing break,
            # independent of whether the live UI has already noticed it.
            if elapsed - self.last_arrival > 5.0:
                self.mark_gap()
        self.writer.writerow({
            "sample_index": self.index,
            "received_at": datetime.now().astimezone().isoformat(),
            "elapsed_sec": f"{elapsed:.9f}",
            "raw_rr_ms": sample.get("raw_rr_ms"),
            "cleaned_rr_ms": sample.get("cleaned_rr_ms"),
            "correction": sample.get("correction", "invalid"),
            "segment_id": self.segment,
        })
        self.file.flush()
        self.last_arrival = elapsed
        self.index += 1
        self._in_gap = False

    def mark_gap(self):
        if not self._in_gap:
            self.segment += 1
            self._in_gap = True

    def begin_protocol(self, practice_minutes: int, automatic: bool, diary: dict, *,
                       before_minutes=5, after_minutes=5, audio_mode="off", template=None):
        if not self.active or self.metadata["protocol"] is not None:
            raise ValueError("A protocol can only begin once in an active recording.")
        if any(type(value) is not int or not 1 <= value <= 120
               for value in (before_minutes, practice_minutes, after_minutes)):
            raise ValueError("Phase durations must be whole minutes between 1 and 120.")
        if audio_mode not in {"off", "tone", "voice"}:
            raise ValueError("Unknown audio mode.")
        self.metadata["protocol"] = {
            "before_seconds": before_minutes * 60,
            "practice_seconds": practice_minutes * 60,
            "after_seconds": after_minutes * 60,
            "automatic": bool(automatic),
            "audio_mode": audio_mode,
        }
        self.metadata["diary"] = dict(diary)
        if template is not None:
            # Capture the selected version: later template edits cannot rewrite this session.
            self.metadata["practice_template"] = json.loads(json.dumps(template))
        self.metadata["phases"] = [
            {"name": "before", "start_sec": self.elapsed, "end_sec": None}
        ]
        self.persist()

    def advance(self, *, at: float | None = None) -> bool:
        """Close a phase. Return True when the after phase has ended."""
        phase = self.phase
        if not self.active or phase is None:
            return False
        end = self.elapsed if at is None else float(at)
        end = max(phase["start_sec"], min(end, self.elapsed))
        phase["end_sec"] = end
        index = PHASES.index(phase["name"])
        done = index == len(PHASES) - 1
        if done:
            self.metadata["protocol_completed"] = True
        else:
            self.metadata["phases"].append({
                "name": PHASES[index + 1], "start_sec": end, "end_sec": None,
            })
        self.persist()
        return done

    def tick(self) -> bool:
        protocol = self.metadata["protocol"]
        if not self.active or not protocol or not protocol["automatic"]:
            return False
        while self.phase is not None:
            phase = self.phase
            deadline = phase["start_sec"] + protocol[phase["name"] + "_seconds"]
            if self.elapsed < deadline:
                break
            if self.advance(at=deadline):
                return True
        return False

    def finish(self, state="finalized"):
        if not self.active:
            return
        if self.phase is not None:
            self.phase["end_sec"] = self.elapsed
        self.active = False
        self.file.close()
        self.metadata.update({
            "state": state, "ended_at": datetime.now().astimezone().isoformat(),
            "duration_seconds": self.elapsed, "rr_samples": self.index,
        })
        self.persist()
        return analyze_session(self.directory)


def _read_rr(path: Path) -> list[dict]:
    rows = []
    last_index = -1
    last_elapsed = -1.0
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(RR_FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("Missing fields in rr_intervals.csv.")
        for item in reader:
            elapsed = number(item["elapsed_sec"])
            index = int(item["sample_index"])
            if elapsed is None or elapsed < 0 or elapsed < last_elapsed or index <= last_index:
                raise ValueError("RR timing or sample order is invalid.")
            rows.append({
                "index": index, "time": elapsed,
                "raw": number(item["raw_rr_ms"]),
                "clean": number(item["cleaned_rr_ms"]),
                "correction": item["correction"],
                "segment": int(item["segment_id"]),
            })
            last_index, last_elapsed = index, elapsed
    return rows


def window_metrics(rows: list[dict], start: float, end: float) -> dict:
    """Use unmodified plausible RR; do not join pairs across rejected beats.

    This is an NN approximation, not ectopic-beat adjudication. The saved live
    correction flags are deliberately respected; no synthetic replacement is
    silently included in the primary analysis.
    """
    selected = [row for row in rows if start <= row["time"] < end]
    valid = [
        row for row in selected
        if row["correction"] == "unchanged"
        and row["raw"] is not None and 250 <= row["raw"] <= 2500
        and row["clean"] == row["raw"]
    ]
    duration = end - start
    rr_coverage = sum(row["raw"] for row in valid) / 1000 / duration if duration > 0 else 0
    coverage = min(1.0, rr_coverage)
    rejected = len(selected) - len(valid)
    excluded_fraction = rejected / len(selected) if selected else 1.0
    diffs = [
        right["raw"] - left["raw"]
        for left, right in zip(valid, valid[1:])
        if right["index"] == left["index"] + 1
        and right["segment"] == left["segment"]
    ]
    reasons = []
    if duration < WINDOW_SECONDS - 1e-6:
        reasons.append("shorter than 5 minutes")
    if coverage < 0.90:
        reasons.append("less than 90% RR coverage")
    if excluded_fraction > 0.05:
        reasons.append("more than 5% excluded beats")
    times = [start, *(row["time"] for row in selected), end]
    longest_gap = max((b - a for a, b in zip(times, times[1:])), default=duration)
    if len({row["segment"] for row in selected}) > 1 or longest_gap > 5.0:
        reasons.append("connection or arrival gap")
    if rr_coverage > 1.10:
        reasons.append("RR duration exceeds window duration")
    if len(valid) < 2 or not diffs:
        reasons.append("too few adjacent accepted beats")
    result = {
        "start_sec": start, "end_sec": end, "duration_seconds": duration,
        "beats": len(selected), "accepted_beats": len(valid),
        "excluded_beats": rejected, "coverage": coverage,
        "adjacent_pairs": len(diffs), "longest_receipt_gap_sec": longest_gap,
        "usable": not reasons,
        "reason": "; ".join(reasons),
        **dict.fromkeys(METRICS),
    }
    if not reasons:
        rr = [row["raw"] for row in valid]
        rmssd = math.sqrt(statistics.mean(d * d for d in diffs))
        result.update(
            rmssd_ms=rmssd,
            ln_rmssd=math.log(rmssd) if rmssd > 0 else None,
            sdnn_ms=statistics.stdev(rr),
            mean_hr_bpm=60000.0 / statistics.mean(rr),
        )
    return result


def analyze_session(directory: Path) -> dict:
    directory = Path(directory)
    metadata_path = directory / "meditation.json"
    metadata_bytes = metadata_path.read_bytes()
    metadata = json.loads(metadata_bytes)
    if metadata.get("schema_version") != 1:
        raise ValueError("Unsupported meditation recording schema.")
    if metadata.get("state") == "recording":
        raise ValueError("Stop the recording before recalculating its analysis.")
    rr_path = directory / "rr_intervals.csv"
    rows = _read_rr(rr_path)
    windows = []
    summaries = {}
    previous_end = 0.0
    names = [phase["name"] for phase in metadata["phases"]]
    if names != list(PHASES[:len(names)]):
        raise ValueError("Meditation phases are out of order.")
    for phase in metadata["phases"]:
        start, end = number(phase["start_sec"]), number(phase["end_sec"])
        if start is None or end is None or start < previous_end or end < start:
            raise ValueError("Invalid or unfinished meditation phase.")
        previous_end = end
        cursor = start
        phase_windows = []
        while cursor < end - 1e-6:
            stop = min(cursor + WINDOW_SECONDS, end)
            item = window_metrics(rows, cursor, stop)
            item["phase"] = phase["name"]
            phase_windows.append(item)
            windows.append(item)
            cursor = stop
        accepted = [item for item in phase_windows if item["usable"]]
        summaries[phase["name"]] = {
            "duration_seconds": end - start,
            "usable_windows": len(accepted),
            "total_windows": len(phase_windows),
            **{
                metric: statistics.median(values) if values else None
                for metric in METRICS
                for values in [[item[metric] for item in accepted if item[metric] is not None]]
            },
        }
    before, after = summaries.get("before", {}), summaries.get("after", {})
    delta = {
        metric: after[metric] - before[metric]
        if before.get(metric) is not None and after.get(metric) is not None else None
        for metric in METRICS
    }
    result = {
        "analysis_version": ANALYSIS_VERSION,
        "computed_at": datetime.now().astimezone().isoformat(),
        "window_seconds": WINDOW_SECONDS,
        "aggregation": "median of complete usable 5-minute windows in each phase",
        "beat_policy": "unmodified in-range RR only; no pairs across excluded beats or gaps",
        "quality_policy": {
            "minimum_coverage": 0.90, "maximum_excluded_fraction": 0.05,
            "maximum_receipt_gap_seconds": 5.0, "maximum_rr_duration_ratio": 1.10,
            "rr_bounds_ms": [250, 2500],
        },
        "rr_sha256": hashlib.sha256(rr_path.read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "windows": windows, "phases": summaries, "after_minus_before": delta,
        "comparable": (
            metadata.get("state") == "finalized"
            and metadata.get("protocol_completed", False)
            and all(summaries.get(phase, {}).get("usable_windows", 0) > 0 for phase in PHASES)
            and all(item["usable"] for item in windows
                    if item["duration_seconds"] >= WINDOW_SECONDS - 1e-6)
        ),
    }
    write_json(directory / "meditation_analysis.json", result)
    with (directory / "meditation_windows.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["phase", "start_sec", "end_sec", "duration_seconds", "beats",
                  "accepted_beats", "excluded_beats", "coverage", "adjacent_pairs",
                  "longest_receipt_gap_sec",
                  "usable", "reason", *METRICS]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(windows)
    return result


def comparison_key(metadata: dict) -> tuple | None:
    diary = metadata.get("diary") or {}
    protocol = metadata.get("protocol") or {}
    required = [str(diary.get(key) or "").strip().casefold()
                for key in ("technique", "posture", "breathing")]
    if (not all(required) or not protocol or required[1] == "other"
            or required[2] not in {"natural", "paced"}):
        return None
    rate = number(diary.get("breathing_rate"))
    if required[2] == "paced" and (rate is None or rate <= 0):
        return None
    started = datetime.fromisoformat(metadata["started_at"])
    phases = metadata.get("phases") or []
    if phases:
        started += timedelta(seconds=phases[0]["start_sec"])
    period = ("night", "morning", "afternoon", "evening")[started.hour // 6]
    phase_counts = []
    for name in PHASES:
        phase = next((item for item in metadata.get("phases", []) if item["name"] == name), {})
        start, end = number(phase.get("start_sec")), number(phase.get("end_sec"))
        if start is None or end is None or end - start < WINDOW_SECONDS - 1e-6:
            return None
        phase_counts.append(int((end - start + 1e-6) // WINDOW_SECONDS))
    return (*required, rate if required[2] == "paced" else None, period,
            tuple(phase_counts))


def load_history(sessions: list[dict]) -> tuple[list[dict], int, int]:
    """Only rows from the caller's active profile/visibility selection."""
    records, legacy, errors = [], 0, 0
    for session in sessions:
        if session.get("state") not in {"finalized", "abandoned", "interrupted"}:
            continue
        directory = Path(session["session_dir"])
        metadata_path = directory / "meditation.json"
        if not metadata_path.exists():
            legacy += 1
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not metadata.get("protocol"):
                continue
            analysis_path = directory / "meditation_analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            stale = (
                analysis.get("analysis_version") != ANALYSIS_VERSION
                or analysis.get("metadata_sha256") != hashlib.sha256(metadata_path.read_bytes()).hexdigest()
                or analysis.get("rr_sha256") != hashlib.sha256((directory / "rr_intervals.csv").read_bytes()).hexdigest()
            )
            records.append({
                "directory": directory, "metadata": metadata, "analysis": analysis,
                "group": comparison_key(metadata), "stale": stale,
            })
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            errors += 1
    return records, legacy, errors


def metric_value(record: dict, metric: str) -> float | None:
    phase, name = metric.split(".", 1)
    analysis = record["analysis"]
    if phase == "delta":
        return number(analysis.get("after_minus_before", {}).get(name))
    return number(analysis.get("phases", {}).get(phase, {}).get(name))


def trend_points(records: list[dict], group: tuple | None, metric: str, weekly=False):
    """Never aggregate across comparison groups or unidentified conditions."""
    if group is None:
        return []
    values = defaultdict(list)
    anchors = {}
    for record in records:
        if (record["group"] != group or record.get("stale")
                or not record["analysis"].get("comparable", False)):
            continue
        value = metric_value(record, metric)
        if value is None:
            continue
        when = datetime.fromisoformat(record["metadata"]["started_at"])
        if weekly:
            when = (when - timedelta(days=when.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        key = when.date().isoformat() if weekly else when.timestamp()
        anchors.setdefault(key, when.timestamp())
        values[key].append(value)
    return sorted((anchors[key], statistics.median(items)) for key, items in values.items())


def recover_interrupted_recording(directory: Path) -> None:
    """Called only during startup recovery of inactive recordings.

    Original CSV bytes are never rewritten. Last receipt bounds a partial
    phase; a recovered recording is never marked as a completed protocol.
    """
    directory = Path(directory)
    metadata_path = directory / "meditation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("state") != "recording":
        return
    rows = _read_rr(directory / "rr_intervals.csv")
    last_time = rows[-1]["time"] if rows else 0.0
    for phase in metadata.get("phases", []):
        if phase.get("end_sec") is None:
            phase["end_sec"] = max(phase["start_sec"], last_time)
    metadata.update(state="interrupted", protocol_completed=False, duration_seconds=last_time)
    write_json(metadata_path, metadata)
    analyze_session(directory)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Recalculate fixed-window meditation analysis.")
    parser.add_argument("session_directory", type=Path)
    args = parser.parse_args()
    result = analyze_session(args.session_directory)
    print(f"{len(result['windows'])} windows; analysis {result['analysis_version']}")
