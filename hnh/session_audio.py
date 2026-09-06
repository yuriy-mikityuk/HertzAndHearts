"""Non-blocking spoken/tone cues through the local OS audio output."""
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

MESSAGES = {
    "before": "Начался исходный замер. Дыши естественно.",
    "practice": "Исходный замер завершён. Начинай практику.",
    "after": "Практика завершена. Переходи к обычному дыханию.",
    "completed": "Сеанс завершён.",
    "stopped": "Сеанс остановлен раньше времени.",
    "test": "Проверка звука. Так будут звучать уведомления об этапах сеанса.",
}


class SessionAudio(QObject):
    def __init__(self, parent=None, *, status=None):
        super().__init__(parent)
        self.status = status or (lambda message: None)
        self.process = None
        self.fallback = None
        self.files = tempfile.TemporaryDirectory(prefix="hrv-session-audio-")
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)

    def stop(self):
        self.timer.stop()
        self.fallback = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None

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

    def _launch(self, args):
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        if mode == "off":
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
        except OSError as error:
            self.fallback = None
            QApplication.beep()
            self.status(f"Не удалось воспроизвести аудио: {error}. Использован системный сигнал.")

    def _poll(self):
        if self.process is None:
            self.timer.stop()
            return
        result = self.process.poll()
        if result is None:
            return
        event = self.fallback
        self.fallback = None
        self.process = None
        self.timer.stop()
        if result != 0 and event:
            self.status("Голос недоступен — используется звуковой сигнал.")
            try:
                self._tone(event)
            except OSError:
                QApplication.beep()
        elif result != 0:
            self.status("Не удалось воспроизвести звуковой сигнал. Проверьте аудиовыход.")
