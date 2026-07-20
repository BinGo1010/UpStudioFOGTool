import csv
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


# QApplication must exist before RecorderTests in test_biosignal creates its
# QCoreApplication.  unittest imports every test module before running suites,
# so keeping this module-level reference also makes full discovery deterministic.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6 import QtCore, QtWidgets

from biosignal import EmgEegPacketParser
from page1 import Page1Widget


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _signed_24_bytes(value: int) -> bytes:
    if value < 0:
        value += 1 << 24
    return int(value).to_bytes(3, "big")


def _build_frame(
    parser: EmgEegPacketParser,
    header: bytes,
    serial: str,
    channel_field: int,
    raw_values=None,
) -> bytes:
    values = list(raw_values if raw_values is not None else [3000] * 10)
    frame = bytearray(47)
    frame[0:2] = header
    frame[2:5] = bytes.fromhex(serial)
    frame[5] = channel_field
    frame[6:8] = (len(values) * 3).to_bytes(2, "big")
    for index, value in enumerate(values):
        offset = 8 + index * 3
        frame[offset:offset + 3] = _signed_24_bytes(value)
    frame[38:46] = (123456789).to_bytes(8, "big")
    frame[46] = parser.calculate_crc(frame, 46)
    return bytes(frame)


def _candidate_consecutive_udp_port() -> int:
    """Return a recently verified free UDP port pair (port and port + 1)."""
    for _attempt in range(100):
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                first.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                second.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            first.bind(("127.0.0.1", 0))
            port = int(first.getsockname()[1])
            if port >= 65535:
                continue
            second.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
        finally:
            first.close()
            second.close()
    raise RuntimeError("could not find two consecutive free UDP ports")


def _all_biosignal_frames(parser: EmgEegPacketParser):
    frames = []
    for serial in ("000001", "000002"):
        for local_channel in range(1, 5):
            frames.append(_build_frame(parser, b"\xAA\xAA", serial, local_channel))
    for zero_based_channel in range(3):
        frames.append(_build_frame(parser, b"\xAD\xAD", "000003", zero_based_channel))
    return frames


def _read_dict_rows(path: str):
    with open(path, newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


class Page1BiosignalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._remote_monitor_patch = patch.object(
            Page1Widget,
            "_start_remote_status_monitor",
            autospec=True,
        )
        self._remote_monitor_patch.start()
        self.addCleanup(self._remote_monitor_patch.stop)

        self._dialog_patchers = [
            patch("page1.QMessageBox.warning", return_value=None),
            patch("page1.QMessageBox.critical", return_value=None),
            patch("page1.QMessageBox.information", return_value=None),
        ]
        self.dialog_mocks = [patcher.start() for patcher in self._dialog_patchers]
        for patcher in self._dialog_patchers:
            self.addCleanup(patcher.stop)

        self.page = Page1Widget()
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self):
        self.sender.close()
        page = self.page
        if page is None:
            return
        page._remote_single_click_timer.stop()
        page._biosignal_refresh_timer.stop()
        page._biosignal_finalize_timer.stop()
        try:
            page.biosignal_recorder.stop_recording(timeout_s=5.0)
            page.biosignal_recorder.stop()
            page.imu_recorder.stop()
            page.realsense.stop_preview()
            page.close()
        finally:
            page.deleteLater()
            APP.processEvents()
            QtCore.QCoreApplication.sendPostedEvents(
                None,
                QtCore.QEvent.Type.DeferredDelete,
            )
            APP.processEvents()
            self.page = None

    def _wait_for(self, predicate, timeout_s=5.0, message="condition was not met"):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        APP.processEvents()
        self.assertTrue(predicate(), message)

    def _start_real_listener(self) -> int:
        recorder = self.page.biosignal_recorder
        for _attempt in range(20):
            port = _candidate_consecutive_udp_port()
            if recorder.start(port):
                self.page.biosignal_port_spin.setValue(port)
                return port
        self.fail("real EMG/EEG UDP recorder could not bind a dynamic port pair")

    def _send_all_channels(self, port: int):
        for frame in _all_biosignal_frames(self.page.biosignal_recorder.parser):
            self.sender.sendto(frame, ("127.0.0.1", port))

    def _device_counts(self):
        recorder = self.page.biosignal_recorder
        with recorder._device_lock:
            return {
                index: int(state.get("count", 0))
                for index, state in recorder._device_state.items()
            }

    def _wait_until_all_channels_online(self):
        recorder = self.page.biosignal_recorder
        self._wait_for(
            lambda: recorder.online_serials() == {"000001", "000002", "000003"}
            and recorder.online_channel_indices() == set(range(11)),
            message="the three fixed serials and eleven channels did not become online",
        )

    def _configure_non_biosignal_hardware_mocks(self):
        page = self.page
        page._start_capture_devices = Mock(name="start_capture_devices")
        page.imu_recorder.online_imu_indices = Mock(return_value=[1, 2, 3, 4, 5])
        page.imu_recorder.start = Mock(name="imu_start")
        page.imu_recorder.stop_recording = Mock(name="imu_stop_recording")
        page._usb_camera_channel_skipped = Mock(return_value=True)
        page._start_camera_recording = Mock(name="camera_start_recording")
        page._stop_camera_recording = Mock(name="camera_stop_recording")
        page.refresh_camera_devices = Mock(name="refresh_camera_devices")
        page.enable_d435i_checkbox.setChecked(False)
        page.remote_connected = True

    def test_page_contains_two_biosignal_child_panels_with_fixed_mapping(self):
        page = self.page
        self.assertIsNotNone(page.emg_panel)
        self.assertIsNotNone(page.eeg_panel)
        self.assertEqual(page.data_tabs.tabText(page.data_tabs.indexOf(page.emg_panel)), "低频肌电 EMG")
        self.assertEqual(page.data_tabs.tabText(page.data_tabs.indexOf(page.eeg_panel)), "脑电 EEG")

        self.assertEqual(page.emg_panel.serials, ("000001", "000002"))
        self.assertEqual(page.eeg_panel.serials, ("000003",))
        self.assertEqual(len(page.emg_panel.channel_plots), 8)
        self.assertEqual(len(page.eeg_panel.channel_plots), 3)

        self.assertEqual(
            [plot.title_label.text() for plot in page.emg_panel.channel_plots],
            [
                "EMG CH1 · 000001/CH1",
                "EMG CH2 · 000001/CH2",
                "EMG CH3 · 000001/CH3",
                "EMG CH4 · 000001/CH4",
                "EMG CH5 · 000002/CH1",
                "EMG CH6 · 000002/CH2",
                "EMG CH7 · 000002/CH3",
                "EMG CH8 · 000002/CH4",
            ],
        )
        self.assertEqual(
            [plot.title_label.text() for plot in page.eeg_panel.channel_plots],
            [
                "EEG CH1 · 000003/CH1",
                "EEG CH2 · 000003/CH2",
                "EEG CH3 · 000003/CH3",
            ],
        )
        panel_labels = {label.text() for label in page.findChildren(QtWidgets.QLabel)}
        self.assertIn("子框 1 · 8 通道低频肌电（1000 Hz）", panel_labels)
        self.assertIn("子框 2 · 3 通道脑电（1000 Hz）", panel_labels)

    def test_real_udp_receiver_marks_three_serials_and_eleven_channels_online(self):
        port = self._start_real_listener()
        self._send_all_channels(port)
        self._wait_until_all_channels_online()

        recorder = self.page.biosignal_recorder
        self.assertEqual(recorder.online_serials(), {"000001", "000002", "000003"})
        self.assertEqual(recorder.online_channel_indices(), set(range(11)))

        # Exercise the queued Qt route from the receiver into both display
        # panels, in addition to checking the receiver's health state.
        self._wait_for(
            lambda: all(np.any(plot.data_buffer) for plot in self.page.emg_panel.channel_plots)
            and all(np.any(plot.data_buffer) for plot in self.page.eeg_panel.channel_plots),
            message="valid UDP batches did not reach all eleven Page1 plots",
        )
        recorder._emit_device_statuses()
        APP.processEvents()
        for serial in ("000001", "000002"):
            self.assertIn("在线", self.page.emg_panel.status_labels[serial].text())
        self.assertIn("在线", self.page.eeg_panel.status_labels["000003"].text())

    def test_unified_collection_records_udp_remote_labels_and_sync_events(self):
        port = self._start_real_listener()
        self._send_all_channels(port)
        self._wait_until_all_channels_online()
        warm_counts = self._device_counts()
        self.assertEqual(warm_counts, {0: 4, 1: 4, 2: 3})

        self._configure_non_biosignal_hardware_mocks()
        with tempfile.TemporaryDirectory() as output_root:
            self.page.base_dir_input.setText(output_root)
            self.page.name_input.setText("page1_biosignal_e2e")
            self.page.start_collection()

            self.assertTrue(self.page.recording)
            self.assertTrue(self.page.biosignal_recorder.recording)
            self.assertTrue(os.path.isdir(self.page.session_dir))
            self.assertFalse(any(mock.called for mock in self.dialog_mocks))

            # Wait beyond the first frame's 9 ms backfill window, guaranteeing
            # ten recorded samples per frame rather than a session-boundary crop.
            time.sleep(0.05)
            self._send_all_channels(port)
            self._wait_for(
                lambda: self._device_counts() == {0: 8, 1: 8, 2: 6},
                message="recording-period packets were not all accepted",
            )

            # A physical single click is committed after the double-click
            # discrimination window.  Explicitly committing the pending click
            # keeps the test fast while using Page1's public remote entrypoint.
            self.page.handle_remote_button_click("Key_VolumeUp", 16777330)
            self.assertIsNotNone(self.page._pending_remote_click)
            self.assertTrue(self.page._commit_pending_remote_single_click())
            self.assertTrue(self.page.remote_fog_active)
            time.sleep(0.01)
            self.page.handle_remote_button_click("Key_VolumeDown", 16777328)
            self.assertTrue(self.page._commit_pending_remote_single_click())
            self.assertFalse(self.page.remote_fog_active)

            session_dir = self.page.session_dir
            self.page.stop_collection()
            self._wait_for(
                lambda: not self.page.biosignal_recorder.writer_pending,
                message="biosignal CSV writer did not finish after unified stop",
            )
            if self.page.biosignal_finalizing:
                self.page._poll_biosignal_finalize()

            self.assertFalse(self.page.recording)
            self.assertFalse(self.page.biosignal_recorder.recording)
            self.assertFalse(self.page.biosignal_recorder.writer_pending)
            self.assertEqual(self.page.biosignal_recorder.last_write_error, "")

            emg_rows = _read_dict_rows(os.path.join(session_dir, "emg.csv"))
            eeg_rows = _read_dict_rows(os.path.join(session_dir, "eeg.csv"))
            remote_events = _read_dict_rows(os.path.join(session_dir, "remote_fog_events.csv"))
            remote_intervals = _read_dict_rows(os.path.join(session_dir, "remote_fog_intervals.csv"))
            sync_rows = _read_dict_rows(os.path.join(session_dir, "session_sync.csv"))

            self.assertEqual(len(emg_rows), 80)
            self.assertEqual(len(eeg_rows), 30)
            self.assertEqual(list(emg_rows[0]), ["world_time", "sync_timestamp", "channel", "value_uV"])
            self.assertEqual(list(eeg_rows[0]), ["world_time", "sync_timestamp", "channel", "value_uV"])
            self.assertEqual(
                {int(row["channel"]) for row in emg_rows},
                set(range(1, 9)),
            )
            self.assertEqual(
                {int(row["channel"]) for row in eeg_rows},
                {1, 2, 3},
            )
            self.assertEqual(
                {channel: sum(int(row["channel"]) == channel for row in emg_rows)
                 for channel in range(1, 9)},
                {channel: 10 for channel in range(1, 9)},
            )
            self.assertEqual(
                [row["event_type"] for row in remote_events],
                ["fog_start", "fog_end"],
            )
            self.assertEqual(len(remote_intervals), 1)
            self.assertEqual(remote_intervals[0]["interval_index"], "1")
            self.assertGreaterEqual(float(remote_intervals[0]["duration_s"]), 0.0)

            sync_by_device = {}
            for row in sync_rows:
                sync_by_device.setdefault(row["device"], set()).add(row["event"])
            for device in ("EMG 000001", "EMG 000002", "EEG 000003"):
                self.assertIn("record_start_completed", sync_by_device.get(device, set()))
                stop_events = sync_by_device.get(device, set())
                self.assertTrue(
                    {"record_stop_completed", "record_stop_incomplete"} & stop_events,
                    f"missing unified stop event for {device}: {stop_events}",
                )
                if "record_stop_incomplete" in stop_events:
                    self.assertIn("record_finalize_completed", stop_events)
            self.assertTrue(
                {"fog_start", "fog_end"}.issubset(sync_by_device.get("bluetooth_remote", set()))
            )


if __name__ == "__main__":
    unittest.main()
