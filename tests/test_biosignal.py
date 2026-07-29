import csv
import os
import tempfile
import threading
import time
import unittest

import numpy as np
from PyQt6 import QtCore, QtWidgets

from biosignal import BiosignalChannelPlot, EmgEegPacketParser, EmgEegUdpRecorder


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _signed_24_bytes(value: int) -> bytes:
    if value < 0:
        value += 1 << 24
    return int(value).to_bytes(3, "big")


def build_frame(
    parser: EmgEegPacketParser,
    header: bytes,
    serial: str,
    channel_field: int,
    raw_values,
    device_timestamp: int = 123456789,
) -> bytes:
    values = list(raw_values)
    if not 1 <= len(values) <= 10:
        raise ValueError("A 47-byte low-frequency frame can carry 1-10 samples")
    frame = bytearray(47)
    frame[0:2] = header
    frame[2:5] = bytes.fromhex(serial)
    frame[5] = channel_field
    frame[6:8] = (len(values) * 3).to_bytes(2, "big")
    for index, value in enumerate(values):
        offset = 8 + index * 3
        frame[offset:offset + 3] = _signed_24_bytes(value)
    frame[38:46] = int(device_timestamp).to_bytes(8, "big")
    frame[46] = parser.calculate_crc(frame, 46)
    return bytes(frame)


class PacketParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = EmgEegPacketParser()

    def test_emg_signed_24_bit_and_small_value_scaling(self):
        raw_values = [1500, 3000, -1500, -3000, 2000, 2001, -2000, -2001, 0, 4500]
        frame = build_frame(self.parser, b"\xAA\xAA", "000001", 1, raw_values)

        batch = self.parser.parse_frame(frame, source=("10.0.0.1", 40000), receive_time_s=100.0)

        self.assertIsNotNone(batch)
        np.testing.assert_allclose(
            batch.values,
            [1.0, 3.0, -1.0, -3.0, 2.0 / 1.5, 2.001, -2.0 / 1.5, -2.001, 0.0, 4.5],
        )
        self.assertEqual(batch.expected_serial, "000001")
        self.assertEqual(batch.global_channel_index, 0)
        self.assertEqual(batch.modality_channel, 1)
        self.assertEqual(batch.device_timestamp, 123456789)

    def test_serial_and_channel_mapping(self):
        emg_frame = build_frame(self.parser, b"\xAA\xAA", "000002", 4, [3000] * 10)
        eeg_frame = build_frame(self.parser, b"\xAD\xAD", "000003", 2, [3000] * 10)

        emg = self.parser.parse_frame(emg_frame, source=("10.0.0.2", 40000), receive_time_s=100.0)
        eeg = self.parser.parse_frame(eeg_frame, source=("10.0.0.3", 40000), receive_time_s=100.0)

        self.assertEqual((emg.modality, emg.global_channel_index, emg.modality_channel), ("emg", 7, 8))
        self.assertEqual((eeg.modality, eeg.global_channel_index, eeg.modality_channel), ("eeg", 10, 3))

    def test_crc_error_is_rejected(self):
        frame = bytearray(build_frame(self.parser, b"\xAA\xAA", "000001", 1, [3000] * 10))
        frame[12] ^= 0x01
        self.assertIsNone(self.parser.parse_frame(bytes(frame), source=("10.0.0.1", 40000)))

    def test_channel_timestamp_continues_at_nominal_rate(self):
        frame = build_frame(self.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)
        first = self.parser.parse_frame(frame, source=("10.0.0.1", 40000), receive_time_s=100.0)
        second = self.parser.parse_frame(frame, source=("10.0.0.1", 40000), receive_time_s=100.01)

        self.assertAlmostEqual(first.sample_timestamps_us[0], 99_991_000.0)
        self.assertAlmostEqual(first.sample_timestamps_us[-1], 100_000_000.0)
        self.assertAlmostEqual(second.sample_timestamps_us[0], 100_001_000.0)
        self.assertAlmostEqual(second.sample_timestamps_us[-1], 100_010_000.0)

    def test_channel_timestamp_reanchors_after_receive_gap(self):
        frame = build_frame(self.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)
        first = self.parser.parse_frame(frame, source=("10.0.0.1", 40000), receive_time_s=100.0)
        reconnected = self.parser.parse_frame(frame, source=("10.0.0.1", 40000), receive_time_s=101.0)

        self.assertAlmostEqual(first.sample_timestamps_us[-1], 100_000_000.0)
        self.assertAlmostEqual(reconnected.sample_timestamps_us[0], 100_991_000.0)
        self.assertAlmostEqual(reconnected.sample_timestamps_us[-1], 101_000_000.0)
        self.assertGreater(
            reconnected.sample_timestamps_us[0] - first.sample_timestamps_us[-1],
            self.parser.REANCHOR_GAP_S * 1_000_000.0,
        )

    def test_wrong_modality_for_mapped_sensor_is_rejected(self):
        eeg_from_emg_serial = build_frame(self.parser, b"\xAD\xAD", "000001", 0, [3000] * 10)
        self.assertIsNone(
            self.parser.parse_frame(eeg_from_emg_serial, source=("10.0.0.1", 40000), receive_time_s=100.0)
        )

    def test_crc_matches_independent_golden_vectors(self):
        # These payloads and expected CRC bytes are fixed protocol vectors; they
        # intentionally do not use build_frame (which calls calculate_crc).
        broadcast_payload = bytes.fromhex("ffffc0a80a14ffffff0001765c01765c")
        emg_payload = bytes.fromhex(
            "aaaa00000101001e"
            "000bb8000bb8000bb8000bb8000bb8"
            "000bb8000bb8000bb8000bb8000bb8"
            "00000000075bcd15"
        )

        self.assertEqual(len(broadcast_payload), 16)
        self.assertEqual(self.parser.calculate_crc(broadcast_payload), 0xC2)
        self.assertEqual(len(emg_payload), 46)
        self.assertEqual(self.parser.calculate_crc(emg_payload), 0x42)

    def test_unknown_serial_is_rejected(self):
        frame = build_frame(self.parser, b"\xAA\xAA", "000004", 1, [3000] * 10)

        self.assertIsNone(
            self.parser.parse_frame(frame, source=("10.0.0.4", 40000), receive_time_s=100.0)
        )

    def test_different_known_serials_from_same_ip_are_both_parsed(self):
        emg_frame = build_frame(self.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)
        eeg_frame = build_frame(self.parser, b"\xAD\xAD", "000003", 0, [3000] * 10)
        source = ("10.0.0.1", 40000)

        emg = self.parser.parse_frame(emg_frame, source=source, receive_time_s=100.0)
        eeg = self.parser.parse_frame(eeg_frame, source=source, receive_time_s=100.0)

        self.assertIsNotNone(emg)
        self.assertIsNotNone(eeg)
        self.assertEqual((emg.expected_serial, emg.global_channel_index), ("000001", 0))
        self.assertEqual((eeg.expected_serial, eeg.global_channel_index), ("000003", 8))
        self.assertEqual(emg.source, "10.0.0.1")
        self.assertEqual(eeg.source, "10.0.0.1")

    def test_known_serial_remains_parseable_after_source_ip_changes(self):
        frame = build_frame(self.parser, b"\xAA\xAA", "000002", 1, [3000] * 10)

        first = self.parser.parse_frame(frame, source=("10.0.0.2", 40000), receive_time_s=100.0)
        moved = self.parser.parse_frame(frame, source=("10.0.1.22", 40000), receive_time_s=101.0)

        self.assertIsNotNone(first)
        self.assertIsNotNone(moved)
        self.assertEqual((first.expected_serial, first.global_channel_index), ("000002", 4))
        self.assertEqual((moved.expected_serial, moved.global_channel_index), ("000002", 4))
        self.assertEqual(moved.source, "10.0.1.22")


class RecorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def test_mixed_datagram_scan_and_session_csv_write(self):
        recorder = EmgEegUdpRecorder()
        emg_frame = build_frame(recorder.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)
        eeg_frame = build_frame(recorder.parser, b"\xAD\xAD", "000003", 0, [-3000] * 10)

        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.start_recording(temp_dir, session_start_ts=100.0)
            batches = recorder.process_datagram(
                b"noise" + emg_frame + b"\x01\x02" + eeg_frame,
                source=("10.0.0.1", 40000),
                receive_time_s=100.02,
            )
            self.assertTrue(recorder.stop_recording())

            self.assertEqual({batch.modality for batch in batches}, {"emg", "eeg"})
            with open(os.path.join(temp_dir, "emg.csv"), newline="", encoding="utf-8") as file_obj:
                emg_rows = list(csv.DictReader(file_obj))
            with open(os.path.join(temp_dir, "eeg.csv"), newline="", encoding="utf-8") as file_obj:
                eeg_rows = list(csv.DictReader(file_obj))

        self.assertEqual(len(emg_rows), 10)
        self.assertEqual(len(eeg_rows), 10)
        self.assertEqual(
            list(emg_rows[0]),
            ["world_time", "sync_timestamp", "packet_serial_number", "channel", "value_uV"],
        )
        self.assertEqual(list(eeg_rows[0]), ["world_time", "sync_timestamp", "channel", "value_uV"])
        self.assertEqual(emg_rows[0]["packet_serial_number"], "000001")
        self.assertEqual(emg_rows[0]["channel"], "1")
        self.assertEqual(eeg_rows[0]["channel"], "1")
        self.assertAlmostEqual(float(emg_rows[-1]["sync_timestamp"]), 0.02, places=6)

    def test_first_batch_is_cropped_at_session_start_without_negative_sync(self):
        recorder = EmgEegUdpRecorder()
        frame = build_frame(recorder.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)

        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.start_recording(temp_dir, session_start_ts=100.0)
            batches = recorder.process_datagram(
                frame,
                source=("10.0.0.1", 40000),
                receive_time_s=100.005,
            )
            self.assertTrue(recorder.stop_recording())

            with open(os.path.join(temp_dir, "emg.csv"), newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].values), 10)
        self.assertEqual(len(rows), 6)
        sync_timestamps = [float(row["sync_timestamp"]) for row in rows]
        self.assertTrue(all(value >= 0.0 for value in sync_timestamps))
        self.assertAlmostEqual(sync_timestamps[0], 0.0, places=6)
        self.assertAlmostEqual(sync_timestamps[-1], 0.005, places=6)

    def test_stop_drains_inflight_tail_and_rejects_post_stop_writes(self):
        recorder = EmgEegUdpRecorder()
        frame = build_frame(recorder.parser, b"\xAA\xAA", "000001", 1, [3000] * 10)
        inflight_frame_count = 20
        accept_entered = threading.Event()
        release_accept = threading.Event()
        stop_started = threading.Event()
        producer_errors = []
        stop_results = []
        original_accept_batch = recorder._accept_batch

        def paused_first_accept(batch):
            if not accept_entered.is_set():
                accept_entered.set()
                if not release_accept.wait(timeout=5.0):
                    raise TimeoutError("test did not release the in-flight batch")
            original_accept_batch(batch)

        def produce_inflight_datagram():
            try:
                recorder.process_datagram(
                    frame * inflight_frame_count,
                    source=("10.0.0.1", 40000),
                    receive_time_s=100.0,
                )
            except Exception as exc:  # Make worker failures visible to unittest.
                producer_errors.append(exc)

        def request_stop():
            stop_started.set()
            stop_results.append(recorder.request_stop_recording())

        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.start_recording(temp_dir, session_start_ts=99.0)
            recorder._accept_batch = paused_first_accept
            producer = threading.Thread(target=produce_inflight_datagram)
            stopper = threading.Thread(target=request_stop)
            try:
                producer.start()
                self.assertTrue(accept_entered.wait(timeout=5.0))
                stopper.start()
                self.assertTrue(stop_started.wait(timeout=5.0))
                # process_datagram still owns _ingest_lock here. Releasing it
                # lets all accepted frames enter the queue before the stop
                # sentinel can be appended.
                release_accept.set()
                producer.join(timeout=5.0)
                stopper.join(timeout=5.0)
                self.assertFalse(producer.is_alive())
                self.assertFalse(stopper.is_alive())
                self.assertEqual(producer_errors, [])
                self.assertEqual(stop_results, [True])

                # A valid packet parsed after stop must remain display-only and
                # must never be enqueued behind the stop sentinel.
                post_stop_batches = recorder.process_datagram(
                    frame,
                    source=("10.0.0.1", 40000),
                    receive_time_s=101.0,
                )
                self.assertEqual(len(post_stop_batches), 1)
                self.assertTrue(recorder.stop_recording())
            finally:
                release_accept.set()
                producer.join(timeout=5.0)
                stopper.join(timeout=5.0)
                recorder._accept_batch = original_accept_batch
                recorder.stop_recording()

            with open(os.path.join(temp_dir, "emg.csv"), newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertEqual(len(rows), inflight_frame_count * 10)

    def test_broadcast_message_layout_and_crc(self):
        recorder = EmgEegUdpRecorder()
        message = recorder._build_broadcast_message("192.168.10.20", "255.255.255.0", 30300)

        self.assertEqual(len(message), 17)
        self.assertEqual(message[:2], b"\xFF\xFF")
        self.assertEqual(message[2:6], bytes([192, 168, 10, 20]))
        self.assertEqual(message[6:10], bytes([255, 255, 255, 0]))
        self.assertEqual(message[11:13], (30300).to_bytes(2, "big"))
        self.assertEqual(message[14:16], (30300).to_bytes(2, "big"))
        self.assertEqual(message[16], recorder.parser.calculate_crc(message, 16))


class BiosignalPlotTests(unittest.TestCase):
    def test_observed_rate_is_samples_per_second_not_per_refresh_window(self):
        plot = BiosignalChannelPlot("CH1", "#138A36")
        try:
            plot._last_rate_update_monotonic = 100.0
            plot.add_batch(np.ones(2000, dtype=np.float32), rms_uv=1.0, amplitude_uv=1.0)
            original_monotonic = time.monotonic
            try:
                time.monotonic = lambda: 102.0
                plot.refresh(update_rate=True)
            finally:
                time.monotonic = original_monotonic
            self.assertEqual(plot._observed_rate, 1000)
        finally:
            plot.deleteLater()


if __name__ == "__main__":
    unittest.main()
