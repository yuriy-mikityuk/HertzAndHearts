"""Non-blocking spoken/tone cues through the local OS audio output."""
import math
import json
from datetime import datetime
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from hnh.data_paths import app_data_root

MESSAGES = {
    "started": "Запись началась.",
    "before": "Начался исходный замер. Дыши естественно.",
    "practice": "Исходный замер завершён. Начинай практику.",
    "after": "Практика завершена. Переходи к обычному дыханию.",
    "completed": "Сеанс завершён.",
    "stopped": "Сеанс остановлен раньше времени.",
    "paused": "Сеанс на паузе.",
    "resumed": "Продолжаем текущий этап.",
    "test": "Проверка звука. Так будут звучать уведомления об этапах сеанса.",
}


class SessionAudio(QObject):
    def __init__(self, parent=None, *, status=None):
        super().__init__(parent)
        self.status = status or (lambda message: None)
        self.process = None
        self.fallback = None
        self.cue_event = None
        self.stage = None
        self.started = 0.0
        self.stderr = None
        self.files = tempfile.TemporaryDirectory(prefix="hrv-session-audio-")
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)

    def stop(self):
        self.timer.stop()
        self.fallback = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.2)
            self._log("cancelled")
        self.process = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None

    def _log(self, outcome, **details):
        try:
            path = app_data_root() / "session-audio.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": datetime.now().astimezone().isoformat(),
                    "event": self.cue_event, "stage": self.stage, "outcome": outcome,
                    **details}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Diagnostics must not interrupt a recording.

    def _tone_file(self, event):
        count = {"before": 1, "practice": 1, "after": 2, "completed": 3}.get(event, 2)
        path = Path(self.files.name) / f"tone-{count}.wav"
        if not path.exists():
            rate = 16000
            samples = []
            for _ in range(count):
                for i in range(int(rate * 0.35)):
                    envelope = min(1, i / 320, (rate * 0.35 - i) / 640)
                    samples.append(int(7000 * envelope * math.sin(2 * math.pi * 660 * i / rate)))
                samples.extend([0] * int(rate * 0.18))
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        return path

    def _launch(self, args, stage="playback"):
        self.stage = stage
        if self.stderr is not None:
            self.stderr.close()
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self.stderr)
        self.started = time.monotonic()
        self._log("started", backend=Path(args[0]).name)
        self.timer.start()

    def _tone(self, event):
        player = shutil.which("afplay") if sys.platform == "darwin" else None
        if player:
            self._launch([player, str(self._tone_file(event))])
        else:
            QApplication.beep()
            self.status("Использован системный сигнал. Проверьте его громкость кнопкой проверки звука.")

    def play(self, event, mode="voice"):
        self.stop()  # A newer phase supersedes obsolete or queued speech.
        self.cue_event = event
        if mode == "off":
            self._log("disabled")
            self.status("Уведомления: без звука")
            return
        if event not in MESSAGES:
            raise ValueError("Unknown session audio event")
        try:
            speaker = shutil.which("say") if sys.platform == "darwin" else shutil.which("espeak")
            if mode == "voice" and speaker:
                self.fallback = event
                voice = "Milena" if sys.platform == "darwin" else "ru"
                self._launch([speaker, "-v", voice, MESSAGES[event]])
            else:
                if mode == "voice":
                    self.status("Голосовой движок недоступен — используется звуковой сигнал.")
                self._tone(event)
        except (OSError, ValueError, wave.Error) as error:
            self.fallback = None
            self._log("error", error=str(error))
            self._fallback_tone(event)

    def _fallback_tone(self, event):
        self.status("Голос недоступен — звуковой сигнал. Подробности: session-audio.log")
        try:
            self._tone(event)
        except OSError as error:
            self._log("error", error=str(error))
            QApplication.beep()
            self.status("Ошибка аудио. Проверьте выход и громкость macOS.")

    def _poll(self):
        if self.process is None:
            self.timer.stop()
            return
        result = self.process.poll()
        if result is None:
            if time.monotonic() - self.started <= 20:
                return
            event = self.fallback
            self._log("timeout")
            self.stop()
            if event:
                self._fallback_tone(event)
            else:
                self.status("Аудиоплеер не ответил. Проверьте аудиовыход.")
            return
        self.stderr.seek(0)
        error = self.stderr.read(2000).decode("utf-8", errors="replace")
        self.stderr.close()
        self.stderr = None
        self._log("finished", exit_code=result, error=error)
        event = self.fallback
        self.fallback = None
        self.process = None
        self.timer.stop()
        if result != 0 and event:
            self._fallback_tone(event)
        elif result != 0:
            self.status("Не удалось воспроизвести звуковой сигнал. Проверьте аудиовыход.")
        else:
            self.status("Аудиоплеер завершил уведомление. Не слышно — проверьте аудиовыход.")
