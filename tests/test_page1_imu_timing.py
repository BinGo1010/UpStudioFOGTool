import unittest

from page1 import WtMultiImuUdpRecorder


class WtImuTimingTests(unittest.TestCase):
    def test_imu_sync_timestamp_uses_100hz_theoretical_period(self):
        recorder = WtMultiImuUdpRecorder()

        self.assertEqual(recorder.EXPECTED_SAMPLE_RATE_HZ, 100.0)
        self.assertEqual(recorder.EXPECTED_SAMPLE_PERIOD_MS, 10.0)

        first = recorder._recording_sample_timing(1, device_clock_ms=1000, pc_receive_elapsed_ts=0.0)
        second = recorder._recording_sample_timing(1, device_clock_ms=1010, pc_receive_elapsed_ts=0.01)
        after_gap = recorder._recording_sample_timing(1, device_clock_ms=1040, pc_receive_elapsed_ts=0.04)

        self.assertEqual(first["sample_counter"], 1)
        self.assertAlmostEqual(first["sync_timestamp"], 0.0, places=6)
        self.assertEqual(second["sample_counter"], 2)
        self.assertAlmostEqual(second["sync_timestamp"], 0.01, places=6)
        self.assertEqual(after_gap["sample_counter"], 5)
        self.assertEqual(after_gap["dropped_samples_since_previous"], 2)
        self.assertEqual(after_gap["cumulative_dropped_samples"], 2)
        self.assertAlmostEqual(after_gap["sync_timestamp"], 0.04, places=6)


if __name__ == "__main__":
    unittest.main()
