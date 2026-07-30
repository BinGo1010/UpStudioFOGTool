import csv
import os
import tempfile
import unittest

from page1 import WtMultiImuUdpRecorder


class WtImuTimingTests(unittest.TestCase):
    def test_imu_sync_timestamp_uses_200hz_theoretical_period(self):
        recorder = WtMultiImuUdpRecorder()

        self.assertEqual(recorder.EXPECTED_SAMPLE_RATE_HZ, 200.0)
        self.assertEqual(recorder.EXPECTED_SAMPLE_PERIOD_MS, 5.0)

        first = recorder._recording_sample_timing(1, device_clock_ms=1000, pc_receive_elapsed_ts=0.0)
        second = recorder._recording_sample_timing(1, device_clock_ms=1005, pc_receive_elapsed_ts=0.005)
        after_gap = recorder._recording_sample_timing(1, device_clock_ms=1020, pc_receive_elapsed_ts=0.02)

        self.assertEqual(first["sample_counter"], 1)
        self.assertAlmostEqual(first["sync_timestamp"], 0.0, places=6)
        self.assertEqual(second["sample_counter"], 2)
        self.assertAlmostEqual(second["sync_timestamp"], 0.005, places=6)
        self.assertEqual(after_gap["sample_counter"], 5)
        self.assertEqual(after_gap["dropped_samples_since_previous"], 2)
        self.assertEqual(after_gap["cumulative_dropped_samples"], 2)
        self.assertAlmostEqual(after_gap["sync_timestamp"], 0.02, places=6)

    def test_imu_recording_can_be_limited_to_selected_indices(self):
        recorder = WtMultiImuUdpRecorder()

        def fake_rows(_data):
            base = {
                "device_clock_ms": 1000,
                "acc": ["0.000", "0.000", "1.000"],
                "gyr": ["0.000", "0.000", "0.000"],
                "gnt": ["0.000", "0.000", "0.000"],
                "angle": ["0.000", "0.000", "0.000"],
                "acc_csv": ["0", "0", "1"],
                "gyr_csv": ["0", "0", "0"],
                "gnt_csv": ["0", "0", "0"],
                "angle_csv": ["0", "0", "0"],
                "temperature": "25.0",
                "temperature_csv": "25",
                "battery_percent": 100,
                "rssi": "",
            }
            return [
                {"imu_index": 1, "device_id": "WT5500000000", **base},
                {"imu_index": 2, "device_id": "WT5500012214", **base},
            ]

        recorder._parse_wt_frames = fake_rows
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "imu.csv")
            try:
                recorder.start_recording(output_path, session_start_ts=100.0, enabled_imu_indices=[2])
                recorder._process_wt_datagram(1399, ("127.0.0.1", 50000), b"", 100.01, 1)
            finally:
                recorder.stop_recording()
            with open(output_path, newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["imu_index"], "2")


if __name__ == "__main__":
    unittest.main()
