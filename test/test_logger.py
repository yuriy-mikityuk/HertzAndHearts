from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hnh.logger import Logger
from hnh.utils import NamedSignal


class LoggerTests(unittest.TestCase):
    def test_pause_skips_live_metrics_but_stop_still_saves(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.csv"
            logger = Logger()
            logger.start_recording(str(path))
            logger.write_rr_sample({"cleaned_rr_ms": 900})
            logger.set_paused(True)
            logger.write_rr_sample({"cleaned_rr_ms": 1500})
            logger.set_paused(False)
            logger.write_rr_sample({"cleaned_rr_ms": 1000})
            logger.set_paused(True)
            logger.save_recording()
            with path.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["value"] for r in rows if r["event"] == "IBI"], ["900", "1000"])
            self.assertEqual([r["event"] for r in rows],
                             ["IBI", "SessionPause", "SessionResume", "IBI", "SessionPause"])
            self.assertIsNone(logger.file)

    def test_logger_writes_elapsed_for_all_events(self):
        with TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "session.csv"
            logger = Logger()

            with patch("hnh.logger.time.perf_counter", side_effect=[100.0, 100.5, 101.0]):
                logger.start_recording(str(out_path))
                logger.write_to_file(NamedSignal("ibis", ([], [800.0])))
                logger.write_to_file(NamedSignal("hrv", ([], [42.0])))
                logger.save_recording()

            with open(out_path, encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["event"], "IBI")
            self.assertEqual(rows[1]["event"], "hrv")
            self.assertEqual(float(rows[0]["elapsed_ms"]), 500.0)
            self.assertEqual(float(rows[1]["elapsed_ms"]), 1000.0)

    def test_annotations_with_commas_quotes_and_newlines_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.csv"
            logger = Logger()
            logger.start_recording(str(path))
            text = 'Relaxed, "eyes closed"\nsecond line'
            logger.write_to_file(NamedSignal("Annotation", text))
            logger.save_recording()
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["value"], text)

    def test_existing_recording_is_never_appended_or_overwritten(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.csv"
            path.write_text("original")
            logger = Logger()
            logger.start_recording(str(path))
            self.assertIsNone(logger.file)
            self.assertEqual(path.read_text(), "original")

    def test_per_beat_payload_preserves_batched_rr_values(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.csv"
            logger = Logger()
            logger.start_recording(str(path))
            logger.write_rr_sample({"cleaned_rr_ms": 999.0234375})
            logger.write_rr_sample({"cleaned_rr_ms": 1000.9765625})
            logger.write_rr_sample({"cleaned_rr_ms": None})
            logger.save_recording()
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([float(row["value"]) for row in rows], [999.0234375, 1000.9765625])


if __name__ == "__main__":
    unittest.main()
