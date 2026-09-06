from __future__ import annotations

import unittest

from hnh.model import Model


class ModelIbiDiagnosticsTests(unittest.TestCase):
    def test_original_rr_survives_live_outlier_correction(self):
        model = Model()
        self.addCleanup(model._qtc_executor.shutdown, wait=False, cancel_futures=True)
        model._settings.IBI_MEDIAN_WINDOW = 3
        samples = []
        model.rr_sample.connect(samples.append)
        for value in (999.0234375, 999.0234375, 999.0234375, 4000.25):
            model.update_ibis_buffer(value)
        self.assertEqual(samples[0]["raw_rr_ms"], 999.0234375)
        self.assertEqual(samples[-1]["raw_rr_ms"], 4000.25)
        self.assertEqual(samples[-1]["cleaned_rr_ms"], 1000)
        self.assertEqual(samples[-1]["correction"], "out_of_range_replaced")
        self.assertEqual(model.ibis_buffer[-1], 1000)

    def test_invalid_rr_is_retained_as_flagged_sample_but_not_plotted(self):
        model = Model()
        self.addCleanup(model._qtc_executor.shutdown, wait=False, cancel_futures=True)
        samples = []
        model.rr_sample.connect(samples.append)
        model.update_ibis_buffer(0)
        self.assertEqual(samples[0]["raw_rr_ms"], 0)
        self.assertEqual(samples[0]["correction"], "invalid")
        self.assertEqual(len(model.ibis_buffer), 0)

    def test_beat_and_buffer_update_counts_track_one_to_one(self):
        model = Model()
        self.addCleanup(model._qtc_executor.shutdown, wait=False, cancel_futures=True)
        model.reset_ibi_diagnostics()
        rr_values = [900, 880, 910, 895, 905]
        for rr in rr_values:
            model.hr_handler(rr)
            model.update_ibis_buffer(rr)
        snap = model.ibi_diagnostics_snapshot()
        self.assertEqual(snap["beats_received"], len(rr_values))
        self.assertEqual(snap["buffer_updates"], len(rr_values))
        self.assertEqual(snap["delta"], 0)


if __name__ == "__main__":
    unittest.main()
