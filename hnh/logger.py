from datetime import datetime
import csv
import time
from PySide6.QtCore import QObject, Signal, Slot
from hnh.utils import NamedSignal


class Logger(QObject):
    recording_status = Signal(int)
    status_update = Signal(str)

    def __init__(self):
        super().__init__()
        self.file = None
        self.writer = None
        self.current_path: str | None = None
        self._recording_started_perf: float | None = None

    @Slot(str)
    def start_recording(self, file_path: str):
        if self.file:
            self.status_update.emit(f"Already writing to a file at {self.file.name}.")
            return  # only write to one file at a time
        try:
            self.file = open(file_path, "x", encoding="utf-8", newline="")
        except OSError as exc:
            self.file = None
            self.current_path = None
            self.status_update.emit(f"Failed to start recording: {exc}")
            return
        self.current_path = file_path
        self._recording_started_perf = time.perf_counter()
        self.writer = csv.writer(self.file)
        self.writer.writerow(["event", "value", "timestamp", "elapsed_ms"])
        self.file.flush()
        self.recording_status.emit(0)
        self.status_update.emit(f"Started recording to {self.file.name}.")

    @Slot()
    def save_recording(self):
        """Called when:
        1. User saves recording.
        2. User closes app while recording
        """
        if not self.file:
            return
        saved_path = self.current_path or self.file.name
        self.file.close()
        self.recording_status.emit(1)
        self.status_update.emit(f"Saved recording file: {saved_path}")
        self.file = None
        self.writer = None
        self.current_path = None
        self._recording_started_perf = None

    def _elapsed_ms(self) -> float:
        started = self._recording_started_perf
        if started is None:
            return 0.0
        return max(0.0, (time.perf_counter() - started) * 1000.0)

    @Slot(object)
    def write_rr_sample(self, sample: dict):
        # Unlike a mutable model deque, this payload cannot advance to the next
        # beat while waiting in the logger thread's event queue.
        value = sample.get("cleaned_rr_ms")
        if value is not None:
            self.write_to_file(NamedSignal("IBI", value))

    @Slot(object)
    def write_to_file(self, data: NamedSignal):
        if not self.file:
            return
        key, val = data
        timestamp = datetime.now().isoformat()
        elapsed_ms = self._elapsed_ms()

        if key == "ibis":
            try:
                _, buffer = val
                if not buffer:
                    return
                value = buffer[-1]  # IBI in ms
            except (TypeError, ValueError, IndexError):
                return
            self.writer.writerow(["IBI", value, timestamp, f"{elapsed_ms:.3f}"])
        else:
            try:
                if isinstance(val, list):
                    value = val[-1]
                elif isinstance(val, tuple):
                    value = val[-1][-1]
                else:
                    value = val
            except (IndexError, TypeError):
                return
            self.writer.writerow([key, value, timestamp, f"{elapsed_ms:.3f}"])
        self.file.flush()
