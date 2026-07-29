import csv
import json
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtMultimedia import (
    QCamera,
    QMediaCaptureSession,
    QMediaDevices,
    QMediaFormat,
    QMediaRecorder,
)
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from biosignal import BiosignalPanel, EmgEegUdpRecorder


class OptimizedChannelPlot(QWidget):
    def __init__(self, channel_num, parent=None):
        super().__init__(parent)
        self.channel_num = channel_num
        self.fs = 1000
        self.time_window = 5.0
        self.buffer_size = int(self.fs * self.time_window)
        self.data_buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self.time_buffer = np.zeros(self.buffer_size, dtype=np.float64)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_label = QLabel(f"CH {channel_num}")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 10px; color: #555; margin-left: 5px;")
        layout.addWidget(self.title_label)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        self.plot_widget.setDownsampling(mode="peak")
        self.plot_widget.setClipToView(True)
        self.plot_widget.setXRange(0, self.time_window, padding=0)
        self.curve = self.plot_widget.plot(pen=pg.mkPen(color="#007ACC", width=1))
        layout.addWidget(self.plot_widget)

        self.render_timer = QtCore.QTimer(self)
        self.render_timer.timeout.connect(self.refresh_plot)
        self.render_timer.start(50)

    def add_batch(self, timestamps, values):
        n = len(values)
        if n == 0:
            return
        if n >= self.buffer_size:
            self.data_buffer[:] = values[-self.buffer_size:]
            self.time_buffer[:] = timestamps[-self.buffer_size:]
            return
        self.data_buffer[:-n] = self.data_buffer[n:]
        self.data_buffer[-n:] = values
        self.time_buffer[:-n] = self.time_buffer[n:]
        self.time_buffer[-n:] = timestamps

    def refresh_plot(self):
        if not self.isVisible() or self.time_buffer[-1] <= 0:
            return
        latest_ts = self.time_buffer[-1]
        display_times = self.time_buffer - (latest_ts - self.time_window)
        self.curve.setData(display_times, self.data_buffer)

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import pyrealsense2 as rs
except ImportError:
    rs = None


class RefreshCameraComboBox(QComboBox):
    about_to_show = pyqtSignal()

    def showPopup(self):
        self.about_to_show.emit()
        super().showPopup()


class WtMultiImuUdpRecorder(QtCore.QObject):
    status_changed = pyqtSignal(int, str, int)
    sample_received = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    command_status = pyqtSignal(str)

    DEVICE_IDS = {
        1: "WT5500000000",
        2: "WT5500012214",
        3: "WT5500012221",
        4: "WT5500012369",
        5: "WT5500012368",
    }
    WIFI_SSID = "FOG"
    WIFI_PASSWORD = "66666666"
    WT_FRAME_LEN = 54
    UI_EMIT_INTERVAL_S = 0.05
    EXPECTED_SAMPLE_RATE_HZ = 100.0
    EXPECTED_SAMPLE_PERIOD_MS = 1000.0 / EXPECTED_SAMPLE_RATE_HZ
    DEVICE_CLOCK_ROLLOVER_MS = 24 * 60 * 60 * 1000
    DEVICE_CLOCK_GLITCH_TOLERANCE_MS = 100.0
    UDP_PACKET_QUEUE_SIZE = 50000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self._threads: List[threading.Thread] = []
        self._processor_thread: Optional[threading.Thread] = None
        self._sockets: List[socket.socket] = []
        self._packet_queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=self.UDP_PACKET_QUEUE_SIZE)
        self._writer_lock = threading.Lock()
        self._timing_lock = threading.Lock()
        self._csv_file = None
        self._csv_writer = None
        self._recording_start_ts: Optional[float] = None
        self._write_queue: "queue.Queue[Optional[list]]" = queue.Queue(maxsize=100000)
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_running = False
        self._dropped_rows = 0
        self._counts: Dict[int, int] = {}
        self._device_to_index = {device_id: index for index, device_id in self.DEVICE_IDS.items()}
        self._port_sockets: Dict[int, socket.socket] = {}
        self._device_endpoints: Dict[int, tuple] = {}
        self._raw_packet_counts: Dict[int, int] = {}
        self._dropped_udp_packets = 0
        self._recording_sample_counts: Dict[int, int] = {}
        self._recording_clock_states: Dict[int, dict] = {}
        self._unknown_device_ids: set = set()
        self._unparsed_packet_counts: Dict[int, int] = {}
        self._command_lock = threading.Lock()
        self._last_ui_emit_ts: Dict[int, float] = {}
        self._latest_sample_lock = threading.Lock()
        self._latest_samples: Dict[int, dict] = {}

    def start(self, ports: List[int], output_path: Optional[str] = None, session_start_ts: Optional[float] = None):
        if output_path:
            self.start_recording(output_path, session_start_ts=session_start_ts)
        if self.running:
            return

        self._counts = {i: 0 for i in self.DEVICE_IDS}
        self._device_endpoints.clear()
        self._raw_packet_counts = {}
        self._dropped_udp_packets = 0
        self._unknown_device_ids = set()
        self._unparsed_packet_counts = {}
        self._last_ui_emit_ts = {}
        self._packet_queue = queue.Queue(maxsize=self.UDP_PACKET_QUEUE_SIZE)
        with self._latest_sample_lock:
            self._latest_samples = {}
        self.running = True

        unique_ports = []
        for port in ports:
            if port not in unique_ports:
                unique_ports.append(port)

        for imu_index in self.DEVICE_IDS:
            self.status_changed.emit(imu_index, "waiting", 0)

        self._processor_thread = threading.Thread(
            target=self._process_packet_loop,
            daemon=True,
            name="Page14-WT-UDP-Processor",
        )
        self._processor_thread.start()

        for port in unique_ports:
            thread = threading.Thread(
                target=self._receive_loop,
                args=(port,),
                daemon=True,
                name=f"Page14-WT-UDP-{port}",
            )
            thread.start()
            self._threads.append(thread)

    def start_recording(self, output_path: str, session_start_ts: Optional[float] = None):
        with self._writer_lock:
            if self._csv_file:
                self._csv_file.flush()
                self._csv_file.close()
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            self._csv_file = open(output_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._recording_start_ts = session_start_ts if session_start_ts is not None else time.time()
            self._write_queue = queue.Queue(maxsize=100000)
            self._writer_running = True
            self._dropped_rows = 0
            with self._timing_lock:
                self._recording_sample_counts = {i: 0 for i in self.DEVICE_IDS}
                self._recording_clock_states = {}
            self._csv_writer.writerow([
                "world_time",
                "sync_timestamp",
                "imu_index",
                "sample_counter",
                "dropped_samples_since_previous",
                "cumulative_dropped_samples",
                "acc_x",
                "acc_y",
                "acc_z",
                "gyro_x",
                "gyro_y",
                "gyro_z",
                "mag_x",
                "mag_y",
                "mag_z",
                "angle_x",
                "angle_y",
                "angle_z",
                "temperature",
                "battery_percent",
            ])
            self._writer_thread = threading.Thread(
                target=self._write_loop,
                daemon=True,
                name="Page14-WT-CSV-Writer",
            )
            self._writer_thread.start()

    def stop_recording(self):
        self.request_stop_recording()
        writer_thread = None
        with self._writer_lock:
            writer_thread = self._writer_thread
        if writer_thread and writer_thread.is_alive():
            writer_thread.join(timeout=5.0)
        with self._writer_lock:
            if self._csv_file:
                self._csv_file.flush()
                self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
            self._writer_thread = None
            self._recording_start_ts = None

    def request_stop_recording(self) -> bool:
        write_queue = None
        with self._writer_lock:
            writer_thread = self._writer_thread
            write_queue = self._write_queue
            if self._csv_file is None and writer_thread is None and self._recording_start_ts is None:
                return False
            self._writer_running = False
            self._recording_start_ts = None
        if writer_thread is not None and write_queue is not None:
            try:
                write_queue.put(None, timeout=1.0)
            except queue.Full:
                pass
        return True

    def _write_loop(self):
        pending = 0
        last_flush = time.time()
        while True:
            try:
                row = self._write_queue.get(timeout=0.2)
            except queue.Empty:
                row = None
                should_stop = not self._writer_running
            else:
                should_stop = row is None

            with self._writer_lock:
                writer = self._csv_writer
                csv_file = self._csv_file
                if row is not None and writer is not None:
                    writer.writerow(row)
                    pending += 1
                now = time.time()
                if csv_file is not None and pending and (pending >= 200 or now - last_flush >= 1.0 or should_stop):
                    csv_file.flush()
                    pending = 0
                    last_flush = now

            if should_stop and self._write_queue.empty():
                break

    def stop(self):
        if not self.running:
            return

        self.running = False
        for sock in list(self._sockets):
            try:
                sock.close()
            except OSError:
                pass

        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=1.0)
        processor_thread = self._processor_thread
        if processor_thread is not None:
            try:
                self._packet_queue.put(None, timeout=0.5)
            except queue.Full:
                pass
            if processor_thread.is_alive():
                processor_thread.join(timeout=2.0)

        self._threads.clear()
        self._processor_thread = None
        self._sockets.clear()
        self._port_sockets.clear()
        self._device_endpoints.clear()

        self.stop_recording()

    def _receive_loop(self, port: int):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sockets.append(sock)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(0.1)
            self._port_sockets[port] = sock
            for imu_index in self.DEVICE_IDS:
                self.status_changed.emit(imu_index, f"listening:{port}", self._counts.get(imu_index, 0))
        except OSError as exc:
            self.error_occurred.emit(f"WT IMU UDP port {port} start failed: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            try:
                self._sockets.remove(sock)
            except ValueError:
                pass
            if not self._port_sockets:
                self.running = False
            for imu_index in self.DEVICE_IDS:
                self.status_changed.emit(imu_index, "stopped", self._counts.get(imu_index, 0))
            return

        while self.running:
            try:
                data, addr = sock.recvfrom(8192)
                recv_ts = time.time()
                raw_count = self._raw_packet_counts.get(port, 0) + 1
                self._raw_packet_counts[port] = raw_count
                try:
                    self._packet_queue.put_nowait((port, addr, data, recv_ts, raw_count))
                except queue.Full:
                    self._dropped_udp_packets += 1
                    if (
                        self._dropped_udp_packets in (1, 10, 100, 1000)
                        or self._dropped_udp_packets % 10000 == 0
                    ):
                        self.error_occurred.emit(
                            f"WT IMU UDP processing queue full, dropped packets={self._dropped_udp_packets}"
                        )
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                if self.running:
                    self.error_occurred.emit(f"WT IMU UDP receive failed on port {port}: {exc}")

        for imu_index in self.DEVICE_IDS:
            self.status_changed.emit(imu_index, "stopped", self._counts.get(imu_index, 0))
        self._port_sockets.pop(port, None)

    def _process_packet_loop(self):
        while self.running or not self._packet_queue.empty():
            try:
                item = self._packet_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            port, addr, data, recv_ts, raw_count = item
            try:
                self._process_wt_datagram(port, addr, data, recv_ts, raw_count)
            except Exception as exc:
                if self.running:
                    self.error_occurred.emit(f"WT IMU packet processing failed on port {port}: {exc}")

    def _process_wt_datagram(self, port: int, addr, data: bytes, recv_ts: float, raw_count: int):
        rows = self._parse_wt_frames(data)
        if not rows:
            self._log_unparsed_udp_packet(port, addr, data, raw_count)
            return

        with self._writer_lock:
            start_ts = self._recording_start_ts
            write_queue = self._write_queue
            writer_active = self._csv_writer is not None and start_ts is not None
        pc_receive_elapsed_ts = recv_ts - start_ts if writer_active else 0.0

        csv_rows = []
        if writer_active:
            with self._timing_lock:
                for row in rows:
                    imu_index = row["imu_index"]
                    timing = self._recording_sample_timing(
                        imu_index,
                        row["device_clock_ms"],
                        pc_receive_elapsed_ts,
                    )
                    csv_rows.append([
                        self._format_world_time(start_ts + timing["sync_timestamp"]),
                        f"{timing['sync_timestamp']:.6f}",
                        imu_index,
                        timing["sample_counter"],
                        timing["dropped_samples_since_previous"],
                        timing["cumulative_dropped_samples"],
                        *row["acc_csv"],
                        *row["gyr_csv"],
                        *row["gnt_csv"],
                        *row["angle_csv"],
                        row["temperature_csv"],
                        row["battery_percent"],
                    ])

        queue_warnings = []
        if csv_rows:
            with self._writer_lock:
                still_active = (
                    self._csv_writer is not None
                    and self._recording_start_ts == start_ts
                    and self._write_queue is write_queue
                )
            if still_active:
                for csv_row in csv_rows:
                    try:
                        write_queue.put_nowait(csv_row)
                    except queue.Full:
                        self._dropped_rows += 1
                        if self._dropped_rows in (1, 100, 1000) or self._dropped_rows % 10000 == 0:
                            queue_warnings.append(f"WT IMU CSV queue full, dropped rows={self._dropped_rows}")
        for warning in queue_warnings:
            self.error_occurred.emit(warning)

        for row in rows:
            imu_index = row["imu_index"]
            self._device_endpoints[imu_index] = (addr, port)
            self._counts[imu_index] = self._counts.get(imu_index, 0) + 1
            packet_count = self._counts[imu_index]
            sample = {
                "imu_index": imu_index,
                "device_id": row["device_id"],
                "port": port,
                "count": packet_count,
                "timestamp": recv_ts,
                "acc": row["acc"],
                "gyr": row["gyr"],
                "gnt": row["gnt"],
                "angle": row["angle"],
                "acc_csv": row["acc_csv"],
                "gyr_csv": row["gyr_csv"],
                "gnt_csv": row["gnt_csv"],
                "angle_csv": row["angle_csv"],
                "temperature": row["temperature"],
                "temperature_csv": row["temperature_csv"],
                "battery_percent": row["battery_percent"],
                "rssi": row["rssi"],
            }
            with self._latest_sample_lock:
                self._latest_samples[imu_index] = dict(sample)
            last_ui_ts = self._last_ui_emit_ts.get(imu_index, 0.0)
            if packet_count == 1 or recv_ts - last_ui_ts >= self.UI_EMIT_INTERVAL_S:
                self._last_ui_emit_ts[imu_index] = recv_ts
                self.status_changed.emit(imu_index, "receiving", packet_count)
                self.sample_received.emit(sample)

    def calibrate_accelerometer(self, imu_index: int):
        sequence = [
            (bytes.fromhex("FF AA 69 88 B5"), 0.2, "unlock"),
            (bytes.fromhex("FF AA 01 01 00"), 5.0, "enter accelerometer calibration"),
            (bytes.fromhex("FF AA 01 00 00"), 0.2, "exit calibration"),
            (bytes.fromhex("FF AA 00 00 00"), 0.0, "save calibration"),
        ]
        self._send_command_sequence_to_indices([imu_index], sequence, "校准")

    def calibrate_connected_accelerometers(self):
        sequence = [
            (bytes.fromhex("FF AA 69 88 B5"), 0.2, "unlock"),
            (bytes.fromhex("FF AA 01 01 00"), 5.0, "enter accelerometer calibration"),
            (bytes.fromhex("FF AA 01 00 00"), 0.2, "exit calibration"),
            (bytes.fromhex("FF AA 00 00 00"), 0.0, "save calibration"),
        ]
        self._send_command_sequence_to_indices(self.online_imu_indices(), sequence, "校准")

    def zero_angle(self, imu_index: int):
        sequence = [
            (bytes.fromhex("FF AA 01 08 00"), 0.5, "set angle reference 1"),
            (bytes.fromhex("FF AA 01 08 00"), 0.5, "set angle reference 2"),
            (bytes.fromhex("FF AA 01 04 00"), 0.0, "zero heading"),
        ]
        self._send_command_sequence_to_indices([imu_index], sequence, "角度置零")

    def zero_connected_angles(self):
        sequence = [
            (bytes.fromhex("FF AA 01 08 00"), 0.5, "set angle reference 1"),
            (bytes.fromhex("FF AA 01 08 00"), 0.5, "set angle reference 2"),
            (bytes.fromhex("FF AA 01 04 00"), 0.0, "zero heading"),
        ]
        self._send_command_sequence_to_indices(self.online_imu_indices(), sequence, "角度置零")

    def online_imu_indices(self) -> List[int]:
        return sorted(self._device_endpoints.keys())

    def latest_samples(self) -> Dict[int, dict]:
        with self._latest_sample_lock:
            return {index: dict(sample) for index, sample in self._latest_samples.items()}

    def _send_command_sequence(self, imu_index: int, sequence: List[tuple], label: str):
        self._send_command_sequence_to_indices([imu_index], sequence, label)

    def _send_command_sequence_to_indices(self, imu_indices: List[int], sequence: List[tuple], label: str):
        targets = []
        for imu_index in imu_indices:
            endpoint = self._device_endpoints.get(imu_index)
            if endpoint is None:
                continue
            addr, listen_port = endpoint
            sock = self._port_sockets.get(listen_port)
            if sock is not None:
                targets.append((imu_index, addr, sock))

        if not targets:
            self.error_occurred.emit("No online IMU; cannot send command.")
            return

        thread = threading.Thread(
            target=self._send_command_sequence_worker,
            args=(targets, sequence, label),
            daemon=True,
            name="Page14-WT-Cmd-Online",
        )
        thread.start()

    def _send_command_sequence_worker(self, targets: List[tuple], sequence: List[tuple], label: str):
        with self._command_lock:
            try:
                for imu_index, _addr, _sock in targets:
                    self.command_status.emit(f"IMU{imu_index} {label} sending")

                for payload, delay, _step in sequence:
                    for _imu_index, addr, sock in targets:
                        sock.sendto(payload, addr)
                    if delay > 0:
                        time.sleep(delay)

                for imu_index, _addr, _sock in targets:
                    self.command_status.emit(f"IMU{imu_index} {label} done")
            except Exception as exc:
                self.error_occurred.emit(f"{label} command failed: {exc}")

    def _parse_wt_frames(self, data: bytes) -> List[dict]:
        rows = []
        cursor = 0
        while cursor <= len(data) - self.WT_FRAME_LEN:
            start = data.find(b"WT", cursor)
            if start < 0 or start > len(data) - self.WT_FRAME_LEN:
                break
            row = self._parse_wt_frame(data[start:start + self.WT_FRAME_LEN])
            if row is not None:
                rows.append(row)
            cursor = start + self.WT_FRAME_LEN
        return rows

    def _log_unparsed_udp_packet(self, port: int, addr, data: bytes, raw_count: int):
        count = self._unparsed_packet_counts.get(port, 0) + 1
        self._unparsed_packet_counts[port] = count
        if count not in (1, 10, 100) and count % 1000 != 0:
            return

        reason = "no WT frame marker"
        start = data.find(b"WT")
        if 0 <= start <= len(data) - 12:
            try:
                device_id = data[start:start + 12].decode("ascii", errors="replace")
            except Exception:
                device_id = "<decode failed>"
            if device_id not in self._device_to_index:
                self._unknown_device_ids.add(device_id)
                reason = (
                    f"unknown device id {device_id}; expected "
                    + ", ".join(self.DEVICE_IDS.values())
                )

        preview = data[:16].hex(" ").upper()
        self.error_occurred.emit(
            f"WT IMU UDP packet received but not parsed on port {port} "
            f"from {addr[0]}:{addr[1]} ({reason}, bytes={len(data)}, first16={preview}, raw={raw_count})"
        )

    def raw_packet_total(self) -> int:
        return sum(self._raw_packet_counts.values())

    def unparsed_packet_total(self) -> int:
        return sum(self._unparsed_packet_counts.values())

    def _recording_sample_timing(
        self,
        imu_index: int,
        device_clock_ms: int,
        pc_receive_elapsed_ts: float,
    ) -> dict:
        period_ms = self.EXPECTED_SAMPLE_PERIOD_MS
        state = self._recording_clock_states.get(imu_index)
        if state is None:
            state = {
                "base_device_clock_ms": float(device_clock_ms),
                "last_raw_device_clock_ms": float(device_clock_ms),
                "clock_rollover_offset_ms": 0.0,
                "last_unwrapped_device_clock_ms": float(device_clock_ms),
                "base_sync_timestamp": float(pc_receive_elapsed_ts),
                "received_sample_counter": 0,
                "last_sample_counter": 0,
                "sample_counter_offset": 0,
                "last_sync_timestamp": float(pc_receive_elapsed_ts),
                "last_pc_receive_elapsed_ts": float(pc_receive_elapsed_ts),
                "cumulative_dropped_samples": 0,
                "clock_glitch_count": 0,
            }
            self._recording_clock_states[imu_index] = state
            self._recording_sample_counts[imu_index] = 1
            state["received_sample_counter"] = 1
            state["last_sample_counter"] = 1
            state["last_sync_timestamp"] = float(pc_receive_elapsed_ts)
            return {
                "sync_timestamp": float(pc_receive_elapsed_ts),
                "device_elapsed_ms": 0.0,
                "sample_counter": 1,
                "received_sample_counter": 1,
                "dropped_samples_since_previous": 0,
                "cumulative_dropped_samples": 0,
            }

        raw_clock_ms = float(device_clock_ms)
        last_raw_ms = float(state["last_raw_device_clock_ms"])
        rollover_offset_ms = float(state["clock_rollover_offset_ms"])
        raw_delta_ms = raw_clock_ms - last_raw_ms
        half_rollover = self.DEVICE_CLOCK_ROLLOVER_MS / 2
        if raw_delta_ms < -half_rollover:
            rollover_offset_ms += self.DEVICE_CLOCK_ROLLOVER_MS
        elif raw_delta_ms > half_rollover:
            rollover_offset_ms -= self.DEVICE_CLOCK_ROLLOVER_MS

        unwrapped_clock_ms = raw_clock_ms + rollover_offset_ms
        last_unwrapped_ms = float(state["last_unwrapped_device_clock_ms"])
        device_gap_ms = unwrapped_clock_ms - last_unwrapped_ms
        last_pc_elapsed_ts = float(state.get("last_pc_receive_elapsed_ts", pc_receive_elapsed_ts))
        pc_gap_ms = max(0.0, (float(pc_receive_elapsed_ts) - last_pc_elapsed_ts) * 1000.0)

        clock_glitch = False
        if device_gap_ms < -period_ms:
            clock_glitch = True
            gap_ms = pc_gap_ms
        elif device_gap_ms - pc_gap_ms > self.DEVICE_CLOCK_GLITCH_TOLERANCE_MS:
            # A single IMU can occasionally report a future device clock while
            # packets are still arriving continuously. Treat that as a clock
            # glitch rather than a huge packet-loss burst.
            clock_glitch = True
            gap_ms = pc_gap_ms
        else:
            gap_ms = max(0.0, device_gap_ms)

        theoretical_elapsed_steps = max(1, int(math.floor(gap_ms / period_ms + 0.5)))
        last_sample_counter = int(state["last_sample_counter"])
        sample_counter = last_sample_counter + theoretical_elapsed_steps
        received_sample_counter = int(state["received_sample_counter"]) + 1
        dropped_since_previous = max(0, theoretical_elapsed_steps - 1)
        cumulative_dropped = int(state["cumulative_dropped_samples"]) + dropped_since_previous
        sync_timestamp = float(state["last_sync_timestamp"]) + theoretical_elapsed_steps * period_ms / 1000.0
        device_elapsed_ms = unwrapped_clock_ms - float(state["base_device_clock_ms"])

        state["last_raw_device_clock_ms"] = raw_clock_ms
        state["clock_rollover_offset_ms"] = rollover_offset_ms
        state["last_unwrapped_device_clock_ms"] = unwrapped_clock_ms
        state["last_pc_receive_elapsed_ts"] = float(pc_receive_elapsed_ts)
        state["received_sample_counter"] = received_sample_counter
        state["last_sample_counter"] = sample_counter
        state["last_sync_timestamp"] = sync_timestamp
        state["cumulative_dropped_samples"] = cumulative_dropped
        if clock_glitch:
            state["clock_glitch_count"] = int(state.get("clock_glitch_count", 0)) + 1
            state["sample_counter_offset"] = sample_counter - 1
            state["base_device_clock_ms"] = unwrapped_clock_ms
            state["base_sync_timestamp"] = sync_timestamp
        self._recording_sample_counts[imu_index] = received_sample_counter

        return {
            "sync_timestamp": sync_timestamp,
            "device_elapsed_ms": device_elapsed_ms,
            "sample_counter": sample_counter,
            "received_sample_counter": received_sample_counter,
            "dropped_samples_since_previous": dropped_since_previous,
            "cumulative_dropped_samples": cumulative_dropped,
        }

    @staticmethod
    def _format_world_time(timestamp_s: float) -> str:
        try:
            dt = datetime.fromtimestamp(float(timestamp_s))
        except (OSError, OverflowError, TypeError, ValueError):
            return ""
        return dt.strftime("%H:%M:%S:") + f"{dt.microsecond // 1000:03d}"

    def _parse_wt_frame(self, frame: bytes) -> Optional[dict]:
        try:
            device_id = frame[:12].decode("ascii")
        except UnicodeDecodeError:
            return None
        imu_index = self._device_to_index.get(device_id)
        if imu_index is None:
            return None

        device_ms = frame[19] << 8 | frame[18]
        device_time = (
            f"20{frame[12]:02d}-{frame[13]:02d}-{frame[14]:02d} "
            f"{frame[15]:02d}:{frame[16]:02d}:{frame[17]:02d}.{device_ms:03d}"
        )
        device_timestamp_ms = (frame[15] * 3600 + frame[16] * 60 + frame[17]) * 1000 + device_ms
        device_clock_ms = self._device_clock_ms_from_frame(frame, device_timestamp_ms)
        acc = [
            self._i16(frame[21] << 8 | frame[20]) / 32768 * 16,
            self._i16(frame[23] << 8 | frame[22]) / 32768 * 16,
            self._i16(frame[25] << 8 | frame[24]) / 32768 * 16,
        ]
        gyr = [
            self._i16(frame[27] << 8 | frame[26]) / 32768 * 2000,
            self._i16(frame[29] << 8 | frame[28]) / 32768 * 2000,
            self._i16(frame[31] << 8 | frame[30]) / 32768 * 2000,
        ]
        gnt = [
            self._i16(frame[33] << 8 | frame[32]) * 100 / 1024,
            self._i16(frame[35] << 8 | frame[34]) * 100 / 1024,
            self._i16(frame[37] << 8 | frame[36]) * 100 / 1024,
        ]
        angle = [
            self._i16(frame[39] << 8 | frame[38]) / 32768 * 180,
            self._i16(frame[41] << 8 | frame[40]) / 32768 * 180,
            self._i16(frame[43] << 8 | frame[42]) / 32768 * 180,
        ]
        voltage = frame[47] << 8 | frame[46]
        temperature = self._i16(frame[45] << 8 | frame[44]) / 100
        return {
            "imu_index": imu_index,
            "device_id": device_id,
            "device_time": device_time,
            "device_timestamp_ms": device_timestamp_ms,
            "device_clock_ms": device_clock_ms,
            "acc": [f"{value:.2f}" for value in acc],
            "gyr": [f"{value:.2f}" for value in gyr],
            "gnt": [f"{value:.2f}" for value in gnt],
            "angle": [f"{value:.2f}" for value in angle],
            "temperature": f"{temperature:.2f}",
            "acc_csv": [f"{value:.3f}" for value in acc],
            "gyr_csv": [f"{value:.3f}" for value in gyr],
            "gnt_csv": [f"{value:.3f}" for value in gnt],
            "angle_csv": [f"{value:.3f}" for value in angle],
            "temperature_csv": f"{temperature:.3f}",
            "battery_percent": self._battery_percent(voltage),
            "rssi": self._i16(frame[49] << 8 | frame[48]),
            "version": self._i16(frame[51] << 8 | frame[50]),
        }

    @staticmethod
    def _device_clock_ms_from_frame(frame: bytes, fallback_day_ms: int) -> int:
        try:
            year = 2000 + int(frame[12])
            month = int(frame[13])
            day = int(frame[14])
            hour = int(frame[15])
            minute = int(frame[16])
            second = int(frame[17])
            millisecond = int(frame[19] << 8 | frame[18])
            dt = datetime(year, month, day, hour, minute, second)
            origin = datetime(2000, 1, 1)
            return int((dt - origin).total_seconds() * 1000) + millisecond
        except (ValueError, OverflowError):
            return int(fallback_day_ms)

    @staticmethod
    def _i16(value: int) -> int:
        return value - 65536 if value >= 32768 else value

    @staticmethod
    def _battery_percent(voltage: int) -> str:
        if voltage > 396:
            return "100"
        if voltage > 393:
            return "90"
        if voltage > 387:
            return "75"
        if voltage > 382:
            return "60"
        if voltage > 379:
            return "50"
        if voltage > 377:
            return "40"
        if voltage > 373:
            return "30"
        if voltage > 370:
            return "20"
        if voltage > 368:
            return "15"
        if voltage > 350:
            return "10"
        if voltage > 340:
            return "5"
        return "0"


class RealSenseD435iWorker(QtCore.QObject):
    rgb_frame = pyqtSignal(object)
    stereo_frame = pyqtSignal(object)
    status_changed = pyqtSignal(str, str)
    error_occurred = pyqtSignal(str)
    PREVIEW_EMIT_INTERVAL_S = 1.0 / 15.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.recording = False
        self.thread: Optional[threading.Thread] = None
        self.pipeline = None
        self.profile = None
        self.output_dir = ""
        self.depth_dir = ""
        self.rgb_writer = None
        self.stereo_writer = None
        self.frames_file = None
        self.frames_writer = None
        self.imu_file = None
        self.imu_writer = None
        self.frame_index = 0
        self._recording_start_ts: Optional[float] = None
        self._write_queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=120)
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_running = False
        self._dropped_video_frames = 0
        self._dropped_motion_rows = 0
        self.lock = threading.Lock()
        self.metadata: Dict[str, object] = {}
        self._last_waiting_log = 0.0
        self._last_preview_emit_ts = 0.0
        self.motion_streams_enabled = False
        self.motion_profile_fps: Optional[tuple] = None
        self.save_depth_raw = True

    def start_preview(self):
        if self.running:
            return
        if rs is None:
            self.status_changed.emit("disconnected", "pyrealsense2 not installed")
            return
        if not self._has_realsense_device():
            self.status_changed.emit("disconnected", "D435i disconnected")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="Page14-D435i")
        self.thread.start()

    def _has_realsense_device(self) -> bool:
        try:
            context = rs.context()
            for device in context.query_devices():
                try:
                    name = device.get_info(rs.camera_info.name).lower()
                except Exception:
                    name = ""
                if any(key in name for key in ("realsense", "d435", "depth camera")):
                    return True
        except Exception as exc:
            self.status_changed.emit("disconnected", f"D435i check failed: {exc}")
            return False
        return False

    def stop_preview(self):
        self.running = False
        self.stop_recording()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None

    def start_recording(
        self,
        output_dir: str,
        subject: str = "",
        imu_ports: Optional[List[int]] = None,
        session_start_ts: Optional[float] = None,
        save_depth_raw: bool = True,
    ):
        if cv2 is None:
            message = "opencv-python not installed; RealSense video cannot be saved"
            self.error_occurred.emit(message)
            raise RuntimeError(message)
        with self.lock:
            self.output_dir = output_dir
            self.depth_dir = os.path.join(output_dir, "depth_raw_npy")
            os.makedirs(output_dir, exist_ok=True)
            self.save_depth_raw = bool(save_depth_raw)
            if self.save_depth_raw:
                os.makedirs(self.depth_dir, exist_ok=True)
            self.frame_index = 0
            self._recording_start_ts = session_start_ts if session_start_ts is not None else time.time()
            self._write_queue = queue.Queue(maxsize=900)
            self._writer_running = True
            self._dropped_video_frames = 0
            self._dropped_motion_rows = 0
            self.rgb_writer = cv2.VideoWriter(
                os.path.join(output_dir, "RGB.mp4"),
                cv2.VideoWriter_fourcc(*"mp4v"),
                30.0,
                (self.metadata.get("rgb", {}).get("width", 1280), self.metadata.get("rgb", {}).get("height", 720)),
            )
            self.stereo_writer = cv2.VideoWriter(
                os.path.join(output_dir, "Stereo.mp4"),
                cv2.VideoWriter_fourcc(*"mp4v"),
                30.0,
                (1280, 480),
            )
            unopened_writers = [
                name
                for name, writer in (("RGB.mp4", self.rgb_writer), ("Stereo.mp4", self.stereo_writer))
                if writer is None or not writer.isOpened()
            ]
            if unopened_writers:
                for writer_name in ("rgb_writer", "stereo_writer"):
                    writer = getattr(self, writer_name)
                    if writer is not None:
                        writer.release()
                        setattr(self, writer_name, None)
                self._writer_running = False
                self._recording_start_ts = None
                raise RuntimeError("D435i 视频文件无法创建：" + "、".join(unopened_writers))
            self.frames_file = open(os.path.join(output_dir, "frames.csv"), "w", newline="", encoding="utf-8")
            self.frames_writer = csv.writer(self.frames_file)
            self.frames_writer.writerow([
                "frame_index",
                "world_time",
                "sync_timestamp",
                "rgb_frame_number",
                "depth_frame_number",
                "ir_left_frame_number",
                "ir_right_frame_number",
                "depth_npy",
            ])
            if self.motion_streams_enabled:
                self.imu_file = open(os.path.join(output_dir, "imu.csv"), "w", newline="", encoding="utf-8")
                self.imu_writer = csv.writer(self.imu_file)
                self.imu_writer.writerow([
                    "world_time",
                    "sync_timestamp",
                    "source",
                    "frame_number",
                    "sensor_timestamp_ms",
                    "x",
                    "y",
                    "z",
                ])
            else:
                self.imu_file = None
                self.imu_writer = None
            self._write_metadata(output_dir, subject, imu_ports or [], self._recording_start_ts)
            self._writer_thread = threading.Thread(
                target=self._recording_write_loop,
                daemon=True,
                name="Page14-D435i-Disk-Writer",
            )
            self._writer_thread.start()
            self.recording = True

    def stop_recording(self):
        self.request_stop_recording()
        writer_thread = None
        with self.lock:
            writer_thread = self._writer_thread
        if writer_thread and writer_thread.is_alive():
            writer_thread.join()
        with self.lock:
            for writer_name in ("rgb_writer", "stereo_writer"):
                writer = getattr(self, writer_name)
                if writer is not None:
                    writer.release()
                    setattr(self, writer_name, None)
            if self.frames_file is not None:
                self.frames_file.flush()
                self.frames_file.close()
                self.frames_file = None
                self.frames_writer = None
            if self.imu_file is not None:
                self.imu_file.flush()
                self.imu_file.close()
                self.imu_file = None
                self.imu_writer = None
            self._recording_start_ts = None
            self._writer_thread = None

    def request_stop_recording(self) -> bool:
        write_queue = None
        with self.lock:
            writer_thread = self._writer_thread
            write_queue = self._write_queue
            if not self.recording and writer_thread is None and self._recording_start_ts is None:
                return False
            self.recording = False
            self._writer_running = False
        if writer_thread is not None and write_queue is not None:
            try:
                write_queue.put(None, timeout=2.0)
            except queue.Full:
                pass
        return True

    def _recording_write_loop(self):
        pending = 0
        last_flush = time.time()
        while True:
            try:
                item = self._write_queue.get(timeout=0.2)
            except queue.Empty:
                item = None
                should_stop = not self._writer_running
            else:
                should_stop = item is None

            if item is not None:
                kind = item[0]
                if kind == "video" and self.rgb_writer is not None and self.stereo_writer is not None:
                    (
                        _kind,
                        frame_index,
                        rel_ts,
                        color,
                        stereo_bgr,
                        depth,
                        depth_rel,
                        depth_abs,
                        save_depth_raw,
                        color_number,
                        depth_number,
                        ir_left_number,
                        ir_right_number,
                    ) = item
                    if save_depth_raw and depth_abs:
                        np.save(depth_abs, depth)
                    self.rgb_writer.write(color)
                    self.stereo_writer.write(stereo_bgr)
                    if self.frames_writer is not None:
                        self.frames_writer.writerow([
                            frame_index,
                            WtMultiImuUdpRecorder._format_world_time(self._recording_start_ts + rel_ts),
                            f"{rel_ts:.6f}",
                            color_number,
                            depth_number,
                            ir_left_number,
                            ir_right_number,
                            depth_rel,
                        ])
                    pending += 1
                elif kind == "motion" and self.imu_writer is not None:
                    for rel_ts, source, frame_number, sensor_ts, x, y, z in item[1]:
                        self.imu_writer.writerow([
                            WtMultiImuUdpRecorder._format_world_time(self._recording_start_ts + rel_ts),
                            f"{rel_ts:.6f}",
                            source,
                            frame_number,
                            f"{sensor_ts:.3f}",
                            f"{x:.6f}",
                            f"{y:.6f}",
                            f"{z:.6f}",
                        ])
                        pending += 1

            now = time.time()
            if pending and (pending >= 60 or now - last_flush >= 1.0 or should_stop):
                if self.frames_file is not None:
                    self.frames_file.flush()
                if self.imu_file is not None:
                    self.imu_file.flush()
                pending = 0
                last_flush = now

            if should_stop and self._write_queue.empty():
                break

    def _run(self):
        try:
            self._start_pipeline_with_fallback()
            self.status_changed.emit("connected", self.metadata.get("device_name", "Intel RealSense D435i"))
        except Exception as exc:
            self.running = False
            self.status_changed.emit("disconnected", str(exc))
            self.error_occurred.emit(f"D435i start failed: {exc}")
            return

        while self.running:
            try:
                frames = self.pipeline.poll_for_frames()
                if not frames:
                    now = time.time()
                    if now - self._last_waiting_log > 5.0:
                        self.status_changed.emit("connected", "D435i connected, waiting for frames")
                        self._last_waiting_log = now
                    time.sleep(0.01)
                    continue
                self._handle_motion_frames(frames, time.time())
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                ir_left_frame = frames.get_infrared_frame(1)
                ir_right_frame = frames.get_infrared_frame(2)
                if not color_frame or not depth_frame or not ir_left_frame or not ir_right_frame:
                    continue

                color = np.asanyarray(color_frame.get_data())
                ir_left = np.asanyarray(ir_left_frame.get_data())
                ir_right = np.asanyarray(ir_right_frame.get_data())
                stereo = np.hstack((ir_left, ir_right))
                stereo_bgr = cv2.cvtColor(stereo, cv2.COLOR_GRAY2BGR) if cv2 is not None else stereo

                now = time.time()
                if now - self._last_preview_emit_ts >= self.PREVIEW_EMIT_INTERVAL_S:
                    self._last_preview_emit_ts = now
                    self.rgb_frame.emit(color.copy())
                    self.stereo_frame.emit(stereo_bgr.copy())

                with self.lock:
                    recording = self.recording
                    start_ts = self._recording_start_ts
                    depth_dir = self.depth_dir
                    save_depth_raw = self.save_depth_raw
                    if recording and start_ts is not None:
                        frame_pc_ts = time.time()
                        self.frame_index += 1
                        frame_index = self.frame_index
                    else:
                        frame_index = 0

                if recording and start_ts is not None:
                    depth_name = f"depth_{frame_index:06d}.npy"
                    depth_rel = os.path.join("depth_raw_npy", depth_name).replace("\\", "/") if save_depth_raw else ""
                    depth_abs = os.path.join(depth_dir, depth_name) if save_depth_raw else ""
                    depth_data = np.asanyarray(depth_frame.get_data()).copy() if save_depth_raw else None
                    item = (
                        "video",
                        frame_index,
                        frame_pc_ts - start_ts,
                        color.copy(),
                        stereo_bgr.copy(),
                        depth_data,
                        depth_rel,
                        depth_abs,
                        save_depth_raw,
                        color_frame.get_frame_number(),
                        depth_frame.get_frame_number(),
                        ir_left_frame.get_frame_number(),
                        ir_right_frame.get_frame_number(),
                    )
                    try:
                        with self.lock:
                            if not self.recording or self._recording_start_ts != start_ts:
                                continue
                            self._write_queue.put_nowait(item)
                    except queue.Full:
                        self._dropped_video_frames += 1
                        if self._dropped_video_frames in (1, 10, 100) or self._dropped_video_frames % 1000 == 0:
                            self.error_occurred.emit(f"D435i disk queue full, dropped video frames={self._dropped_video_frames}")
            except Exception as exc:
                if self.running:
                    self.error_occurred.emit(f"D435i frame error: {exc}")
                    time.sleep(0.1)

        try:
            if self.pipeline is not None:
                self.pipeline.stop()
        except Exception:
            pass
        self.pipeline = None
        self.profile = None
        self.motion_streams_enabled = False
        self.motion_profile_fps = None
        self.status_changed.emit("disconnected", "stopped")

    def _handle_motion_frames(self, frames, pc_ts: float):
        motion_rows = []
        try:
            frame_count = frames.size()
        except Exception:
            frame_count = 0

        for index in range(frame_count):
            try:
                try:
                    frame = frames.get_frame(index)
                except Exception:
                    frame = frames[index]
                stream_type = frame.profile.stream_type()
                if stream_type not in (rs.stream.gyro, rs.stream.accel):
                    continue
                motion = frame.as_motion_frame()
                if not motion:
                    continue
                data = motion.get_motion_data()
                source = "gyro" if stream_type == rs.stream.gyro else "accel"
                motion_rows.append((
                    source,
                    frame.get_frame_number(),
                    frame.get_timestamp(),
                    data.x,
                    data.y,
                    data.z,
                ))
            except Exception:
                continue

        if not motion_rows:
            return

        with self.lock:
            if not self.recording or self.imu_writer is None or self._recording_start_ts is None:
                return
            rel_ts = pc_ts - self._recording_start_ts

        item = ("motion", [
            (rel_ts, source, frame_number, sensor_ts, x, y, z)
            for source, frame_number, sensor_ts, x, y, z in motion_rows
        ])
        try:
            with self.lock:
                if not self.recording:
                    return
                self._write_queue.put_nowait(item)
        except queue.Full:
            self._dropped_motion_rows += len(motion_rows)
            if self._dropped_motion_rows in (1, 100, 1000) or self._dropped_motion_rows % 10000 == 0:
                self.error_occurred.emit(f"D435i disk queue full, dropped motion rows={self._dropped_motion_rows}")

    def _start_pipeline_with_fallback(self):
        profiles = [
            {"color": (1280, 720, 30), "depth": (640, 480, 30), "ir": (640, 480, 30)},
            {"color": (1920, 1080, 30), "depth": (640, 480, 30), "ir": (640, 480, 30)},
            {"color": (640, 480, 30), "depth": (640, 480, 30), "ir": (640, 480, 30)},
        ]
        motion_profiles = self._available_motion_profiles()
        errors = []
        attempts = [(profile, motion_profile) for motion_profile in motion_profiles for profile in profiles]
        attempts.extend((profile, None) for profile in profiles)
        for profile, motion_profile in attempts:
            try:
                self.pipeline = rs.pipeline()
                config = self._build_realsense_config(profile, motion_profile)
                self.profile = self.pipeline.start(config)
                self.motion_streams_enabled = motion_profile is not None
                self.motion_profile_fps = motion_profile
                self._collect_metadata(profile)
                if motion_profile is None:
                    self.error_occurred.emit("D435i IMU stream unavailable; video/depth recording started without D435i IMU.")
                else:
                    self.error_occurred.emit(
                        "D435i IMU enabled with "
                        f"color={profile['color']}, depth={profile['depth']}, ir={profile['ir']}, "
                        f"gyro={motion_profile[0]}, accel={motion_profile[1]}"
                    )
                return
            except Exception as exc:
                video_label = f"color={profile['color']}, depth={profile['depth']}, ir={profile['ir']}"
                motion_label = "video-only" if motion_profile is None else f"gyro={motion_profile[0]}, accel={motion_profile[1]}"
                errors.append(f"{video_label}, {motion_label}: {exc}")
                try:
                    if self.pipeline is not None:
                        self.pipeline.stop()
                except Exception:
                    pass
                self.pipeline = None
                self.profile = None
                self.motion_streams_enabled = False
                self.motion_profile_fps = None
        raise RuntimeError(" | ".join(errors))

    def _available_motion_profiles(self) -> List[tuple]:
        fallback = [(200, 200), (400, 400), (200, 100), (400, 100)]
        try:
            context = rs.context()
            devices = context.query_devices()
            if not devices:
                return fallback
            gyro_fps = set()
            accel_fps = set()
            for sensor in devices[0].query_sensors():
                for profile in sensor.get_stream_profiles():
                    stream_type = profile.stream_type()
                    if stream_type == rs.stream.gyro:
                        gyro_fps.add(profile.fps())
                    elif stream_type == rs.stream.accel:
                        accel_fps.add(profile.fps())
            preferred = [(200, 200), (400, 400), (200, 100), (400, 100)]
            ordered = [pair for pair in preferred if pair[0] in gyro_fps and pair[1] in accel_fps]
            for gyro in sorted(gyro_fps, reverse=True):
                for accel in sorted(accel_fps, reverse=True):
                    pair = (gyro, accel)
                    if pair not in ordered:
                        ordered.append(pair)
            return ordered or fallback
        except Exception:
            return fallback

    def _build_realsense_config(self, profile: dict, motion_profile: Optional[tuple]):
        config = rs.config()
        cw, ch, cfps = profile["color"]
        dw, dh, dfps = profile["depth"]
        iw, ih, ifps = profile["ir"]
        config.enable_stream(rs.stream.color, cw, ch, rs.format.bgr8, cfps)
        config.enable_stream(rs.stream.depth, dw, dh, rs.format.z16, dfps)
        config.enable_stream(rs.stream.infrared, 1, iw, ih, rs.format.y8, ifps)
        config.enable_stream(rs.stream.infrared, 2, iw, ih, rs.format.y8, ifps)
        if motion_profile is not None:
            gyro_fps, accel_fps = motion_profile
            config.enable_stream(rs.stream.gyro, rs.format.motion_xyz32f, gyro_fps)
            config.enable_stream(rs.stream.accel, rs.format.motion_xyz32f, accel_fps)
        return config

    def _collect_metadata(self, stream_profile: Optional[dict] = None):
        stream_profile = stream_profile or {
            "color": (1280, 720, 30),
            "depth": (640, 480, 30),
            "ir": (640, 480, 30),
        }
        cw, ch, cfps = stream_profile["color"]
        dw, dh, dfps = stream_profile["depth"]
        iw, ih, ifps = stream_profile["ir"]
        self.metadata = {
            "device_name": "Intel RealSense",
            "serial_number": "",
            "rgb": {"width": cw, "height": ch, "fps": cfps, "format": "bgr8"},
            "depth": {"width": dw, "height": dh, "fps": dfps, "format": "z16"},
            "stereo": {"width": iw * 2, "height": ih, "fps": ifps, "format": "left_ir|right_ir"},
            "imu": {
                "enabled": self.motion_streams_enabled,
                "gyro": {
                    "format": "motion_xyz32f",
                    "fps": self.motion_profile_fps[0] if self.motion_profile_fps else None,
                } if self.motion_streams_enabled else None,
                "accel": {
                    "format": "motion_xyz32f",
                    "fps": self.motion_profile_fps[1] if self.motion_profile_fps else None,
                } if self.motion_streams_enabled else None,
            },
        }
        try:
            device = self.profile.get_device()
            self.metadata["device_name"] = device.get_info(rs.camera_info.name)
            self.metadata["serial_number"] = device.get_info(rs.camera_info.serial_number)
            depth_sensor = device.first_depth_sensor()
            self.metadata["depth_scale"] = depth_sensor.get_depth_scale()
        except Exception:
            pass

    def _write_metadata(self, output_dir: str, subject: str, imu_ports: List[int], session_start_ts: float):
        metadata = dict(self.metadata)
        metadata.update({
            "recording_started_at": datetime.now().isoformat(timespec="seconds"),
            "session_start_pc_timestamp": f"{session_start_ts:.6f}",
            "timestamp_zero": "sync_timestamp is seconds relative to session_start_pc_timestamp; world_time is HH:MM:SS:mmm.",
            "subject": subject,
            "imu_ports": imu_ports,
            "files": {
                "rgb": "RGB.mp4",
                "stereo": "Stereo.mp4",
                "depth_raw_dir": "depth_raw_npy" if self.save_depth_raw else None,
                "frames": "frames.csv",
                "imu": "imu.csv" if self.motion_streams_enabled else None,
            },
            "save_depth_raw": self.save_depth_raw,
        })
        with open(os.path.join(output_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)


class Page1Widget(QWidget):
    TASK_TYPES = [
        "直线步行",
        "窄通道步行",
        "过门步行",
        "双任务步行",
        "360° 原地转身",
        "180° 转身",
        "其他",
    ]

    IMU_CHANNELS = [
        ("Acc X", "acc", 0, (-5.0, 5.0)),
        ("Acc Y", "acc", 1, (-5.0, 5.0)),
        ("Acc Z", "acc", 2, (-5.0, 5.0)),
        ("Gyr X", "gyr", 0, (-100.0, 100.0)),
        ("Gyr Y", "gyr", 1, (-100.0, 100.0)),
        ("Gyr Z", "gyr", 2, (-100.0, 100.0)),
        ("Mag X", "gnt", 0, (-100.0, 100.0)),
        ("Mag Y", "gnt", 1, (-100.0, 100.0)),
        ("Mag Z", "gnt", 2, (-100.0, 100.0)),
        ("Angle X", "angle", 0, (-180.0, 180.0)),
        ("Angle Y", "angle", 1, (-180.0, 180.0)),
        ("Angle Z", "angle", 2, (-180.0, 180.0)),
    ]
    DEFAULT_IMU_PLOTS = [
        (1, 2),  # IMU1 Acc Z
        (2, 2),  # IMU2 Acc Z
        (3, 2),  # IMU3 Acc Z
        (4, 2),  # IMU4 Acc Z
        (5, 2),  # IMU5 Acc Z
        (1, 0),  # IMU1 Acc X
    ]

    USB_CAMERA_COUNT = 4
    D435I_RGB_WIDGET_INDEX = USB_CAMERA_COUNT
    D435I_STEREO_WIDGET_INDEX = USB_CAMERA_COUNT + 1
    D435I_WIDGET_INDICES = (D435I_RGB_WIDGET_INDEX, D435I_STEREO_WIDGET_INDEX)
    CAMERA_GRID_COLUMNS = 2

    REMOTE_DEVICE_INSTANCE_ID = r"BTHLE\DEV_2A0798DB3597\8&3E274D5&2&2A0798DB3597"
    REMOTE_ALLOWED_KEYS = (
        ("Key_VolumeUp", 16777330),
        ("Key_VolumeDown", 16777328),
    )
    REMOTE_DOUBLE_CLICK_WINDOW_MS = 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page_1")
        self.recording = False
        self.session_dir = ""
        self.session_start_ts: Optional[float] = None
        self.imu_recorder = WtMultiImuUdpRecorder(self)
        self.imu_recorder.status_changed.connect(self._on_imu_status)
        self.imu_recorder.sample_received.connect(self._on_imu_sample)
        self.imu_recorder.error_occurred.connect(self.log_message)
        self.imu_recorder.command_status.connect(self.log_message)
        self.biosignal_recorder = EmgEegUdpRecorder(self)
        self.biosignal_recorder.sample_batch_received.connect(self._on_biosignal_batch)
        self.biosignal_recorder.device_status_changed.connect(self._on_biosignal_device_status)
        self.biosignal_recorder.listener_status_changed.connect(self._on_biosignal_listener_status)
        self.biosignal_recorder.error_occurred.connect(self.log_message)
        self.biosignal_recorder.recording_failed.connect(self._on_biosignal_recording_failed)

        self.cameras: List[Optional[QCamera]] = []
        self.capture_sessions: List[QMediaCaptureSession] = []
        self.recorders: List[Optional[QMediaRecorder]] = []
        self.video_widgets: List[QWidget] = []
        self.camera_selects: List[RefreshCameraComboBox] = []
        self.camera_labels: List[QLabel] = []
        self.camera_status_labels: List[QLabel] = []
        self.video_refresh_buttons: List[QPushButton] = []
        self.imu_status_labels: List[QLabel] = []
        self.imu_command_select: Optional[QComboBox] = None
        self.imu_plot_widgets: List[OptimizedChannelPlot] = []
        self.imu_plot_imu_selects: List[QComboBox] = []
        self.imu_plot_channel_selects: List[QComboBox] = []
        self.port_spins: List[QSpinBox] = []
        self.imu_table: Optional[QTableWidget] = None
        self.emg_panel: Optional[BiosignalPanel] = None
        self.eeg_panel: Optional[BiosignalPanel] = None
        self.biosignal_status_labels: Dict[str, QLabel] = {}
        self.biosignal_wifi_ip_label: Optional[QLabel] = None
        self.biosignal_listener_label: Optional[QLabel] = None
        self.biosignal_port_spin: Optional[QSpinBox] = None
        self.btn_refresh_biosignal: Optional[QPushButton] = None
        self.biosignal_finalizing = False
        self._shutting_down = False
        self._biosignal_refresh_timer = QtCore.QTimer(self)
        self._biosignal_refresh_timer.setSingleShot(True)
        self._biosignal_refresh_timer.timeout.connect(self._restart_biosignal_listener)
        self._biosignal_finalize_timer = QtCore.QTimer(self)
        self._biosignal_finalize_timer.setInterval(1000)
        self._biosignal_finalize_timer.timeout.connect(self._poll_biosignal_finalize)
        self._wifi_ip_refresh_timer = QtCore.QTimer(self)
        self._wifi_ip_refresh_timer.setInterval(3000)
        self._wifi_ip_refresh_timer.timeout.connect(self._update_biosignal_wifi_ip_label)
        self._imu_refresh_generation = 0
        self.latest_imu_samples: Dict[int, dict] = {}
        self.remote_indicator_on = False
        self.remote_indicator_dot: Optional[QLabel] = None
        self.remote_indicator_label: Optional[QLabel] = None
        self.remote_connection_dot: Optional[QLabel] = None
        self.remote_connection_label: Optional[QLabel] = None
        self.remote_refresh_button: Optional[QPushButton] = None
        self.remote_connected = False
        self.remote_device_name = "Ulanzi MT-44 B"
        self.remote_pair_status = ""
        self.remote_input_status = ""
        self.remote_fog_label: Optional[QLabel] = None
        self.remote_fog_active = False
        self.remote_fog_start_pc_ts: Optional[float] = None
        self.remote_fog_start_key = ""
        self.remote_fog_interval_count = 0
        self.remote_fog_events_path = ""
        self.remote_fog_intervals_path = ""
        self.remote_experiment_active = False
        self.remote_experiment_start_pc_ts: Optional[float] = None
        self.remote_experiment_interval_count = 0
        self.remote_experiment_label: Optional[QLabel] = None
        self._pending_remote_click: Optional[dict] = None
        self._remote_single_click_timer = QtCore.QTimer(self)
        self._remote_single_click_timer.setSingleShot(True)
        self._remote_single_click_timer.timeout.connect(self._commit_pending_remote_single_click)
        self._remote_status_process: Optional[QtCore.QProcess] = None
        self._remote_status_timer: Optional[QtCore.QTimer] = None
        self.available_usb_devices = []
        self.cameras_initialized = False
        self.realsense = RealSenseD435iWorker(self)
        self.realsense.rgb_frame.connect(
            lambda frame: self._update_image_widget(self.D435I_RGB_WIDGET_INDEX, frame)
        )
        self.realsense.stereo_frame.connect(
            lambda frame: self._update_image_widget(self.D435I_STEREO_WIDGET_INDEX, frame)
        )
        self.realsense.status_changed.connect(self._on_realsense_status)
        self.realsense.error_occurred.connect(self.log_message)

        self.setup_ui()
        self._start_remote_status_monitor()

    def activate_page(self):
        self._shutting_down = False
        self._update_biosignal_wifi_ip_label()
        self._wifi_ip_refresh_timer.start()
        QtCore.QTimer.singleShot(0, self._start_capture_devices)

    def _start_capture_devices(self):
        if not self.cameras_initialized:
            self.setup_cameras()
        self.realsense.start_preview()
        ports = list(dict.fromkeys(spin.value() for spin in self.port_spins))
        self.imu_recorder.start(ports)
        biosignal_port = (
            self.biosignal_port_spin.value()
            if self.biosignal_port_spin is not None
            else EmgEegUdpRecorder.DEFAULT_PORT
        )
        self.biosignal_recorder.start(biosignal_port)

    def deactivate_page(self):
        if self.recording:
            return False
        self._shutting_down = True
        self._biosignal_refresh_timer.stop()
        self._wifi_ip_refresh_timer.stop()
        if self.biosignal_recorder.writer_pending:
            self._biosignal_finalize_timer.stop()
            self.biosignal_recorder.stop_recording(timeout_s=30.0)
            if self.biosignal_recorder.writer_pending:
                # Keep the window alive and resume polling instead of allowing
                # Qt to destroy the recorder while experimental rows are open.
                self._shutting_down = False
                self.biosignal_finalizing = True
                self._biosignal_finalize_timer.start()
                self.log_message("EMG/EEG 数据仍在落盘；完成前暂不关闭程序。")
                return False
        self._biosignal_finalize_timer.stop()
        self.realsense.stop_preview()
        self._stop_usb_cameras()
        self.imu_recorder.stop()
        self.biosignal_recorder.stop()
        if self._remote_status_timer is not None:
            self._remote_status_timer.stop()
        if self._remote_status_process is not None:
            if self._remote_status_process.state() != QtCore.QProcess.ProcessState.NotRunning:
                self._remote_status_process.kill()
                self._remote_status_process.waitForFinished(1000)
            self._remote_status_process = None
        return True

    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        main_row = QHBoxLayout()
        main_row.setSpacing(12)

        left_panel = QVBoxLayout()
        left_panel.setSpacing(12)

        data_tabs = QtWidgets.QTabWidget()
        self.data_tabs = data_tabs
        data_tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #E5E5EA; border-radius: 4px; } "
            "QTabBar::tab { font-size: 11px; font-weight: 600; padding: 6px 16px; } "
            "QTabBar::tab:selected { background: #1A1A1A; color: #FFFFFF; }"
        )

        imu_tab = QWidget()
        imu_tab_layout = QVBoxLayout(imu_tab)
        imu_tab_layout.setContentsMargins(8, 8, 8, 8)

        imu_group = QGroupBox("实时 9 轴数据")
        imu_layout = QVBoxLayout(imu_group)
        imu_plot_grid = QGridLayout()
        imu_plot_grid.setContentsMargins(2, 2, 2, 2)
        imu_plot_grid.setSpacing(6)
        for plot_index in range(6):
            panel = self._create_imu_plot_panel(plot_index)
            imu_plot_grid.addWidget(panel, plot_index // 3, plot_index % 3)
        imu_layout.addLayout(imu_plot_grid, 3)

        self.imu_table = QTableWidget(5, 16)
        self.imu_table.setHorizontalHeaderLabels([
            "IMU", "Status", "Packets",
            "Acc X", "Acc Y", "Acc Z",
            "Gyr X", "Gyr Y", "Gyr Z",
            "Angle X", "Angle Y", "Angle Z",
            "Mag X", "Mag Y", "Mag Z",
            "Battery",
        ])
        self.imu_table.verticalHeader().setVisible(False)
        self.imu_table.setAlternatingRowColors(True)
        self.imu_table.setMaximumHeight(190)
        self.imu_table.setMinimumHeight(150)
        self.imu_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.imu_table.horizontalHeader()
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(15, QHeaderView.ResizeMode.Stretch)
        self.imu_table.setColumnWidth(0, 150)
        self.imu_table.setColumnWidth(1, 96)
        self.imu_table.setColumnWidth(2, 68)
        for col in range(3, 15):
            self.imu_table.setColumnWidth(col, 72)

        for idx in range(5):
            port_spin = QSpinBox()
            port_spin.setRange(1, 65535)
            port_spin.setValue(1399)
            self.port_spins.append(port_spin)
            device_id = WtMultiImuUdpRecorder.DEVICE_IDS.get(idx + 1, "")
            self.imu_table.setItem(idx, 0, QTableWidgetItem(f"IMU{idx + 1} {device_id}"))
            for col in range(1, 16):
                item = QTableWidgetItem("idle" if col == 1 else "")
                if col == 1:
                    item.setForeground(QtGui.QColor("#666666"))
                self.imu_table.setItem(idx, col, item)
        imu_layout.addWidget(self.imu_table)
        imu_tab_layout.addWidget(imu_group)
        data_tabs.addTab(imu_tab, "IMU")

        self.emg_panel = BiosignalPanel(
            "子框 1 · 8 通道低频肌电（1000 Hz）",
            [
                "EMG CH1 · 000001/CH1", "EMG CH2 · 000001/CH2",
                "EMG CH3 · 000001/CH3", "EMG CH4 · 000001/CH4",
                "EMG CH5 · 000002/CH1", "EMG CH6 · 000002/CH2",
                "EMG CH7 · 000002/CH3", "EMG CH8 · 000002/CH4",
            ],
            serials=("000001", "000002"),
            columns=2,
        )
        data_tabs.addTab(self.emg_panel, "低频肌电 EMG")

        self.eeg_panel = BiosignalPanel(
            "子框 2 · 3 通道脑电（1000 Hz）",
            ["EEG CH1 · 000003/CH1", "EEG CH2 · 000003/CH2", "EEG CH3 · 000003/CH3"],
            serials=("000003",),
            columns=3,
        )
        data_tabs.addTab(self.eeg_panel, "脑电 EEG")

        camera_tab = QWidget()
        camera_tab_layout = QVBoxLayout(camera_tab)
        camera_tab_layout.setContentsMargins(8, 8, 8, 8)

        video_tabs = QtWidgets.QTabWidget()
        video_tabs.setDocumentMode(True)

        def prepare_camera_grid(grid: QGridLayout, panel_count: int):
            for col in range(self.CAMERA_GRID_COLUMNS):
                grid.setColumnStretch(col, 1)
            row_count = (panel_count + self.CAMERA_GRID_COLUMNS - 1) // self.CAMERA_GRID_COLUMNS
            for row in range(row_count):
                grid.setRowStretch(row, 1)

        def add_camera_panel(grid: QGridLayout, widget_index: int, camera_title: str, grid_index: int):
            box = QGroupBox(camera_title)
            box_layout = QVBoxLayout(box)
            if widget_index < self.USB_CAMERA_COUNT:
                selector = RefreshCameraComboBox()
                selector.setMinimumHeight(28)
                selector.about_to_show.connect(self.refresh_camera_devices)
                selector.currentIndexChanged.connect(
                    lambda _index, camera_index=widget_index: self._on_usb_camera_selection_changed(camera_index)
                )
                box_layout.addWidget(selector)
                self.camera_selects.append(selector)
                video = QVideoWidget()
                video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            else:
                video = QLabel("等待 D435i 数据")
                video.setAlignment(Qt.AlignmentFlag.AlignCenter)
                video.setStyleSheet("background: #111; color: #ccc;")
            video.setMinimumSize(320, 180)
            video.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            label = QLabel("未连接")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status = QLabel("状态: disconnected")
            status.setStyleSheet("color: #666;")
            box_layout.addWidget(video, 1)
            box_layout.addWidget(label)
            box_layout.addWidget(status)
            self.video_widgets.append(video)
            self.camera_labels.append(label)
            self.camera_status_labels.append(status)
            grid.addWidget(box, grid_index // self.CAMERA_GRID_COLUMNS, grid_index % self.CAMERA_GRID_COLUMNS)

        usb_camera_tab = QWidget()
        usb_camera_layout = QVBoxLayout(usb_camera_tab)
        usb_camera_layout.setContentsMargins(4, 4, 4, 4)
        usb_refresh_row = QHBoxLayout()
        usb_refresh_row.addStretch(1)
        self.btn_refresh_usb_video_devices = QPushButton("刷新 USB 视频设备")
        self.btn_refresh_usb_video_devices.clicked.connect(self.refresh_usb_video_devices)
        usb_refresh_row.addWidget(self.btn_refresh_usb_video_devices)
        usb_camera_layout.addLayout(usb_refresh_row)
        self.video_refresh_buttons.append(self.btn_refresh_usb_video_devices)
        usb_camera_grid = QGridLayout()
        usb_camera_grid.setSpacing(8)
        prepare_camera_grid(usb_camera_grid, self.USB_CAMERA_COUNT)
        for idx in range(self.USB_CAMERA_COUNT):
            add_camera_panel(usb_camera_grid, idx, f"USB Camera {idx + 1}", idx)
        usb_camera_layout.addLayout(usb_camera_grid, 1)
        video_tabs.addTab(usb_camera_tab, "USB Camera")

        d435i_tab = QWidget()
        d435i_layout = QVBoxLayout(d435i_tab)
        d435i_layout.setContentsMargins(4, 4, 4, 4)
        d435i_refresh_row = QHBoxLayout()
        d435i_refresh_row.addStretch(1)
        self.btn_refresh_d435i_video_devices = QPushButton("刷新 D435i 视频设备")
        self.btn_refresh_d435i_video_devices.clicked.connect(self.refresh_d435i_video_devices)
        d435i_refresh_row.addWidget(self.btn_refresh_d435i_video_devices)
        d435i_layout.addLayout(d435i_refresh_row)
        self.video_refresh_buttons.append(self.btn_refresh_d435i_video_devices)
        d435i_grid = QGridLayout()
        d435i_grid.setSpacing(8)
        d435i_specs = [
            (self.D435I_RGB_WIDGET_INDEX, "D435i RGB"),
            (self.D435I_STEREO_WIDGET_INDEX, "D435i Stereo"),
        ]
        prepare_camera_grid(d435i_grid, len(d435i_specs))
        for grid_index, (widget_index, title) in enumerate(d435i_specs):
            add_camera_panel(d435i_grid, widget_index, title, grid_index)
        d435i_layout.addLayout(d435i_grid, 1)
        video_tabs.addTab(d435i_tab, "D435i")

        camera_tab_layout.addWidget(video_tabs, 1)
        data_tabs.addTab(camera_tab, "视频预览")
        left_panel.addWidget(data_tabs, 1)
        main_row.addLayout(left_panel, 1)

        right_panel = QVBoxLayout()
        right_panel.setSpacing(12)

        info_group = QGroupBox("被试与保存")
        info_layout = QFormLayout(info_group)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("请输入被试姓名")
        self.task_type_combo = QComboBox()
        self.task_type_combo.addItems(self.TASK_TYPES)
        self.task_type_combo.setMinimumWidth(140)
        name_row = QHBoxLayout()
        name_row.addWidget(self.name_input, 1)
        name_row.addWidget(QLabel("任务"))
        name_row.addWidget(self.task_type_combo)
        documents_dir = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation
        )
        if not documents_dir:
            documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
        default_data_dir = os.path.join(documents_dir, "UpStudioFOGTool", "data")
        self.base_dir_input = QLineEdit(os.path.abspath(default_data_dir))
        browse_btn = QPushButton("选择目录")
        browse_btn.clicked.connect(self.choose_base_dir)
        path_row = QHBoxLayout()
        path_row.addWidget(self.base_dir_input, 1)
        path_row.addWidget(browse_btn)
        info_layout.addRow("姓名", name_row)
        info_layout.addRow("保存目录", path_row)
        right_container = QWidget()
        right_container.setMinimumWidth(320)
        right_container.setMaximumWidth(380)
        right_container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        right_container.setLayout(right_panel)
        right_panel.addWidget(info_group)

        control_group = QGroupBox("采集控制")
        control_layout = QVBoxLayout(control_group)
        self.btn_start = QPushButton("开始采集")
        self.btn_stop = QPushButton("停止采集")
        self.btn_start.setMinimumHeight(44)
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_collection)
        self.btn_stop.clicked.connect(self.stop_collection)
        self.enable_d435i_checkbox = QCheckBox("启用 D435i 视频采集")
        self.enable_d435i_checkbox.setChecked(True)
        self.enable_d435i_checkbox.setToolTip("关闭后本次采集不检查、不录制 D435i RGB/Stereo/depth_raw 数据")
        self.btn_record_baseline = QPushButton("记录佩戴基线")
        self.btn_record_baseline.setToolTip("开始采集前记录当前在线 IMU 姿态，并保存 baseline CSV")
        self.btn_record_baseline.clicked.connect(self.record_wearing_baseline)
        depth_row = QHBoxLayout()
        depth_row.addWidget(self.enable_d435i_checkbox, 1)
        depth_row.addWidget(self.btn_record_baseline)
        control_layout.addWidget(self.btn_start)
        control_layout.addWidget(self.btn_stop)
        control_layout.addLayout(depth_row)
        right_panel.addWidget(control_group)

        biosignal_group = QGroupBox("EMG / EEG 接收")
        biosignal_layout = QVBoxLayout(biosignal_group)
        biosignal_port_row = QHBoxLayout()
        biosignal_port_row.addWidget(QLabel("UDP 端口"))
        self.biosignal_port_spin = QSpinBox()
        self.biosignal_port_spin.setRange(1024, 65534)
        self.biosignal_port_spin.setValue(EmgEegUdpRecorder.DEFAULT_PORT)
        self.biosignal_port_spin.setToolTip("与 emg_program 默认配置一致；独立程序运行时会占用该端口。")
        self.btn_refresh_biosignal = QPushButton("重新监听")
        self.btn_refresh_biosignal.clicked.connect(self.refresh_biosignal_connection)
        biosignal_port_row.addWidget(self.biosignal_port_spin, 1)
        biosignal_port_row.addWidget(self.btn_refresh_biosignal)
        biosignal_layout.addLayout(biosignal_port_row)
        self.biosignal_wifi_ip_label = QLabel("当前 Wi-Fi 主机 IP 是：检测中")
        self.biosignal_wifi_ip_label.setStyleSheet("color: #666; font-weight: 600;")
        biosignal_layout.addWidget(self.biosignal_wifi_ip_label)
        self._update_biosignal_wifi_ip_label()
        self.biosignal_listener_label = QLabel("接收器：等待启动")
        self.biosignal_listener_label.setStyleSheet("color: #777;")
        biosignal_layout.addWidget(self.biosignal_listener_label)
        for serial, name in (("000001", "EMG 1"), ("000002", "EMG 2"), ("000003", "EEG")):
            label = QLabel(f"● {name} · {serial} · 等待数据")
            label.setStyleSheet("color: #777;")
            self.biosignal_status_labels[serial] = label
            biosignal_layout.addWidget(label)
        right_panel.addWidget(biosignal_group)

        remote_group = QGroupBox("蓝牙遥控器")
        remote_layout = QVBoxLayout(remote_group)
        remote_layout.setContentsMargins(10, 8, 10, 8)
        remote_layout.setSpacing(6)

        remote_connection_row = QHBoxLayout()
        remote_connection_row.setSpacing(8)
        self.remote_connection_dot = QLabel()
        self.remote_connection_dot.setFixedSize(12, 12)
        self.remote_connection_label = QLabel()
        self.remote_refresh_button = QPushButton("刷新")
        self.remote_refresh_button.setFixedWidth(58)
        self.remote_refresh_button.clicked.connect(self.refresh_remote_connection_status)
        remote_connection_row.addWidget(self.remote_connection_dot)
        remote_connection_row.addWidget(self.remote_connection_label, 1)
        remote_connection_row.addWidget(self.remote_refresh_button)
        remote_layout.addLayout(remote_connection_row)

        remote_indicator_row = QHBoxLayout()
        remote_indicator_row.setSpacing(10)
        self.remote_indicator_dot = QLabel()
        self.remote_indicator_dot.setFixedSize(24, 24)
        self.remote_indicator_label = QLabel()
        self.remote_indicator_label.setMinimumWidth(180)
        remote_indicator_row.addWidget(self.remote_indicator_dot)
        remote_indicator_row.addWidget(self.remote_indicator_label, 1)
        remote_layout.addLayout(remote_indicator_row)

        self.remote_fog_label = QLabel()
        self.remote_fog_label.setWordWrap(True)
        remote_layout.addWidget(self.remote_fog_label)
        self.remote_experiment_label = QLabel()
        self.remote_experiment_label.setWordWrap(True)
        remote_layout.addWidget(self.remote_experiment_label)
        self._update_remote_indicator()
        self._update_remote_connection_status_ui()
        self._update_remote_fog_label()
        self._update_remote_experiment_label()
        right_panel.addWidget(remote_group)

        imu_calib_group = QGroupBox("IMU 校准")
        imu_calib_layout = QVBoxLayout(imu_calib_group)
        imu_target_row = QHBoxLayout()
        imu_target_row.addWidget(QLabel("目标 IMU"))
        self.imu_command_select = QComboBox()
        self.imu_command_select.addItem("所有在线 IMU", 0)
        self.imu_command_select.setEnabled(False)
        imu_target_row.addWidget(self.imu_command_select, 1)
        imu_calib_layout.addLayout(imu_target_row)

        btn_acc_calib = QPushButton("加速度计校准")
        btn_angle_zero = QPushButton("角度置零")
        btn_refresh_imu = QPushButton("刷新 IMU 连接")
        btn_acc_calib.clicked.connect(self._on_accelerometer_calibration_clicked)
        btn_angle_zero.clicked.connect(self._on_angle_zero_clicked)
        btn_refresh_imu.clicked.connect(self.refresh_imu_connection)
        imu_calib_layout.addWidget(btn_refresh_imu)
        imu_calib_layout.addWidget(btn_acc_calib)
        imu_calib_layout.addWidget(btn_angle_zero)
        right_panel.addWidget(imu_calib_group)

        log_group = QGroupBox("操作日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(200)
        log_layout.addWidget(self.log_text)
        right_panel.addWidget(log_group, 1)
        main_row.addWidget(right_container)
        main_row.setStretch(0, 1)
        main_row.setStretch(1, 0)

        root.addLayout(main_row, 1)

    def handle_remote_button_click(self, key_name: str = "", key_code: Optional[int] = None):
        pc_ts = time.time()
        self.remote_indicator_on = not self.remote_indicator_on
        self._update_remote_indicator()
        self.remote_connected = True
        self._update_remote_connection_status_ui()
        if self.recording:
            self._route_recording_remote_click(pc_ts, key_name, key_code)
        else:
            self._clear_pending_remote_single_click()
            self._update_remote_fog_label()
            self._update_remote_experiment_label()

    def _route_recording_remote_click(self, pc_ts: float, key_name: str, key_code: Optional[int]):
        click = {"pc_ts": pc_ts, "key_name": key_name, "key_code": key_code}
        if self._pending_remote_click is not None:
            first_click = self._pending_remote_click
            elapsed_ms = (pc_ts - float(first_click["pc_ts"])) * 1000.0
            if elapsed_ms <= self.REMOTE_DOUBLE_CLICK_WINDOW_MS:
                self._clear_pending_remote_single_click()
                try:
                    self._handle_remote_double_click(first_click, click)
                except Exception as exc:
                    self._handle_remote_label_write_failure(exc)
                return
            if not self._commit_pending_remote_single_click() or not self.recording:
                return

        self._pending_remote_click = click
        self._remote_single_click_timer.start(self.REMOTE_DOUBLE_CLICK_WINDOW_MS)
        self._update_remote_fog_label()

    def _clear_pending_remote_single_click(self):
        if self._remote_single_click_timer.isActive():
            self._remote_single_click_timer.stop()
        self._pending_remote_click = None

    def _commit_pending_remote_single_click(self, stop_on_failure: bool = True) -> bool:
        click = self._pending_remote_click
        self._clear_pending_remote_single_click()
        if not click or not self.recording:
            self._update_remote_fog_label()
            return True
        try:
            self._toggle_remote_fog_label(
                float(click["pc_ts"]),
                str(click.get("key_name", "")),
                click.get("key_code"),
            )
        except Exception as exc:
            if stop_on_failure:
                self._handle_remote_label_write_failure(exc)
            else:
                raise
            return False
        return True

    def _handle_remote_label_write_failure(self, exc: Exception):
        message = f"蓝牙遥控标签写入失败：{exc}"
        self.log_message(message)
        if not self.recording:
            return
        try:
            self.stop_collection()
        finally:
            QMessageBox.critical(
                self,
                "蓝牙标签写入失败",
                "采集已停止，以避免视频、IMU 与生理信号标签不一致。\n\n" + message,
            )

    def _handle_remote_double_click(self, first_click: dict, second_click: dict):
        event_ts = float(first_click["pc_ts"])
        key_name = self._combined_remote_key_text(first_click, second_click)
        key_code = first_click.get("key_code")
        if self.remote_experiment_active:
            self._end_remote_experiment(event_ts, key_name, key_code)
        else:
            self._begin_remote_experiment(event_ts, key_name, key_code)
        self._update_remote_fog_label()

    def _begin_remote_experiment(self, pc_ts: float, key_name: str, key_code: Optional[int]):
        interval_index = self.remote_experiment_interval_count + 1
        self.remote_experiment_active = True
        self.remote_experiment_start_pc_ts = pc_ts
        self._append_remote_fog_event("experiment_start", pc_ts, key_name, key_code, interval_index)
        self._append_session_event("remote_experiment_start", pc_ts)
        self._update_remote_experiment_label()
        self.log_message(f"实验开始 #{interval_index}: {self._event_sync_timestamp(pc_ts):.3f}s")

        baseline_path = self._save_wearing_baseline(
            allow_recording=True,
            show_dialog=False,
            source="remote_experiment_start",
        )
        if baseline_path:
            self._append_session_event("remote_experiment_baseline_saved", time.time())

    def _end_remote_experiment(
        self,
        pc_ts: float,
        key_name: str = "",
        key_code: Optional[int] = None,
        event_type: str = "experiment_end",
    ):
        interval_index = self.remote_experiment_interval_count + 1
        if self.remote_experiment_start_pc_ts is not None:
            start_rel = self._event_sync_timestamp(self.remote_experiment_start_pc_ts)
            end_rel = max(start_rel, self._event_sync_timestamp(pc_ts))
            duration_s = end_rel - start_rel
            self.log_message(f"实验结束 #{interval_index}: {end_rel:.3f}s，时长 {duration_s:.3f}s")
        else:
            self.log_message(f"实验结束 #{interval_index}: {self._event_sync_timestamp(pc_ts):.3f}s")
        self._append_remote_fog_event(event_type, pc_ts, key_name, key_code, interval_index)
        self._append_session_event(
            "remote_experiment_end" if event_type == "experiment_end" else "remote_experiment_end_auto_stop",
            pc_ts,
        )
        self.remote_experiment_interval_count = interval_index
        self.remote_experiment_active = False
        self.remote_experiment_start_pc_ts = None
        self._update_remote_experiment_label()

    def _start_remote_fog_labeling(self):
        self._clear_pending_remote_single_click()
        self.remote_fog_active = False
        self.remote_fog_start_pc_ts = None
        self.remote_fog_start_key = ""
        self.remote_fog_interval_count = 0
        self.remote_experiment_active = False
        self.remote_experiment_start_pc_ts = None
        self.remote_experiment_interval_count = 0
        self.remote_fog_events_path = os.path.join(self.session_dir, "remote_fog_events.csv")
        self.remote_fog_intervals_path = os.path.join(self.session_dir, "remote_fog_intervals.csv")
        with open(self.remote_fog_events_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["event_type", "world_time", "sync_timestamp", "key_name", "key_code", "interval_index"])
        with open(self.remote_fog_intervals_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "interval_index",
                "start_world_time",
                "end_world_time",
                "start_sync_timestamp",
                "end_sync_timestamp",
                "duration_s",
                "start_key",
                "end_key",
            ])
        self._update_remote_fog_label()
        self._update_remote_experiment_label()

    def _toggle_remote_fog_label(self, pc_ts: float, key_name: str, key_code: Optional[int]):
        if self.remote_fog_active:
            self._end_remote_fog_label(pc_ts, key_name, key_code, "fog_end")
        else:
            self._begin_remote_fog_label(pc_ts, key_name, key_code)

    def _begin_remote_fog_label(self, pc_ts: float, key_name: str, key_code: Optional[int]):
        interval_index = self.remote_fog_interval_count + 1
        self.remote_fog_active = True
        self.remote_fog_start_pc_ts = pc_ts
        self.remote_fog_start_key = self._remote_key_text(key_name, key_code)
        self._append_remote_fog_event("fog_start", pc_ts, key_name, key_code, interval_index)
        self._append_session_event("remote_fog_start", pc_ts)
        self._update_remote_fog_label()
        self.log_message(f"FOG 开始 #{interval_index}: {self._event_sync_timestamp(pc_ts):.3f}s")

    def _end_remote_fog_label(self, pc_ts: float, key_name: str = "", key_code: Optional[int] = None, event_type: str = "fog_end"):
        if self.remote_fog_start_pc_ts is None:
            self.remote_fog_active = False
            self._update_remote_fog_label()
            return

        interval_index = self.remote_fog_interval_count + 1
        start_pc_ts = self.remote_fog_start_pc_ts
        start_rel = self._event_sync_timestamp(start_pc_ts)
        end_rel = max(start_rel, self._event_sync_timestamp(pc_ts))
        duration_s = end_rel - start_rel
        end_key = self._remote_key_text(key_name, key_code)

        self._append_remote_fog_event(event_type, pc_ts, key_name, key_code, interval_index)
        self._append_remote_fog_interval(interval_index, start_rel, end_rel, duration_s, start_pc_ts, pc_ts, self.remote_fog_start_key, end_key)
        self._append_session_event("remote_fog_end" if event_type == "fog_end" else "remote_fog_end_auto_stop", pc_ts)

        self.remote_fog_interval_count = interval_index
        self.remote_fog_active = False
        self.remote_fog_start_pc_ts = None
        self.remote_fog_start_key = ""
        self._update_remote_fog_label()
        self.log_message(f"FOG 结束 #{interval_index}: {end_rel:.3f}s，时长 {duration_s:.3f}s")

    def _append_remote_fog_event(self, event_type: str, pc_ts: float, key_name: str, key_code: Optional[int], interval_index: int):
        if not self.remote_fog_events_path:
            return
        with open(self.remote_fog_events_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            sync_ts = self._event_sync_timestamp(pc_ts)
            writer.writerow([
                event_type,
                self._format_event_world_time(pc_ts),
                f"{sync_ts:.6f}",
                key_name,
                "" if key_code is None else key_code,
                interval_index,
            ])
        self._append_sync_event(
            "bluetooth_remote",
            event_type,
            pc_ts,
            self._remote_key_text(key_name, key_code),
        )

    def _append_remote_fog_interval(
        self,
        interval_index: int,
        start_rel: float,
        end_rel: float,
        duration_s: float,
        start_pc_ts: float,
        end_pc_ts: float,
        start_key: str,
        end_key: str,
    ):
        if not self.remote_fog_intervals_path:
            return
        with open(self.remote_fog_intervals_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                interval_index,
                self._format_event_world_time(start_pc_ts),
                self._format_event_world_time(end_pc_ts),
                f"{start_rel:.6f}",
                f"{end_rel:.6f}",
                f"{duration_s:.6f}",
                start_key,
                end_key,
            ])

    def _event_sync_timestamp(self, pc_ts: float) -> float:
        if self.session_start_ts is None:
            return 0.0
        return max(0.0, pc_ts - self.session_start_ts)

    @staticmethod
    def _format_event_world_time(pc_ts: float) -> str:
        return WtMultiImuUdpRecorder._format_world_time(pc_ts)

    @staticmethod
    def _remote_key_text(key_name: str, key_code: Optional[int]) -> str:
        return f"{key_name}/{key_code}" if key_name or key_code is not None else ""

    def _combined_remote_key_text(self, first_click: dict, second_click: dict) -> str:
        first = self._remote_key_text(str(first_click.get("key_name", "")), first_click.get("key_code"))
        second = self._remote_key_text(str(second_click.get("key_name", "")), second_click.get("key_code"))
        return f"{first}+{second}" if first and second else first or second

    def _start_remote_status_monitor(self):
        self._remote_status_process = QtCore.QProcess(self)
        self._remote_status_process.finished.connect(self._on_remote_status_process_finished)
        self._remote_status_process.errorOccurred.connect(self._on_remote_status_process_error)
        self._remote_status_timer = QtCore.QTimer(self)
        self._remote_status_timer.timeout.connect(self._refresh_remote_connection_status)
        self._remote_status_timer.start(10000)
        self._refresh_remote_connection_status()

    def refresh_remote_connection_status(self):
        self._refresh_remote_connection_status(manual=True)

    def _refresh_remote_connection_status(self, manual: bool = False):
        if self._remote_status_process is None:
            return
        if self._remote_status_process.state() != QtCore.QProcess.ProcessState.NotRunning:
            return

        if manual and self.remote_connection_label is not None:
            self.remote_connection_label.setText(f"连接状态：检测中 ({self.remote_device_name})")
            self.remote_connection_label.setStyleSheet("font-weight: 600; color: #666;")
        if self.remote_refresh_button is not None:
            self.remote_refresh_button.setEnabled(False)

        command = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            f"$device = Get-PnpDevice -InstanceId '{self.REMOTE_DEVICE_INSTANCE_ID}' -ErrorAction SilentlyContinue; "
            "$input = Get-PnpDevice -Class HIDClass -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InstanceId -like 'HID\\{00001812-0000-1000-8000-00805F9B34FB}_DEV_VID*2A0798DB3597*' } | "
            "Select-Object -First 1; "
            "$name = if ($device) { $device.FriendlyName } else { 'Ulanzi MT-44 B' }; "
            "$pair = if ($device) { $device.Status } else { 'Missing' }; "
            "if ($input) { Write-Output \"$($input.Status)|$name|$pair|$($input.FriendlyName)\" } "
            "else { Write-Output \"MissingInput|$name|$pair|\" }"
        )
        self._remote_status_process.start(
            "powershell.exe",
            ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        )

    def _on_remote_status_process_finished(self, exit_code: int, _exit_status):
        if self._remote_status_process is None:
            return
        if self.remote_refresh_button is not None:
            self.remote_refresh_button.setEnabled(True)

        output = bytes(self._remote_status_process.readAllStandardOutput()).decode("utf-8", errors="ignore").strip()
        status = ""
        name = ""
        pair_status = ""
        input_name = ""
        if output:
            first_line = output.splitlines()[0]
            parts = first_line.split("|", 3)
            status = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ""
            pair_status = parts[2].strip() if len(parts) > 2 else ""
            input_name = parts[3].strip() if len(parts) > 3 else ""

        if exit_code == 0 and status:
            self.remote_connected = status.upper() == "OK"
            self.remote_input_status = status
            self.remote_pair_status = pair_status
            if name:
                self.remote_device_name = name
            if input_name and status.upper() != "OK":
                self.remote_input_status = f"{status}/{input_name}"
        else:
            self.remote_connected = False
            self.remote_input_status = ""

        self._update_remote_connection_status_ui()

    def _on_remote_status_process_error(self, _error):
        self.remote_connected = False
        if self.remote_refresh_button is not None:
            self.remote_refresh_button.setEnabled(True)
        self._update_remote_connection_status_ui()

    def _update_remote_indicator(self):
        if self.remote_indicator_dot is None or self.remote_indicator_label is None:
            return

        if self.remote_indicator_on:
            color = "#17a34a"
            shadow = "#b9f6ca"
            label = "已接收：绿灯"
        else:
            color = "#d32f2f"
            shadow = "#ffcdd2"
            label = "等待单击：红灯"

        self.remote_indicator_dot.setStyleSheet(
            "QLabel {"
            f"background-color: {color};"
            f"border: 3px solid {shadow};"
            "border-radius: 12px;"
            "}"
        )
        self.remote_indicator_label.setText(label)
        self.remote_indicator_label.setStyleSheet("font-weight: 600; color: #333;")

    def _update_remote_connection_status_ui(self):
        if self.remote_connection_dot is None or self.remote_connection_label is None:
            return

        if self.remote_connected:
            color = "#17a34a"
            text = f"连接状态：已连接 ({self.remote_device_name})"
        elif self.remote_pair_status.upper() == "OK":
            color = "#a66a00"
            input_status = self.remote_input_status or "unknown"
            text = f"连接状态：未连接，已配对 ({self.remote_device_name}, HID: {input_status})"
        else:
            color = "#b00020"
            text = f"连接状态：未连接 ({self.remote_device_name})"

        self.remote_connection_dot.setStyleSheet(
            "QLabel {"
            f"background-color: {color};"
            "border-radius: 6px;"
            "}"
        )
        self.remote_connection_label.setText(text)
        self.remote_connection_label.setStyleSheet(f"font-weight: 600; color: {color};")

    def _update_remote_fog_label(self):
        if self.remote_fog_label is None:
            return

        if not self.recording:
            text = "FOG label: not recording"
            color = "#666666"
        elif self._pending_remote_click is not None:
            text = (
                f"FOG label: pending single click; second click within "
                f"{self.REMOTE_DOUBLE_CLICK_WINDOW_MS / 1000:.1f}s marks experiment start/end"
            )
            color = "#8a6d00"
        elif self.remote_fog_active and self.remote_fog_start_pc_ts is not None:
            text = f"FOG label: active since {self._event_sync_timestamp(self.remote_fog_start_pc_ts):.3f}s; next click ends FOG"
            color = "#b00020"
        else:
            text = f"FOG label: ready; intervals saved {self.remote_fog_interval_count}; next click starts FOG"
            color = "#138a36"

        self.remote_fog_label.setText(text)
        self.remote_fog_label.setStyleSheet(f"font-weight: 600; color: {color};")

    def _update_remote_experiment_label(self):
        if self.remote_experiment_label is None:
            return

        if not self.recording:
            text = "Experiment: not recording"
            color = "#666666"
        elif self.remote_experiment_active and self.remote_experiment_start_pc_ts is not None:
            text = (
                "Experiment: active since "
                f"{self._event_sync_timestamp(self.remote_experiment_start_pc_ts):.3f}s; double-click ends"
            )
            color = "#138a36"
        elif self.remote_experiment_interval_count:
            text = f"Experiment: ended; intervals saved {self.remote_experiment_interval_count}"
            color = "#333333"
        else:
            text = "Experiment: waiting; double-click remote to start and save baseline"
            color = "#b00020"

        self.remote_experiment_label.setText(text)
        self.remote_experiment_label.setStyleSheet(f"font-weight: 600; color: {color};")

    def _create_imu_plot_panel(self, plot_index: int) -> QWidget:
        panel = QGroupBox(f"Axis {plot_index + 1}")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(6, 6, 6, 6)
        panel_layout.setSpacing(4)

        control_row = QHBoxLayout()
        control_row.setSpacing(6)

        imu_select = QComboBox()
        for imu_index in range(1, 6):
            imu_select.addItem(f"IMU{imu_index}", imu_index)

        channel_select = QComboBox()
        for channel_index, (label, *_rest) in enumerate(self.IMU_CHANNELS):
            channel_select.addItem(label, channel_index)

        default_imu, default_channel = self.DEFAULT_IMU_PLOTS[plot_index]
        imu_select.setCurrentIndex(max(0, min(default_imu - 1, imu_select.count() - 1)))
        channel_select.setCurrentIndex(max(0, min(default_channel, channel_select.count() - 1)))

        imu_select.currentIndexChanged.connect(
            lambda _index, axis_index=plot_index: self._on_imu_plot_selection_changed(axis_index)
        )
        channel_select.currentIndexChanged.connect(
            lambda _index, axis_index=plot_index: self._on_imu_plot_selection_changed(axis_index)
        )

        control_row.addWidget(QLabel("IMU"))
        control_row.addWidget(imu_select, 1)
        control_row.addWidget(QLabel("Channel"))
        control_row.addWidget(channel_select, 1)
        panel_layout.addLayout(control_row)

        plot = OptimizedChannelPlot(plot_index)
        plot.setMinimumHeight(120)
        panel_layout.addWidget(plot, 1)

        self.imu_plot_imu_selects.append(imu_select)
        self.imu_plot_channel_selects.append(channel_select)
        self.imu_plot_widgets.append(plot)
        self._on_imu_plot_selection_changed(plot_index)
        return panel

    def _on_imu_plot_selection_changed(self, plot_index: int):
        if plot_index >= len(self.imu_plot_widgets):
            return
        imu_index = self.imu_plot_imu_selects[plot_index].currentData()
        channel_index = self.imu_plot_channel_selects[plot_index].currentData()
        if channel_index is None:
            return
        channel_label, _group, _offset, y_range = self.IMU_CHANNELS[int(channel_index)]
        plot = self.imu_plot_widgets[plot_index]
        plot.title_label.setText(f"IMU{imu_index} - {channel_label}")
        plot.plot_widget.setYRange(y_range[0], y_range[1])

    def _append_imu_sample_to_plots(self, sample: dict):
        sample_imu_index = int(sample.get("imu_index", 0))
        timestamp = float(sample.get("timestamp", time.time()))
        for plot_index, plot in enumerate(self.imu_plot_widgets):
            selected_imu = self.imu_plot_imu_selects[plot_index].currentData()
            if int(selected_imu or 0) != sample_imu_index:
                continue

            channel_index = self.imu_plot_channel_selects[plot_index].currentData()
            if channel_index is None:
                continue
            _label, group, offset, _y_range = self.IMU_CHANNELS[int(channel_index)]
            values = sample.get(group, ["", "", ""])
            try:
                value = float(values[offset])
            except (TypeError, ValueError, IndexError):
                continue
            plot.add_batch([timestamp], [value])

    def setup_cameras(self):
        if self.cameras_initialized:
            return
        self.refresh_camera_devices()
        self.cameras = []
        self.capture_sessions = []
        self.recorders = []
        for idx in range(self.USB_CAMERA_COUNT):
            session = QMediaCaptureSession(self)
            recorder = QMediaRecorder(self)
            recorder.recorderStateChanged.connect(
                lambda state, camera_index=idx + 1: self._on_usb_recorder_state_changed(camera_index, state)
            )
            session.setRecorder(recorder)
            self.capture_sessions.append(session)
            self.recorders.append(recorder)

            device = self._selected_usb_camera_device(idx)
            if device is None:
                self.cameras.append(None)
                if self._usb_camera_channel_skipped(idx):
                    self.camera_labels[idx].setText("不使用")
                    self.camera_status_labels[idx].setText("状态: skipped")
                    self.camera_status_labels[idx].setStyleSheet("color: #666;")
                else:
                    self.camera_labels[idx].setText("未检测到设备")
                    self.camera_status_labels[idx].setText("状态: disconnected")
                    self.camera_status_labels[idx].setStyleSheet("color: #b00020;")
                continue

            camera = QCamera(device, self)
            self._set_1080p_format(camera, device)
            session.setCamera(camera)
            session.setVideoOutput(self.video_widgets[idx])
            recorder.setQuality(QMediaRecorder.Quality.HighQuality)
            media_format = QMediaFormat()
            media_format.setFileFormat(QMediaFormat.FileFormat.MPEG4)
            recorder.setMediaFormat(media_format)
            camera.start()

            self.cameras.append(camera)
            self.camera_labels[idx].setText(device.description())
            self.camera_status_labels[idx].setText("状态: connected")
            self.camera_status_labels[idx].setStyleSheet("color: #138a36;")

        for idx in range(len(self.cameras), self.USB_CAMERA_COUNT):
            self.cameras.append(None)
            self.recorders.append(None)
            self.capture_sessions.append(QMediaCaptureSession(self))
        self.cameras.extend([None] * len(self.D435I_WIDGET_INDICES))
        self.recorders.extend([None] * len(self.D435I_WIDGET_INDICES))
        self.cameras_initialized = True

    def refresh_camera_devices(self):
        previous_ids = [self._combo_device_id(combo) for combo in self.camera_selects]
        previous_texts = [combo.currentText().strip() for combo in self.camera_selects]
        self.available_usb_devices = [
            device for device in QMediaDevices.videoInputs()
            if not self._is_realsense_qt_camera(device.description())
        ]

        for idx, combo in enumerate(self.camera_selects):
            preferred_id = previous_ids[idx] if idx < len(previous_ids) else ""
            explicit_skip = idx < len(previous_texts) and previous_texts[idx] == "不使用"
            if not preferred_id and not explicit_skip and idx < len(self.available_usb_devices):
                preferred_id = self._camera_device_id(self.available_usb_devices[idx])

            combo.blockSignals(True)
            combo.clear()
            if not self.available_usb_devices:
                combo.addItem("未检测到 USB Camera", "")
                combo.setEnabled(False)
                combo.setProperty("explicit_skip", False)
            else:
                combo.setEnabled(not self.recording)
                combo.addItem("不使用", "")
                for device in self.available_usb_devices:
                    combo.addItem(self._camera_device_label(device), self._camera_device_id(device))
                if preferred_id and combo.findData(preferred_id) < 0:
                    combo.addItem(f"已选择设备未检测到 ({preferred_id})", preferred_id)
                selected_index = combo.findData(preferred_id)
                combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
                combo.setProperty(
                    "explicit_skip",
                    not self._combo_device_id(combo) and combo.currentText().strip() == "不使用",
                )
            combo.blockSignals(False)

    def _on_usb_camera_selection_changed(self, camera_index: int):
        if self.recording:
            return
        if self.cameras_initialized:
            self._stop_usb_cameras()
            self.setup_cameras()

    def _selected_usb_camera_device(self, camera_index: int):
        if camera_index >= len(self.camera_selects):
            return None
        selected_id = self._combo_device_id(self.camera_selects[camera_index])
        if not selected_id:
            return None
        for device in self.available_usb_devices:
            if self._camera_device_id(device) == selected_id:
                return device
        return None

    def _usb_camera_channel_skipped(self, camera_index: int) -> bool:
        if camera_index >= len(self.camera_selects):
            return False
        combo = self.camera_selects[camera_index]
        return (
            not self._combo_device_id(combo)
            and combo.currentText().strip() == "不使用"
            and bool(combo.property("explicit_skip"))
        )

    def _enabled_usb_camera_count(self) -> int:
        count = 0
        for idx in range(self.USB_CAMERA_COUNT):
            camera = self.cameras[idx] if idx < len(self.cameras) else None
            if camera is not None:
                count += 1
            elif not self._usb_camera_channel_skipped(idx):
                count += 1
        return count

    def _combo_device_id(self, combo: QComboBox) -> str:
        value = combo.currentData()
        return str(value) if value is not None else ""

    def _camera_device_id(self, device) -> str:
        try:
            device_id = bytes(device.id()).decode("utf-8", errors="ignore")
        except Exception:
            device_id = ""
        return device_id or device.description()

    def _camera_device_label(self, device) -> str:
        device_id = self._camera_device_id(device)
        description = device.description()
        if device_id and device_id != description:
            return f"{description} ({device_id})"
        return description

    def _is_realsense_qt_camera(self, description: str) -> bool:
        text = description.lower()
        return any(key in text for key in ("realsense", "d435", "depth camera"))

    def _stop_usb_cameras(self):
        for idx, camera in enumerate(self.cameras[:self.USB_CAMERA_COUNT]):
            if camera:
                camera.stop()
            self.camera_labels[idx].setText("未连接")
            self.camera_status_labels[idx].setText("状态: disconnected")
            self.camera_status_labels[idx].setStyleSheet("color: #666;")
        self.cameras = []
        self.capture_sessions = []
        self.recorders = []
        self.cameras_initialized = False

    def _set_1080p_format(self, camera: QCamera, device):
        best_format = None
        best_score = None
        for fmt in device.videoFormats():
            size = fmt.resolution()
            score = abs(size.width() - 1920) + abs(size.height() - 1080)
            if best_score is None or score < best_score:
                best_format = fmt
                best_score = score
        if best_format is not None:
            camera.setCameraFormat(best_format)

    def choose_base_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择保存目录", self.base_dir_input.text())
        if directory:
            self.base_dir_input.setText(directory)

    def refresh_biosignal_connection(self):
        if self.recording:
            QMessageBox.warning(self, "重新监听 EMG/EEG", "当前正在采集，请先停止采集后再修改接收端口。")
            return
        self._update_biosignal_wifi_ip_label()
        port = (
            self.biosignal_port_spin.value()
            if self.biosignal_port_spin is not None
            else EmgEegUdpRecorder.DEFAULT_PORT
        )
        self.log_message(f"EMG/EEG 接收器已重新监听 UDP {port}。")
        self.biosignal_recorder.stop()
        self._biosignal_refresh_timer.start(150)

    def _restart_biosignal_listener(self):
        if self._shutting_down:
            return
        port = (
            self.biosignal_port_spin.value()
            if self.biosignal_port_spin is not None
            else EmgEegUdpRecorder.DEFAULT_PORT
        )
        self._update_biosignal_wifi_ip_label()
        self.biosignal_recorder.start(port)

    def refresh_imu_connection(self):
        if self.recording:
            QMessageBox.warning(
                self,
                "刷新 IMU 连接",
                "当前正在采集，请先停止采集后再刷新 IMU 连接。",
            )
            return

        ports = list(dict.fromkeys(spin.value() for spin in self.port_spins))
        self._imu_refresh_generation += 1
        refresh_generation = self._imu_refresh_generation
        self.log_message("IMU 连接已刷新，请观察状态是否变为 receiving。")
        self.imu_recorder.stop()
        self._reset_imu_table()
        QtCore.QTimer.singleShot(200, lambda: self.imu_recorder.start(ports))
        QtCore.QTimer.singleShot(5000, lambda: self._log_imu_refresh_diagnostics(refresh_generation))

    def _local_ipv4_addresses(self) -> List[str]:
        addresses = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith("127.") or ip in addresses:
                    continue
                addresses.append(ip)
        except OSError:
            pass
        return addresses

    def _wlan_status_summary(self) -> str:
        command = (
            "$profile = Get-NetConnectionProfile -InterfaceAlias 'WLAN' -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "$ip = Get-NetIPAddress -InterfaceAlias 'WLAN' -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.IPAddress -notlike '169.254*' } | Select-Object -First 1; "
            "$name = if ($profile) { $profile.Name } else { 'unknown' }; "
            "$category = if ($profile) { $profile.NetworkCategory } else { 'unknown' }; "
            "$addr = if ($ip) { $ip.IPAddress } else { 'unknown' }; "
            "Write-Output \"$name|$category|$addr\""
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
            )
        except Exception as exc:
            return f"WLAN diagnostic unavailable: {exc}"

        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            return "WLAN diagnostic unavailable."

        name, category, ip = (output.splitlines()[0].split("|") + ["", "", ""])[:3]
        return f"WLAN diagnostic: SSID={name}, category={category}, IPv4={ip}, IMU target should use {ip}:1399"

    def _current_wlan_ipv4(self) -> str:
        for adapter_name, ip in self._wifi_ipv4_candidates_from_ipconfig():
            if self._is_usable_ipv4(ip):
                return ip
        return "unknown"

    def _wifi_ipv4_candidates_from_ipconfig(self) -> List[Tuple[str, str]]:
        try:
            result = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                timeout=1.5,
            )
        except Exception:
            return []
        if result.returncode != 0:
            return []

        encodings = ["mbcs", "utf-8"] if os.name == "nt" else ["utf-8"]
        text = ""
        for encoding in encodings:
            try:
                text = result.stdout.decode(encoding, errors="ignore")
                if "IPv4" in text or "adapter" in text.lower() or "适配器" in text:
                    break
            except LookupError:
                continue

        candidates = []
        current_adapter = ""
        current_is_wifi = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.endswith(":") and ("adapter" in line.lower() or "适配器" in line):
                current_adapter = line[:-1].strip()
                current_is_wifi = self._is_wifi_adapter_label(current_adapter)
                continue
            if not current_adapter or not current_is_wifi or "IPv4" not in line:
                continue
            match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
            if match:
                candidates.append((current_adapter, match.group(1)))
        return candidates

    @staticmethod
    def _is_wifi_adapter_label(label: str) -> bool:
        text = label.lower()
        wifi_markers = ("wireless lan", "wi-fi", "wifi", "wlan", "802.11", "无线")
        return any(marker in text for marker in wifi_markers)

    @staticmethod
    def _is_usable_ipv4(ip: str) -> bool:
        try:
            parts = [int(part) for part in str(ip).split(".")]
        except ValueError:
            return False
        if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
            return False
        return not (
            ip.startswith("127.")
            or ip.startswith("169.254.")
            or ip == "0.0.0.0"
        )

    def _update_biosignal_wifi_ip_label(self):
        if self.biosignal_wifi_ip_label is None:
            return
        ip = self._current_wlan_ipv4()
        display_ip = ip if ip != "unknown" else "未检测到"
        self.biosignal_wifi_ip_label.setText(f"当前 Wi-Fi 主机 IP 是：{display_ip}")
        color = "#138a36" if ip != "unknown" else "#b00020"
        self.biosignal_wifi_ip_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _log_imu_refresh_diagnostics(self, refresh_generation: Optional[int] = None):
        if refresh_generation is not None and refresh_generation != self._imu_refresh_generation:
            return
        if self.recording:
            return
        online = self.imu_recorder.online_imu_indices()
        if online:
            self.log_message("IMU 在线：" + "、".join(f"IMU{i}" for i in online))
            return

        raw_packets = self.imu_recorder.raw_packet_total()
        unparsed_packets = self.imu_recorder.unparsed_packet_total()
        if raw_packets == 0:
            self.log_message(
                "WT IMU 诊断：监听器已启动，但未收到 UDP 数据。"
                "请确认 IMU 目标 IP 为当前 WLAN IPv4，目标端口为 1399。"
                "同时检查 FOG 网络类型和 Windows 防火墙入站规则。"
            )
        elif unparsed_packets:
            self.log_message(
                f"WT IMU 诊断：已收到 {raw_packets} 个 UDP 包，但未匹配配置的 IMU ID。"
                "请检查 WT IMU 设备 ID 和数据包格式。"
            )
        else:
            self.log_message("WT IMU 诊断：暂未发现在线 IMU，请等待数据或再次刷新。")

    def refresh_video_devices(self):
        if self.recording:
            QMessageBox.warning(self, "刷新视频设备", "当前正在采集，请先停止采集后再刷新视频设备。")
            return
        self.realsense.stop_preview()
        self._stop_usb_cameras()
        self.refresh_camera_devices()
        self.setup_cameras()
        self.realsense.start_preview()
        self.log_message("视频设备已刷新。")

    def refresh_usb_video_devices(self):
        if self.recording:
            QMessageBox.warning(self, "刷新 USB 视频设备", "当前正在采集，请先停止采集后再刷新 USB 视频设备。")
            return
        self._stop_usb_cameras()
        self.refresh_camera_devices()
        self.setup_cameras()
        self.log_message("USB 视频设备已刷新。")

    def refresh_d435i_video_devices(self):
        if self.recording:
            QMessageBox.warning(self, "刷新 D435i 视频设备", "当前正在采集，请先停止采集后再刷新 D435i 视频设备。")
            return
        self.realsense.stop_preview()
        self.realsense.start_preview()
        self.log_message("D435i 视频设备已刷新。")

    def _selected_command_imu_index(self) -> int:
        if self.imu_command_select is None:
            return 1
        value = self.imu_command_select.currentData()
        return int(value or 1)

    def _on_accelerometer_calibration_clicked(self):
        online_indices = self.imu_recorder.online_imu_indices()
        if not online_indices:
            QMessageBox.warning(self, "加速度计校准", "当前没有在线 IMU，请先确认 IMU 状态为 receiving。")
            return
        reply = QMessageBox.question(
            self,
            "加速度计校准",
            "请确认所有在线 IMU 已静止，并且模块正面水平放置。\n\n"
            f"将对以下 IMU 执行加速度计校准：{', '.join(f'IMU{i}' for i in online_indices)}\n\n继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.imu_recorder.calibrate_connected_accelerometers()

    def _on_angle_zero_clicked(self):
        online_indices = self.imu_recorder.online_imu_indices()
        if not online_indices:
            QMessageBox.warning(self, "角度置零", "当前没有在线 IMU，请先确认 IMU 状态为 receiving。")
            return
        self.imu_recorder.zero_connected_angles()

    def record_wearing_baseline(self):
        self._save_wearing_baseline(allow_recording=False, show_dialog=True, source="manual")

    def _save_wearing_baseline(
        self,
        allow_recording: bool = False,
        show_dialog: bool = True,
        source: str = "manual",
    ) -> Optional[str]:
        if self.recording and not allow_recording:
            message = "当前正在采集，请先停止采集后再记录佩戴基线。"
            if show_dialog:
                QMessageBox.warning(self, "记录佩戴基线", message)
            else:
                self.log_message(f"佩戴基线未保存: {message}")
            return None

        online_indices = set(self.imu_recorder.online_imu_indices())
        recorder_samples = self.imu_recorder.latest_samples()
        samples = [
            recorder_samples.get(index, self.latest_imu_samples.get(index, {}))
            for index in sorted(online_indices)
            if index in online_indices
        ]
        samples = [sample for sample in samples if sample]
        if not samples:
            message = "当前没有可用 IMU 姿态数据，请先确认 IMU 状态为 receiving。"
            if show_dialog:
                QMessageBox.warning(self, "记录佩戴基线", message)
            else:
                self.log_message(f"佩戴基线未保存: {message}")
            return None

        subject = self.name_input.text().strip() or "unknown"
        task_type = self.task_type_combo.currentText()
        safe_subject = self._safe_filename(subject)
        safe_task = self._safe_filename(task_type)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if source == "remote_experiment_start" and self.session_dir:
            baseline_root = self.session_dir
            filename_suffix = "experiment_start_wearing_baseline"
        else:
            baseline_root = self.base_dir_input.text()
            filename_suffix = "wearing_baseline"
        baseline_dir = os.path.join(baseline_root, "baselines")
        os.makedirs(baseline_dir, exist_ok=True)
        output_path = os.path.join(baseline_dir, f"{safe_subject}_{safe_task}_{timestamp}_{filename_suffix}.csv")

        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "recorded_at",
                "subject",
                "task_type",
                "imu_index",
                "device_id",
                "packet_count",
                "acc_x",
                "acc_y",
                "acc_z",
                "gyro_x",
                "gyro_y",
                "gyro_z",
                "angle_x",
                "angle_y",
                "angle_z",
                "mag_x",
                "mag_y",
                "mag_z",
                "temperature",
                "battery_percent",
                "rssi",
            ])
            recorded_at = datetime.now().isoformat(timespec="milliseconds")
            for sample in samples:
                acc = self._format_numeric_triplet(sample.get("acc_csv", sample.get("acc", ["", "", ""])))
                gyr = self._format_numeric_triplet(sample.get("gyr_csv", sample.get("gyr", ["", "", ""])))
                angle = self._format_numeric_triplet(sample.get("angle_csv", sample.get("angle", ["", "", ""])))
                mag = self._format_numeric_triplet(sample.get("gnt_csv", sample.get("gnt", ["", "", ""])))
                writer.writerow([
                    recorded_at,
                    subject,
                    task_type,
                    sample.get("imu_index", ""),
                    sample.get("device_id", ""),
                    sample.get("count", ""),
                    *acc,
                    *gyr,
                    *angle,
                    *mag,
                    self._format_numeric_value(sample.get("temperature_csv", sample.get("temperature", ""))),
                    sample.get("battery_percent", ""),
                    sample.get("rssi", ""),
                ])

        self.log_message(f"佩戴基线已保存：{len(samples)} 个 IMU")
        if show_dialog:
            QMessageBox.information(
                self,
                "记录佩戴基线",
                f"已保存 {len(samples)} 个 IMU 的佩戴基线：\n{os.path.basename(output_path)}",
            )
        return output_path

    @staticmethod
    def _safe_filename(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-", "掳") else "_" for ch in value.strip())
        return safe or "unknown"
    @staticmethod
    def _format_numeric_value(value, decimals: int = 3) -> str:
        if value == "" or value is None:
            return ""
        try:
            return f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    def _format_numeric_triplet(cls, values) -> List[str]:
        items = list(values) if isinstance(values, (list, tuple)) else []
        items = (items + ["", "", ""])[:3]
        return [cls._format_numeric_value(value) for value in items]

    def _recording_preflight_errors(self) -> List[str]:
        errors = []
        online_imus = set(self.imu_recorder.online_imu_indices())
        missing_imus = [f"IMU{i}" for i in range(1, 6) if i not in online_imus]
        if missing_imus:
            errors.append("IMU 未全部在线：" + "、".join(missing_imus))

        missing_cameras = []
        for idx in range(self.USB_CAMERA_COUNT):
            if self._usb_camera_channel_skipped(idx):
                continue
            camera = self.cameras[idx] if idx < len(self.cameras) else None
            if camera is None:
                missing_cameras.append(f"Camera{idx + 1}")
        if self._d435i_recording_enabled() and not self._realsense_ready():
            missing_cameras.append("D435i")
        if missing_cameras:
            errors.append("相机未全部在线：" + "、".join(missing_cameras))

        if not self.biosignal_recorder.running:
            errors.append("EMG/EEG UDP 接收器未启动")
        else:
            online_biosignals = self.biosignal_recorder.online_serials()
            biosignal_names = {
                "000001": "EMG1(000001)",
                "000002": "EMG2(000002)",
                "000003": "EEG(000003)",
            }
            missing_biosignals = [
                name for serial, name in biosignal_names.items() if serial not in online_biosignals
            ]
            if missing_biosignals:
                errors.append("EMG/EEG 未全部在线：" + "、".join(missing_biosignals))

            online_channels = self.biosignal_recorder.online_channel_indices()
            channel_names = [*(f"EMG CH{i}" for i in range(1, 9)), *(f"EEG CH{i}" for i in range(1, 4))]
            missing_channels = [
                channel_names[index] for index in range(11) if index not in online_channels
            ]
            if missing_channels:
                errors.append("EMG/EEG 通道无有效帧：" + "、".join(missing_channels))

        if not self._remote_ready():
            detail = self.remote_input_status or self.remote_pair_status or "unknown"
            errors.append(f"蓝牙遥控器未连接：{self.remote_device_name} ({detail})")
        return errors

    def _d435i_recording_enabled(self) -> bool:
        return bool(getattr(self, "enable_d435i_checkbox", None) is not None and self.enable_d435i_checkbox.isChecked())

    def _realsense_ready(self) -> bool:
        return bool(self.realsense.running and self.realsense.pipeline is not None)

    def _remote_ready(self) -> bool:
        return bool(self.remote_connected)

    def start_collection(self):
        if self.recording:
            return
        subject = self.name_input.text().strip()
        if not subject:
            QMessageBox.warning(self, "缺少信息", "请先填写被试姓名。")
            return

        ports = list(dict.fromkeys(spin.value() for spin in self.port_spins))
        if len(set(ports)) != len(ports):
            QMessageBox.warning(self, "端口冲突", "5 路 IMU 端口不能重复。")
            return

        self._start_capture_devices()
        preflight_errors = self._recording_preflight_errors()
        if preflight_errors:
            message = "采集未开始，请先确认设备在线：\n" + "\n".join(preflight_errors)
            QMessageBox.warning(self, "采集预检", message)
            self.log_message("采集未开始：设备预检未通过。")
            return

        safe_subject = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in subject)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.session_dir = os.path.join(self.base_dir_input.text(), f"{safe_subject}_{timestamp}")
        self.session_start_ts = time.time()
        startup_events = []
        try:
            os.makedirs(self.session_dir, exist_ok=False)
            self._reset_imu_table()
            imu_path = os.path.join(self.session_dir, "imu.csv")
            self._write_session_metadata(subject, ports, self.session_start_ts)
            self._start_remote_fog_labeling()
            self._append_or_queue_session_event(startup_events, "recording_zero", self.session_start_ts)
            self._append_or_queue_sync_event(startup_events, "system", "recording_zero", self.session_start_ts)
            self._append_or_queue_session_event(startup_events, "start_clicked", self.session_start_ts)
        except Exception as exc:
            self._rollback_failed_collection_start(f"会话文件初始化失败：{exc}")
            return
        try:
            biosignal_request_ts = time.time()
            self.biosignal_recorder.start_recording(
                self.session_dir,
                session_start_ts=self.session_start_ts,
            )
            biosignal_started_ts = time.time()
            self._append_or_queue_session_event(startup_events, "emg_eeg_record_started", biosignal_started_ts)
            for device, filename, channels in (
                ("EMG 000001", "emg.csv", "1-4"),
                ("EMG 000002", "emg.csv", "5-8"),
                ("EEG 000003", "eeg.csv", "1-3"),
            ):
                detail = f"port={self.biosignal_recorder.port};file={filename};channels={channels}"
                self._append_or_queue_sync_event(
                    startup_events,
                    device,
                    "record_start_requested",
                    biosignal_request_ts,
                    detail,
                )
                self._append_or_queue_sync_event(
                    startup_events,
                    device,
                    "record_start_completed",
                    biosignal_started_ts,
                    detail,
                )
        except Exception as exc:
            self._rollback_failed_collection_start(f"EMG/EEG 文件启动失败：{exc}")
            return
        try:
            imu_request_ts = time.time()
            self.imu_recorder.start(ports, imu_path, session_start_ts=self.session_start_ts)
            imu_started_ts = time.time()
            self._append_or_queue_session_event(startup_events, "wt_imu_record_requested", imu_request_ts)
            self._append_or_queue_sync_event(
                startup_events,
                "wt_imu",
                "record_start_requested",
                imu_request_ts,
                f"ports={sorted(set(ports))}",
            )
            self._append_or_queue_sync_event(
                startup_events,
                "wt_imu",
                "record_start_completed",
                imu_started_ts,
                f"ports={sorted(set(ports))}",
            )
            self._start_camera_recording(event_sink=startup_events)
            if self._d435i_recording_enabled():
                d435i_dir = os.path.join(self.session_dir, "D435i")
                d435i_request_ts = time.time()
                self.realsense.start_recording(
                    d435i_dir,
                    subject=subject,
                    imu_ports=ports,
                    session_start_ts=self.session_start_ts,
                    save_depth_raw=True,
                )
                if not self.realsense.recording:
                    raise RuntimeError("D435i 录像线程未进入记录状态")
                d435i_started_ts = time.time()
                self._append_or_queue_session_event(startup_events, "d435i_record_requested", d435i_request_ts)
                self._append_or_queue_sync_event(startup_events, "d435i", "record_start_requested", d435i_request_ts)
                self._append_or_queue_sync_event(startup_events, "d435i", "record_start_completed", d435i_started_ts)
            else:
                d435i_skip_ts = time.time()
                self._append_or_queue_session_event(startup_events, "d435i_record_disabled", d435i_skip_ts)
                self._append_or_queue_sync_event(startup_events, "d435i", "record_disabled", d435i_skip_ts)
            if not self.biosignal_recorder.recording:
                raise RuntimeError("EMG/EEG 写线程在启动阶段提前停止")
            self._flush_queued_events(startup_events)
        except Exception as exc:
            self._rollback_failed_collection_start(str(exc))
            return
        for spin in self.port_spins:
            spin.setEnabled(False)

        self.recording = True
        self._update_remote_fog_label()
        self._update_remote_experiment_label()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.enable_d435i_checkbox.setEnabled(False)
        self.btn_record_baseline.setEnabled(False)
        if self.biosignal_port_spin is not None:
            self.biosignal_port_spin.setEnabled(False)
        if self.btn_refresh_biosignal is not None:
            self.btn_refresh_biosignal.setEnabled(False)
        for button in self.video_refresh_buttons:
            button.setEnabled(False)
        self.task_type_combo.setEnabled(False)
        for combo in self.camera_selects:
            combo.setEnabled(False)
        video_channel_count = self._enabled_usb_camera_count() + (len(self.D435I_WIDGET_INDICES) if self._d435i_recording_enabled() else 0)
        self.log_message(
            f"采集已开始：5 个 IMU、2 个 EMG、1 个 EEG 与 {video_channel_count} 路视频共用同一同步零点。"
        )
        self.log_message(f"会话：{os.path.basename(self.session_dir)}")
        self.log_message("等待遥控器双击标记实验开始。")

    def _rollback_failed_collection_start(self, detail: str):
        failure_ts = time.time()
        rollback_errors = []
        try:
            self._stop_camera_recording()
        except Exception as exc:
            rollback_errors.append(f"USB Camera: {exc}")
        try:
            self.realsense.stop_recording()
        except Exception as exc:
            rollback_errors.append(f"D435i: {exc}")
        try:
            self.imu_recorder.stop_recording()
        except Exception as exc:
            rollback_errors.append(f"WT IMU: {exc}")
        try:
            self.biosignal_recorder.stop_recording(timeout_s=0.05)
        except Exception as exc:
            rollback_errors.append(f"EMG/EEG: {exc}")

        # Failure logging is best-effort: a full or unavailable disk must not
        # prevent already-started devices from being stopped.
        try:
            self._append_session_event("record_start_failed", failure_ts)
        except Exception as exc:
            rollback_errors.append(f"启动失败事件: {exc}")
        try:
            self._append_sync_event("system", "record_start_failed", failure_ts, detail)
        except Exception as exc:
            rollback_errors.append(f"启动失败同步事件: {exc}")

        biosignal_pending = self.biosignal_recorder.writer_pending
        self.recording = False
        self.biosignal_finalizing = biosignal_pending
        self.btn_start.setEnabled(not biosignal_pending)
        self.btn_stop.setEnabled(False)
        if self.biosignal_port_spin is not None:
            self.biosignal_port_spin.setEnabled(not biosignal_pending)
        if self.btn_refresh_biosignal is not None:
            self.btn_refresh_biosignal.setEnabled(not biosignal_pending)
        if biosignal_pending:
            self._biosignal_finalize_timer.start()
        else:
            self.session_start_ts = None

        suffix = "；" + "；".join(rollback_errors) if rollback_errors else ""
        self.log_message(f"采集启动失败，已回滚已启动的数据流：{detail}{suffix}")
        QMessageBox.critical(
            self,
            "采集启动失败",
            f"已停止本轮已启动的数据流。\n\n{detail}" + (f"\n\n收尾提示：{'; '.join(rollback_errors)}" if rollback_errors else ""),
        )

    def stop_collection(self):
        if not self.recording:
            return

        stop_ts = time.time()
        stop_errors = []
        stop_events = []

        def append_session_best_effort(event: str, pc_ts: float):
            try:
                self._append_session_event(event, pc_ts)
            except Exception as exc:
                stop_errors.append(f"事件 {event}: {exc}")

        def append_sync_best_effort(device: str, event: str, pc_ts: float, detail: str = ""):
            try:
                self._append_sync_event(device, event, pc_ts, detail)
            except Exception as exc:
                stop_errors.append(f"同步事件 {device}/{event}: {exc}")

        def queue_session_event(event: str, pc_ts: float):
            self._append_or_queue_session_event(stop_events, event, pc_ts)

        def queue_sync_event(device: str, event: str, pc_ts: float, detail: str = ""):
            self._append_or_queue_sync_event(stop_events, device, event, pc_ts, detail)

        def flush_stop_events_best_effort():
            for item in list(stop_events):
                try:
                    self._flush_queued_events([item])
                except Exception as exc:
                    stop_errors.append(f"停止同步事件 {item}: {exc}")

        queue_session_event("stop_clicked", stop_ts)
        queue_sync_event("system", "record_stop_requested", stop_ts)

        # Request every stream to stop before doing slower label/file finalization.
        # This keeps end boundaries close to the operator's Stop click.
        try:
            biosignal_request_ts = time.time()
            self.biosignal_recorder.request_stop_recording()
            queue_session_event("emg_eeg_record_stop_requested", biosignal_request_ts)
            for device in ("EMG 000001", "EMG 000002", "EEG 000003"):
                queue_sync_event(device, "record_stop_requested", biosignal_request_ts)
        except Exception as exc:
            stop_errors.append(f"EMG/EEG 停止门：{exc}")

        try:
            self._stop_camera_recording(event_sink=stop_events)
        except Exception as exc:
            stop_errors.append(f"USB Camera: {exc}")

        if self._d435i_recording_enabled():
            d435i_request_ts = time.time()
            queue_session_event("d435i_record_stop_requested", d435i_request_ts)
            queue_sync_event("d435i", "record_stop_requested", d435i_request_ts)
            try:
                self.realsense.request_stop_recording()
            except Exception as exc:
                stop_errors.append(f"D435i 停止门：{exc}")

        imu_request_ts = time.time()
        queue_session_event("wt_imu_record_stop_requested", imu_request_ts)
        queue_sync_event("wt_imu", "record_stop_requested", imu_request_ts)
        try:
            self.imu_recorder.request_stop_recording()
        except Exception as exc:
            stop_errors.append(f"WT IMU 停止门：{exc}")

        flush_stop_events_best_effort()

        try:
            self._commit_pending_remote_single_click(stop_on_failure=False)
        except Exception as exc:
            stop_errors.append(f"蓝牙待处理标签：{exc}")
        if self.remote_fog_active:
            try:
                self._end_remote_fog_label(stop_ts, "", None, "fog_end_auto_stop")
            except Exception as exc:
                stop_errors.append(f"FOG 自动结束标签：{exc}")
        if self.remote_experiment_active:
            try:
                self._end_remote_experiment(stop_ts, "", None, "experiment_end_auto_stop")
            except Exception as exc:
                stop_errors.append(f"实验自动结束标签：{exc}")

        if self._d435i_recording_enabled():
            try:
                self.realsense.stop_recording()
                d435i_event = "record_stop_completed"
                d435i_detail = ""
            except Exception as exc:
                d435i_event = "record_stop_failed"
                d435i_detail = str(exc)
                stop_errors.append(f"D435i: {exc}")
            d435i_completed_ts = time.time()
            append_session_best_effort(f"d435i_{d435i_event}", d435i_completed_ts)
            append_sync_best_effort("d435i", d435i_event, d435i_completed_ts, d435i_detail)

        try:
            self.imu_recorder.stop_recording()
            imu_event = "record_stop_completed"
            imu_detail = ""
        except Exception as exc:
            imu_event = "record_stop_failed"
            imu_detail = str(exc)
            stop_errors.append(f"WT IMU: {exc}")
        imu_completed_ts = time.time()
        append_session_best_effort(f"wt_imu_{imu_event}", imu_completed_ts)
        append_sync_best_effort("wt_imu", imu_event, imu_completed_ts, imu_detail)

        biosignal_stop_exception = ""
        try:
            # Normal UI stop is asynchronous after a short grace period; the
            # finalize timer keeps Start disabled until every queued row closes.
            self.biosignal_recorder.stop_recording(timeout_s=0.05)
        except Exception as exc:
            biosignal_stop_exception = str(exc)
            stop_errors.append(f"EMG/EEG: {exc}")
        biosignal_write_error = self.biosignal_recorder.last_write_error
        biosignal_pending = self.biosignal_recorder.writer_pending
        biosignal_completed_ts = time.time()
        if biosignal_write_error or biosignal_stop_exception:
            biosignal_stop_event = "record_stop_failed"
        elif biosignal_pending:
            biosignal_stop_event = "record_stop_incomplete"
        else:
            biosignal_stop_event = "record_stop_completed"
        biosignal_error_detail = biosignal_write_error or biosignal_stop_exception
        if biosignal_write_error and not biosignal_stop_exception:
            stop_errors.append(f"EMG/EEG: {biosignal_write_error}")
        append_session_best_effort(f"emg_eeg_{biosignal_stop_event}", biosignal_completed_ts)
        for device in ("EMG 000001", "EMG 000002", "EEG 000003"):
            append_sync_best_effort(device, biosignal_stop_event, biosignal_completed_ts, biosignal_error_detail)

        self.recording = False
        self.biosignal_finalizing = biosignal_pending
        self._clear_pending_remote_single_click()
        self.remote_fog_active = False
        self.remote_fog_start_pc_ts = None
        self.remote_experiment_active = False
        self.remote_experiment_start_pc_ts = None
        if biosignal_pending:
            self._biosignal_finalize_timer.start()
        else:
            self._biosignal_finalize_timer.stop()
            self.session_start_ts = None

        self._update_remote_fog_label()
        self._update_remote_experiment_label()
        self.btn_start.setEnabled(not biosignal_pending)
        self.btn_stop.setEnabled(False)
        self.enable_d435i_checkbox.setEnabled(True)
        self.btn_record_baseline.setEnabled(True)
        if self.biosignal_port_spin is not None:
            self.biosignal_port_spin.setEnabled(not biosignal_pending)
        if self.btn_refresh_biosignal is not None:
            self.btn_refresh_biosignal.setEnabled(not biosignal_pending)
        for button in self.video_refresh_buttons:
            button.setEnabled(True)
        self.task_type_combo.setEnabled(True)
        for spin in self.port_spins:
            spin.setEnabled(True)
        for combo in self.camera_selects:
            combo.setEnabled(True)
        try:
            self.refresh_camera_devices()
        except Exception as exc:
            stop_errors.append(f"相机列表刷新：{exc}")

        if stop_errors:
            self.log_message("采集已停止，但部分设备收尾失败：" + "；".join(stop_errors))
        elif biosignal_pending:
            self.log_message("其他设备已停止；EMG/EEG 仍在落盘，完成前不能开始下一轮采集。")
        else:
            self.log_message("采集已停止，数据写入已完成。")

    def _poll_biosignal_finalize(self):
        if self.biosignal_recorder.writer_pending:
            return
        self._biosignal_finalize_timer.stop()
        finalize_errors = []
        try:
            completed = self.biosignal_recorder.stop_recording()
        except Exception as exc:
            completed = False
            finalize_errors.append(str(exc))
        finalize_ts = time.time()
        event = "record_finalize_completed" if completed else "record_finalize_failed"
        detail = self.biosignal_recorder.last_write_error or "；".join(finalize_errors)
        try:
            self._append_session_event(f"emg_eeg_{event}", finalize_ts)
        except Exception as exc:
            finalize_errors.append(f"finalize event: {exc}")
        for device in ("EMG 000001", "EMG 000002", "EEG 000003"):
            try:
                self._append_sync_event(device, event, finalize_ts, detail)
            except Exception as exc:
                finalize_errors.append(f"{device} finalize event: {exc}")
        self.biosignal_finalizing = False
        self.session_start_ts = None
        if not self.recording and not self._shutting_down:
            self.btn_start.setEnabled(True)
            if self.biosignal_port_spin is not None:
                self.biosignal_port_spin.setEnabled(True)
            if self.btn_refresh_biosignal is not None:
                self.btn_refresh_biosignal.setEnabled(True)
        if completed:
            self.log_message("EMG/EEG 队列落盘已完成，可以开始下一轮采集。")
        else:
            self.log_message(f"EMG/EEG 队列落盘失败：{detail}")
        if finalize_errors:
            self.log_message("EMG/EEG 收尾事件写入失败：" + "；".join(finalize_errors))

    def _write_session_metadata(self, subject: str, ports: List[int], session_start_ts: float):
        usb_cameras = []
        for idx in range(self.USB_CAMERA_COUNT):
            camera = self.cameras[idx] if idx < len(self.cameras) else None
            label = self.camera_labels[idx].text() if idx < len(self.camera_labels) else ""
            usb_cameras.append({
                "index": idx + 1,
                "enabled": not self._usb_camera_channel_skipped(idx),
                "connected": camera is not None,
                "label": label,
                "file": f"camera{idx + 1}.mp4" if camera is not None else None,
            })
        d435i_enabled = self._d435i_recording_enabled()
        metadata = {
            "subject": subject,
            "task_type": self.task_type_combo.currentText(),
            "session_start_pc_timestamp": f"{session_start_ts:.6f}",
            "timestamp_zero": "Use sync_timestamp as the canonical session-relative time axis.",
            "timebase": {
                "canonical_column": "sync_timestamp",
                "unit": "seconds",
                "zero": "session_start_pc_timestamp",
                "source": "Shared PC session zero; each device stream reconstructs its own sample positions from its native timing rules.",
                "expected_wt_sample_rate_hz": WtMultiImuUdpRecorder.EXPECTED_SAMPLE_RATE_HZ,
                "expected_wt_sample_period_ms": WtMultiImuUdpRecorder.EXPECTED_SAMPLE_PERIOD_MS,
                "note": "WT IMU uses its device clock. EMG/EEG reconstructs sample time from PC packet receipt and the 1000 Hz interval, and re-anchors after long receive gaps so disconnect/reconnect periods remain visible in sync_timestamp. EMG packet serials are written to emg.csv to distinguish devices 000001 and 000002; EEG packet serials and device timestamps remain internal diagnostics.",
            },
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "imu": {
                "file": "imu.csv",
                "ports": ports,
                "device_ids": WtMultiImuUdpRecorder.DEVICE_IDS,
                "time_columns": {
                    "world_time": "Local wall-clock time formatted as HH:MM:SS:mmm, derived from session_start_pc_timestamp + sync_timestamp.",
                    "sync_timestamp": "Canonical acquisition timestamp reconstructed from WT device clock and theoretical sample period.",
                    "sample_counter": "Theoretical per-IMU sampling position; it jumps when samples are missing.",
                    "dropped_samples_since_previous": "Estimated missing samples between the previous received sample and this row.",
                    "cumulative_dropped_samples": "Estimated cumulative missing samples for this IMU during the recording.",
                },
            },
            "biosignals": {
                "transport": "UDP",
                "data_port": self.biosignal_recorder.port,
                "device_info_port": self.biosignal_recorder.port + 1,
                "configuration_broadcast_port": EmgEegUdpRecorder.BROADCAST_PORT,
                "sample_rate_hz": 1000,
                "emg": {
                    "file": "emg.csv",
                    "frame_header": "AA AA",
                    "serial_channels": {
                        "000001": [1, 2, 3, 4],
                        "000002": [5, 6, 7, 8],
                    },
                },
                "eeg": {
                    "file": "eeg.csv",
                    "frame_header": "AD AD",
                    "serial_channels": {"000003": [1, 2, 3]},
                },
                "packet_format": {
                    "packet_length_bytes": 47,
                    "serial_offset": [2, 4],
                    "data_length_offset": [6, 7],
                    "sample_data_offset": 8,
                    "sample_format": "signed 24-bit big-endian; divide by 1000, then divide by 1.5 when abs(value) <= 2",
                    "device_timestamp_offset": [38, 45],
                    "crc_offset": 46,
                    "crc": "CRC-8, polynomial 0xD5, initial value 0",
                },
                "time_columns": {
                    "world_time": "Local wall-clock time formatted as HH:MM:SS:mmm.",
                    "sync_timestamp": "PC-reconstructed sample time relative to session_start_pc_timestamp; long receive gaps are preserved by re-anchoring to the reconnect packet time.",
                },
                "emg_csv_columns": ["world_time", "sync_timestamp", "packet_serial_number", "channel", "value_uV"],
                "eeg_csv_columns": ["world_time", "sync_timestamp", "channel", "value_uV"],
            },
            "usb_cameras": usb_cameras,
            "d435i": {
                "enabled": d435i_enabled,
                "directory": "D435i" if d435i_enabled else None,
                "files": ["RGB.mp4", "Stereo.mp4", "frames.csv", "metadata.json"] if d435i_enabled else [],
                "save_depth_raw": True if d435i_enabled else False,
            },
            "remote_fog_labels": {
                "events_file": "remote_fog_events.csv",
                "intervals_file": "remote_fog_intervals.csv",
                "timestamp_zero": "sync_timestamp uses session_start_pc_timestamp.",
                "single_click": "FOG start/end after the 1.0s double-click window expires.",
                "double_click_events": ["experiment_start", "experiment_end", "experiment_end_auto_stop"],
                "double_click_window_s": self.REMOTE_DOUBLE_CLICK_WINDOW_MS / 1000.0,
            },
            "sync": {
                "file": "session_sync.csv",
                "timestamp_zero": "sync_timestamp uses session_start_pc_timestamp.",
                "recording_boundary_policy": (
                    "Start requests are issued to biosignal, WT IMU, USB cameras, and D435i "
                    "before queued start events are flushed. Stop requests close each stream's "
                    "recording gate before slower label finalization and writer joins."
                ),
                "required_devices": [
                    *(
                        f"camera{idx + 1}"
                        for idx in range(self.USB_CAMERA_COUNT)
                        if not self._usb_camera_channel_skipped(idx)
                    ),
                    *([] if not d435i_enabled else ["d435i"]),
                    "WT IMU 1-5",
                    "EMG 000001",
                    "EMG 000002",
                    "EEG 000003",
                    "bluetooth_remote",
                ],
            },
        }
        with open(os.path.join(self.session_dir, "session_metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _append_session_event(self, event: str, pc_ts: float):
        if not self.session_dir:
            return
        path = os.path.join(self.session_dir, "session_events.csv")
        exists = os.path.exists(path)
        start_ts = self.session_start_ts if self.session_start_ts is not None else pc_ts
        sync_ts = pc_ts - start_ts
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["event", "world_time", "sync_timestamp"])
            writer.writerow([event, self._format_event_world_time(pc_ts), f"{sync_ts:.6f}"])

    def _append_sync_event(self, device: str, event: str, pc_ts: float, detail: str = ""):
        if not self.session_dir:
            return
        path = os.path.join(self.session_dir, "session_sync.csv")
        exists = os.path.exists(path)
        start_ts = self.session_start_ts if self.session_start_ts is not None else pc_ts
        sync_ts = pc_ts - start_ts
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["device", "event", "world_time", "sync_timestamp", "detail"])
            writer.writerow([device, event, self._format_event_world_time(pc_ts), f"{sync_ts:.6f}", detail])

    def _append_or_queue_session_event(self, event_sink, event: str, pc_ts: float):
        if event_sink is None:
            self._append_session_event(event, pc_ts)
        else:
            event_sink.append(("session", event, pc_ts))

    def _append_or_queue_sync_event(self, event_sink, device: str, event: str, pc_ts: float, detail: str = ""):
        if event_sink is None:
            self._append_sync_event(device, event, pc_ts, detail)
        else:
            event_sink.append(("sync", device, event, pc_ts, detail))

    def _flush_queued_events(self, event_sink):
        for item in event_sink:
            if not item:
                continue
            if item[0] == "session":
                _, event, pc_ts = item
                self._append_session_event(event, pc_ts)
            elif item[0] == "sync":
                _, device, event, pc_ts, detail = item
                self._append_sync_event(device, event, pc_ts, detail)

    def _start_camera_recording(self, event_sink=None):
        for idx, recorder in enumerate(self.recorders[:self.USB_CAMERA_COUNT], start=1):
            if recorder is None or self.cameras[idx - 1] is None:
                continue
            output_path = os.path.join(self.session_dir, f"camera{idx}.mp4")
            recorder.setOutputLocation(QUrl.fromLocalFile(output_path))
            request_ts = time.time()
            recorder.record()
            self._append_or_queue_session_event(event_sink, f"camera{idx}_record_requested", request_ts)
            self._append_or_queue_sync_event(
                event_sink,
                f"camera{idx}",
                "record_start_requested",
                request_ts,
                f"file=camera{idx}.mp4",
            )

    def _stop_camera_recording(self, event_sink=None):
        errors = []
        for idx, recorder in enumerate(self.recorders[:self.USB_CAMERA_COUNT], start=1):
            if recorder is not None and recorder.recorderState() != QMediaRecorder.RecorderState.StoppedState:
                request_ts = time.time()
                try:
                    recorder.stop()
                except Exception as exc:
                    errors.append(f"camera{idx} stop: {exc}")
                    continue
                try:
                    self._append_or_queue_session_event(event_sink, f"camera{idx}_stop_requested", request_ts)
                except Exception as exc:
                    errors.append(f"camera{idx} event: {exc}")
                try:
                    self._append_or_queue_sync_event(event_sink, f"camera{idx}", "record_stop_requested", request_ts)
                except Exception as exc:
                    errors.append(f"camera{idx} sync: {exc}")
        if errors:
            raise RuntimeError("；".join(errors))

    def _on_usb_recorder_state_changed(self, camera_index: int, state):
        if not self.session_dir or self.session_start_ts is None:
            return
        pc_ts = time.time()
        try:
            if state == QMediaRecorder.RecorderState.RecordingState:
                self._append_session_event(f"camera{camera_index}_recording_started", pc_ts)
                self._append_sync_event(f"camera{camera_index}", "recording_started", pc_ts)
            elif state == QMediaRecorder.RecorderState.StoppedState:
                self._append_session_event(f"camera{camera_index}_recording_stopped", pc_ts)
                self._append_sync_event(f"camera{camera_index}", "recording_stopped", pc_ts)
        except Exception as exc:
            self.log_message(f"Camera{camera_index} 状态事件写入失败：{exc}")

    def _reset_imu_table(self):
        if self.imu_table is None:
            return
        for row in range(5):
            for col in range(1, 16):
                item = self.imu_table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.imu_table.setItem(row, col, item)
                item.setText("idle" if col == 1 else "0" if col == 2 else "")
                if col == 1:
                    item.setForeground(QtGui.QColor("#666666"))

    def _on_imu_status(self, imu_index: int, status: str, count: int):
        if self.imu_table is None:
            return
        row = imu_index - 1
        status_item = self.imu_table.item(row, 1)
        count_item = self.imu_table.item(row, 2)
        if status_item is None or count_item is None:
            return
        status_item.setText(status)
        count_item.setText(str(count))
        if status == "receiving":
            status_item.setForeground(QtGui.QColor("#138a36"))
        elif status.startswith("listening"):
            status_item.setForeground(QtGui.QColor("#a66a00"))
        else:
            status_item.setForeground(QtGui.QColor("#666666"))

    def _on_biosignal_batch(self, payload: dict):
        global_channel_index = int(payload.get("global_channel_index", -1))
        if payload.get("modality") == "emg" and self.emg_panel is not None:
            self.emg_panel.add_batch(global_channel_index, payload)
        elif payload.get("modality") == "eeg" and self.eeg_panel is not None:
            self.eeg_panel.add_batch(global_channel_index - 8, payload)

    def _on_biosignal_device_status(self, serial: str, online: bool, count: int, source: str):
        if self.emg_panel is not None:
            self.emg_panel.update_device_status(serial, online, count, source)
        if self.eeg_panel is not None:
            self.eeg_panel.update_device_status(serial, online, count, source)

        label = self.biosignal_status_labels.get(serial)
        if label is None:
            return
        device_name = {"000001": "EMG 1", "000002": "EMG 2", "000003": "EEG"}.get(serial, serial)
        if online:
            detail = f" · {source}" if source else ""
            label.setText(f"● {device_name} · {serial} · 在线 · {count} 帧{detail}")
            label.setStyleSheet("color: #138A36;")
        else:
            label.setText(f"● {device_name} · {serial} · 等待数据")
            label.setStyleSheet("color: #777;")

    def _on_biosignal_listener_status(self, status: str):
        if self.biosignal_listener_label is None:
            return
        self._update_biosignal_wifi_ip_label()
        if status.startswith("listening:"):
            port = status.split(":", 1)[1]
            if self.emg_panel is not None:
                self.emg_panel.clear()
            if self.eeg_panel is not None:
                self.eeg_panel.clear()
            self.biosignal_listener_label.setText(f"接收器：正在监听 UDP {port} / 设备信息 {int(port) + 1}")
            self.biosignal_listener_label.setStyleSheet("color: #A66A00;")
        elif status == "error":
            self.biosignal_listener_label.setText("接收器：启动失败")
            self.biosignal_listener_label.setStyleSheet("color: #B00020;")
        else:
            self.biosignal_listener_label.setText("接收器：已停止")
            self.biosignal_listener_label.setStyleSheet("color: #777;")

    def _on_biosignal_recording_failed(self, message: str):
        if not self.recording:
            return
        failure_ts = time.time()
        event_errors = []
        try:
            self._append_session_event("emg_eeg_recording_failed", failure_ts)
        except Exception as exc:
            event_errors.append(str(exc))
        for device in ("EMG 000001", "EMG 000002", "EEG 000003"):
            try:
                self._append_sync_event(device, "recording_failed", failure_ts, message)
            except Exception as exc:
                event_errors.append(f"{device}: {exc}")
        try:
            self.stop_collection()
        finally:
            if event_errors:
                self.log_message("EMG/EEG 失败事件无法写入：" + "；".join(event_errors))
        QMessageBox.critical(self, "EMG/EEG 写入失败", f"采集已停止，以避免继续产生不完整数据。\n\n{message}")

    def _on_imu_sample(self, sample: dict):
        try:
            self.latest_imu_samples[int(sample["imu_index"])] = dict(sample)
        except Exception:
            pass
        if self.imu_table is None:
            return
        self._append_imu_sample_to_plots(sample)
        row = int(sample["imu_index"]) - 1
        values = [
            *sample.get("acc", ["", "", ""]),
            *sample.get("gyr", ["", "", ""]),
            *sample.get("angle", ["", "", ""]),
            *sample.get("gnt", ["", "", ""]),
        ]
        for offset, value in enumerate(values, start=3):
            item = self.imu_table.item(row, offset)
            if item is None:
                item = QTableWidgetItem()
                self.imu_table.setItem(row, offset, item)
            item.setText(str(value))

        battery_item = self.imu_table.item(row, 15)
        if battery_item is None:
            battery_item = QTableWidgetItem()
            self.imu_table.setItem(row, 15, battery_item)
        battery = sample.get("battery_percent", "")
        battery_item.setText(f"{battery}%" if battery != "" else "")

    def _on_realsense_status(self, status: str, detail: str):
        color = "#138a36" if status == "connected" else "#b00020"
        for idx in self.D435I_WIDGET_INDICES:
            self.camera_labels[idx].setText(detail)
            self.camera_status_labels[idx].setText(f"状态: {status}")
            self.camera_status_labels[idx].setStyleSheet(f"color: {color};")

    def _update_image_widget(self, widget_index: int, frame):
        if widget_index >= len(self.video_widgets):
            return
        label = self.video_widgets[widget_index]
        if not isinstance(label, QLabel):
            return
        if frame is None:
            return

        image = self._frame_to_qimage(frame)
        if image.isNull():
            return
        pixmap = QtGui.QPixmap.fromImage(image)
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _frame_to_qimage(self, frame) -> QtGui.QImage:
        if frame.ndim == 2:
            h, w = frame.shape
            return QtGui.QImage(frame.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8).copy()

        h, w, channels = frame.shape
        if channels != 3:
            return QtGui.QImage()
        rgb = frame[:, :, ::-1].copy()
        return QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888).copy()

    def log_message(self, message: str):
        noisy_prefixes = (
            "Page14 ready",
            "Refreshing WT IMU UDP listener",
            "Local IPv4 candidates",
            "WLAN diagnostic:",
            "If port 1399 still fails",
            "Refreshing video devices",
            "Video devices refreshed",
            "D435i IMU enabled with",
            "D435i connected, waiting for frames",
        )
        if any(message.startswith(prefix) for prefix in noisy_prefixes):
            return
        message = self._compact_log_message(message)
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {message}")

    def _compact_log_message(self, message: str) -> str:
        compacted = str(message)
        paths = []
        if self.session_dir:
            paths.append(self.session_dir)
        if hasattr(self, "base_dir_input"):
            paths.append(self.base_dir_input.text().strip())
        paths.append(os.getcwd())
        for path in sorted({os.path.abspath(p) for p in paths if p}, key=len, reverse=True):
            label = os.path.basename(path.rstrip("\\/")) or "当前目录"
            variants = (path, path.replace("\\", "/"))
            for variant in variants:
                compacted = re.sub(
                    re.escape(variant) + r"(?:[\\/][^\s，。；;]*)*",
                    label,
                    compacted,
                )
        return compacted

    def closeEvent(self, event: QtGui.QCloseEvent):
        self.stop_collection()
        if not self.deactivate_page():
            QMessageBox.warning(
                self,
                "数据仍在保存",
                "EMG/EEG 数据仍在落盘。程序会继续完成保存，请稍后再次关闭。",
            )
            event.ignore()
            return
        self._remote_single_click_timer.stop()
        if self._remote_status_timer is not None:
            self._remote_status_timer.stop()
        if self._remote_status_process is not None:
            if self._remote_status_process.state() != QtCore.QProcess.ProcessState.NotRunning:
                self._remote_status_process.kill()
                self._remote_status_process.waitForFinished(1000)
            self._remote_status_process = None
        super().closeEvent(event)
