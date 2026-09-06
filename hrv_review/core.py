"""Read immutable recordings, review boundaries, and compute with NeuroKit2.

Receipt timestamps locate phases; they are NOT R-peak timestamps. We never
interpolate over rejected beats or treat legacy segment markers as proof of a
Bluetooth disconnect. All quality thresholds below are review heuristics.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import warnings

import neurokit2 as nk
import numpy as np

VERSION = "neurokit-review-1"
DEFAULT_ROOT = Path.home() / ".local/share/hertz-and-hearts/Sessions"
DEFAULT_STORE = Path.home() / ".local/share/hrv-review"
POLICY = {"rr_min_ms": 250, "rr_max_ms": 2500, "receipt_gap_sec": 5,
          "minimum_coverage": 0.90, "maximum_coverage": 1.10,
          "maximum_excluded_fraction": 0.05}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


@dataclass
class Recording:
    directory: Path
    metadata: dict
    rows: list[dict]
    raw_hash: str
    metadata_hash: str
    duration: float

    @property
    def key(self):
        return digest(str(self.directory.resolve()).encode())[:24]

    @property
    def source(self):
        return {"directory": str(self.directory), "rr_sha256": self.raw_hash,
                "metadata_sha256": self.metadata_hash}


def load_recording(directory: Path) -> Recording:
    directory = Path(directory).resolve()
    raw = (directory / "rr_intervals.csv").read_bytes()
    meta = (directory / "meditation.json").read_bytes()
    metadata = json.loads(meta)
    if metadata.get("schema_version") != 1:
        raise ValueError("Неподдерживаемый формат записи")
    if metadata.get("state") == "recording":
        raise ValueError("Запись ещё идёт — сначала завершите её")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    required = {"sample_index", "elapsed_sec", "raw_rr_ms", "cleaned_rr_ms",
                "correction", "segment_id"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("В CSV отсутствуют поля исходных RR")
    rows = []
    last_index, last_time = -1, -1.0
    for item in reader:
        index, elapsed = int(item["sample_index"]), finite(item["elapsed_sec"])
        if elapsed is None or elapsed < 0 or elapsed < last_time or index <= last_index:
            raise ValueError("Нарушен порядок отсчётов или времени в CSV")
        rows.append({"index": index, "time": elapsed, "raw": finite(item["raw_rr_ms"]),
                     "clean": finite(item["cleaned_rr_ms"]), "correction": item["correction"],
                     "segment": int(item["segment_id"])})
        last_index, last_time = index, elapsed
    if not rows:
        raise ValueError("Запись не содержит RR-интервалов")
    duration = finite(metadata.get("duration_seconds"))
    if duration is None or duration < last_time:
        raise ValueError("В записи нет корректной длительности")
    return Recording(directory, metadata, rows, digest(raw), digest(meta), duration)


def draft_for(recording: Recording) -> dict:
    diary = recording.metadata.get("diary") or {}
    phases = copy.deepcopy(recording.metadata.get("phases") or [])
    if not phases:
        phases = [{"name": "session", "start_sec": 0, "end_sec": recording.duration}]
    breathing = diary.get("breathing") or ""
    if breathing in {"other", "paced"}:
        breathing = diary.get("technique") or breathing
    return {"phases": phases, "excluded_indices": [], "window_seconds": 300,
            "reviewed_recorder_marks": False,
            "conditions": {"practice": diary.get("technique") or "",
                           "posture": diary.get("posture") or "",
                           "breathing": breathing},
            "notes": diary.get("notes") or ""}


def validate_draft(recording: Recording, draft: dict) -> None:
    window = finite(draft.get("window_seconds"))
    if window is None or not 30 <= window <= 3600:
        raise ValueError("Окно сравнения должно быть от 30 до 3600 секунд")
    if not draft.get("phases"):
        raise ValueError("Добавьте хотя бы один этап")
    previous_end, names = 0.0, set()
    for phase in draft["phases"]:
        name = str(phase.get("name") or "").strip()
        start, end = finite(phase.get("start_sec")), finite(phase.get("end_sec"))
        if (not name or name in names or start is None or end is None
                or start < previous_end or end <= start or end > recording.duration + 1e-6):
            raise ValueError("Этапы должны иметь разные имена, не пересекаться и лежать внутри записи")
        names.add(name)
        previous_end = end
    known = {row["index"] for row in recording.rows}
    excluded = draft.get("excluded_indices", [])
    if any(type(index) is not int or index not in known for index in excluded):
        raise ValueError("В списке исключений есть неизвестный номер отсчёта")
    if not isinstance(draft.get("reviewed_recorder_marks"), bool):
        raise ValueError("Некорректное подтверждение проверки отметок")


def rejection_reason(row: dict, excluded: set[int]) -> str:
    if row["index"] in excluded:
        return "Ручное исключение"
    if row["raw"] is None or not POLICY["rr_min_ms"] <= row["raw"] <= POLICY["rr_max_ms"]:
        return "Вне диапазона RR"
    if row["correction"] != "unchanged" or row["raw"] != row["clean"]:
        return "Отмечен обработкой регистратора"
    return ""


def nk_time(values: list[float]) -> dict:
    # RRI-only input lets NK construct beat times from intervals. Feeding
    # Bluetooth packet arrival times would create false missing-beat labels.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = nk.hrv_time({"RRI": np.asarray(values)}, show=False).iloc[0]
    return {key: finite(result[key]) for key in ("HRV_MeanNN", "HRV_SDNN", "HRV_RMSSD")}


def analyze_interval(recording: Recording, draft: dict, start: float, end: float) -> dict:
    selected = [row for row in recording.rows if start <= row["time"] < end]
    excluded = set(draft["excluded_indices"])
    accepted, runs, run = [], [], []
    previous = None
    marker_breaks, missing_indices = 0, 0
    for row in selected:
        if previous is not None:
            marker_breaks += row["segment"] != previous["segment"]
            missing_indices += row["index"] != previous["index"] + 1
        adjacent = previous is not None and (
            row["index"] == previous["index"] + 1
            and row["segment"] == previous["segment"]
            and row["time"] - previous["time"] <= POLICY["receipt_gap_sec"])
        reason = rejection_reason(row, excluded)
        if reason or not adjacent:
            if run:
                runs.append(run)
            run = []
        if not reason:
            run.append(row["raw"])
            accepted.append(row["raw"])
        previous = row
    if run:
        runs.append(run)
    times = [start, *(row["time"] for row in selected), end]
    longest_gap = max(b - a for a, b in zip(times, times[1:]))
    duration = end - start
    coverage = sum(accepted) / 1000 / duration
    removed = len(selected) - len(accepted)
    issues = []
    if coverage < POLICY["minimum_coverage"] or coverage > POLICY["maximum_coverage"]:
        issues.append("Покрытие RR вне 90–110%")
    if removed / max(1, len(selected)) > POLICY["maximum_excluded_fraction"]:
        issues.append("Исключено более 5% интервалов")
    if longest_gap > POLICY["receipt_gap_sec"] or missing_indices:
        issues.append("Пропуск поступления данных / номеров отсчётов")
    if marker_breaks:
        issues.append("Отметка регистратора: причина требует просмотра")
    metrics = {"rmssd_ms": None, "ln_rmssd": None, "sdnn_ms": None, "mean_hr_bpm": None}
    pairs, squared_differences = 0, 0.0
    if len(accepted) >= 2:
        whole = nk_time(accepted)
        metrics.update(sdnn_ms=whole["HRV_SDNN"], mean_hr_bpm=60000 / whole["HRV_MeanNN"])
        for values in runs:
            if len(values) >= 2:
                value = nk_time(values)["HRV_RMSSD"]
                if value is not None:
                    pairs += len(values) - 1
                    squared_differences += value ** 2 * (len(values) - 1)
        if pairs:
            metrics["rmssd_ms"] = math.sqrt(squared_differences / pairs)
            metrics["ln_rmssd"] = math.log(metrics["rmssd_ms"]) if metrics["rmssd_ms"] > 0 else None
    if not pairs:
        issues.append("Недостаточно соседних принятых интервалов")
    blocking = [issue for issue in issues if not (
        issue.startswith("Отметка регистратора") and draft["reviewed_recorder_marks"])]
    return {"start_sec": start, "end_sec": end, "duration_sec": duration,
            "beats": len(selected), "accepted_beats": len(accepted), "excluded_beats": removed,
            "adjacent_pairs": pairs, "coverage": coverage, "longest_receipt_gap_sec": longest_gap,
            "recorder_marks": marker_breaks, "issues": issues, "quality_review_ok": not blocking,
            **metrics}


def analyze(recording: Recording, draft: dict) -> dict:
    validate_draft(recording, draft)
    summaries, windows = [], []
    size = float(draft["window_seconds"])
    for phase in draft["phases"]:
        start, end = float(phase["start_sec"]), float(phase["end_sec"])
        summaries.append({"phase": phase["name"], **analyze_interval(recording, draft, start, end)})
        cursor = start
        while cursor < end - 1e-6:
            stop = min(cursor + size, end)
            item = analyze_interval(recording, draft, cursor, stop)
            item.update(phase=phase["name"], full_window=stop - cursor >= size - 1e-6)
            item["comparison_ok"] = item["full_window"] and item["quality_review_ok"]
            windows.append(item)
            cursor = stop
    return {"analysis_version": VERSION, "neurokit_version": nk.__version__,
            "source": recording.source, "policy": POLICY.copy(),
            "phases": summaries, "windows": windows}


def latest_review(recording: Recording, store: Path, *, allow_stale=False) -> dict | None:
    files = sorted((Path(store) / recording.key).glob("*.json"))
    if not files:
        return None
    review = json.loads(files[-1].read_text())
    if review.get("source") != recording.source and not allow_stale:
        raise ValueError("Исходная запись изменилась после сохранения разметки. Старая версия сохранена; требуется новый просмотр.")
    return review


def save_review(recording: Recording, draft: dict, store: Path, parent: str | None) -> Path:
    current = load_recording(recording.directory)
    if current.source != recording.source:
        raise ValueError("Исходная запись изменилась. Перезагрузите анализ.")
    latest = latest_review(recording, store, allow_stale=True)
    if (latest or {}).get("revision") != parent:
        raise ValueError("Разметка уже изменена в другом окне. Перезагрузите анализ.")
    result = analyze(recording, draft)
    revision = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    payload = {"schema_version": 1, "revision": revision, "parent": parent,
               "saved_at": datetime.now().astimezone().isoformat(), "source": recording.source,
               "draft": copy.deepcopy(draft), "analysis": result}
    directory = Path(store) / recording.key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (revision + ".json")
    temporary = directory / (revision + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return path


def comparison_rows(records: list[tuple[Recording, dict, dict]], phase: str) -> list[dict]:
    """One value per session; missing other phases never exclude this phase."""
    result = []
    for recording, draft, analysis in records:
        conditions = draft["conditions"]
        if not all(str(conditions.get(k) or "").strip() for k in ("practice", "posture", "breathing")):
            continue
        windows = [w for w in analysis["windows"] if w["phase"] == phase and w["comparison_ok"]]
        values = [w["rmssd_ms"] for w in windows if w["rmssd_ms"] is not None]
        if not values:
            continue
        started = datetime.fromisoformat(recording.metadata["started_at"])
        group = (recording.metadata.get("profile_id", ""),
                 *(str(conditions[k]).strip().casefold() for k in ("practice", "posture", "breathing")),
                 phase, float(draft["window_seconds"]), started.hour // 6)
        result.append({"session": recording.metadata.get("session_id", recording.directory.name),
                       "started_at": started.isoformat(), "group": group,
                       "rmssd_ms": float(np.median(values)), "windows": len(values)})
    return result
