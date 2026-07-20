from __future__ import annotations

import csv
import math
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtNetwork
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget


@dataclass(frozen=True)
class PacketFormat:
    modality: str
    header: bytes
    timestamp_offset: int = 38
    crc_offset: int = 46
    packet_length: int = 47


@dataclass
class ParsedBiosignalBatch:
    modality: str
    sensor_index: int
    global_channel_index: int
    modality_channel: int
    local_channel: int
    expected_serial: str
    packet_serial: str
    source: str
    packet_receive_time_s: float
    values: np.ndarray
    sample_timestamps_us: np.ndarray
    device_timestamp: int
    rms_uv: float
    amplitude_uv: float


class EmgEegPacketParser:
    """Equivalent parser for the 1 kHz low-frequency EMG/EEG frame format."""

    EMG_SENSOR_COUNT = 2
    EMG_CHANNELS_PER_SENSOR = 4
    EEG_CHANNELS_PER_SENSOR = 3
    CHANNEL_COUNT = 11
    SAMPLE_RATE = 1000.0
    REANCHOR_GAP_S = 0.2
    EXPECTED_SERIALS = ("000001", "000002", "000003")
    PACKET_FORMATS = (
        PacketFormat("emg", b"\xAA\xAA"),
        PacketFormat("eeg", b"\xAD\xAD"),
    )

    def __init__(self):
        self.crc_table = self._build_crc_table()
        self._state_lock = threading.Lock()
        self._source_sensor_map: Dict[str, int] = {}
        self._last_sample_time_us = np.full(self.CHANNEL_COUNT, np.nan, dtype=np.float64)
        self._last_packet_receive_time_us = np.full(self.CHANNEL_COUNT, np.nan, dtype=np.float64)
        self._rms_buffers = np.zeros((self.CHANNEL_COUNT, int(self.SAMPLE_RATE)), dtype=np.float64)
        self._rms_positions = np.zeros(self.CHANNEL_COUNT, dtype=np.int32)
        self._rms_sums = np.zeros(self.CHANNEL_COUNT, dtype=np.float64)

    @staticmethod
    def _build_crc_table() -> bytes:
        table = bytearray(256)
        for index in range(256):
            current = index
            for _ in range(8):
                if current & 0x80:
                    current = (current << 1) ^ 0xD5
                else:
                    current <<= 1
            table[index] = current & 0xFF
        return bytes(table)

    def calculate_crc(self, data: bytes, length: Optional[int] = None) -> int:
        crc = 0
        view = data if length is None else data[:length]
        for value in view:
            crc = self.crc_table[crc ^ value]
        return crc

    def reset_session_timing(self):
        with self._state_lock:
            self._last_sample_time_us.fill(np.nan)
            self._last_packet_receive_time_us.fill(np.nan)

    def reset_listener_state(self):
        with self._state_lock:
            self._source_sensor_map.clear()
            self._last_sample_time_us.fill(np.nan)
            self._last_packet_receive_time_us.fill(np.nan)
            self._rms_buffers.fill(0.0)
            self._rms_positions.fill(0)
            self._rms_sums.fill(0.0)

    def reset_sources(self):
        with self._state_lock:
            self._source_sensor_map.clear()

    @staticmethod
    def packet_serial(frame: bytes) -> str:
        return "".join(f"{value:02X}" for value in frame[2:5])

    @staticmethod
    def source_key(source: object, frame: bytes) -> str:
        if isinstance(source, tuple) and source:
            return str(source[0])
        if source:
            return str(source).split(":", 1)[0]
        return "serial:" + EmgEegPacketParser.packet_serial(frame)

    def _serial_sensor_index(self, frame: bytes) -> int:
        serial = self.packet_serial(frame)
        if serial == self.EXPECTED_SERIALS[2]:
            return 2
        try:
            return self.EXPECTED_SERIALS.index(serial)
        except ValueError:
            return -1

    def _resolve_sensor_index(self, frame: bytes, source: object, allow_auto_assign: bool) -> int:
        serial_index = self._serial_sensor_index(frame)
        key = self.source_key(source, frame)

        # The requested integration has three fixed serials. Keep the source IP
        # only as diagnostic state; it must never override a valid packet serial
        # or let an unknown device occupy one of the eleven target channels.
        if serial_index >= 0:
            self._source_sensor_map[key] = serial_index
            return serial_index
        return -1

    @staticmethod
    def _sensor_channel_count(sensor_index: int) -> int:
        return 4 if sensor_index < 2 else 3

    @staticmethod
    def _sensor_first_channel(sensor_index: int) -> int:
        return sensor_index * 4 if sensor_index < 2 else 8

    def parse_frame(
        self,
        frame: bytes,
        source: object = None,
        receive_time_s: Optional[float] = None,
    ) -> Optional[ParsedBiosignalBatch]:
        if len(frame) != 47:
            return None
        packet_format = next((item for item in self.PACKET_FORMATS if frame.startswith(item.header)), None)
        if packet_format is None:
            return None
        if self.calculate_crc(frame, packet_format.crc_offset) != frame[packet_format.crc_offset]:
            return None

        data_length = (frame[6] << 8) | frame[7]
        if data_length <= 0 or data_length % 3 or data_length > packet_format.timestamp_offset - 8:
            return None

        with self._state_lock:
            sensor_index = self._resolve_sensor_index(frame, source, allow_auto_assign=True)
            if sensor_index < 0 or sensor_index >= len(self.EXPECTED_SERIALS):
                return None

            if packet_format.modality == "emg":
                local_channel = frame[5] & 0x07
            else:
                local_channel = (frame[5] & 0x07) + 1
            if not 1 <= local_channel <= self._sensor_channel_count(sensor_index):
                return None

            global_channel_index = self._sensor_first_channel(sensor_index) + local_channel - 1
            if packet_format.modality == "emg" and global_channel_index >= 8:
                return None
            if packet_format.modality == "eeg" and global_channel_index < 8:
                return None

            sample_count = data_length // 3
            values = np.empty(sample_count, dtype=np.float64)
            for sample_index in range(sample_count):
                offset = 8 + sample_index * 3
                raw = (frame[offset] << 16) | (frame[offset + 1] << 8) | frame[offset + 2]
                if raw & 0x800000:
                    raw -= 1 << 24
                value = raw / 1000.0
                if abs(value) <= 2.0:
                    value /= 1.5
                values[sample_index] = value

            packet_receive_time_s = time.time() if receive_time_s is None else float(receive_time_s)
            receive_time_us = packet_receive_time_s * 1_000_000.0
            sample_interval_us = 1_000_000.0 / self.SAMPLE_RATE
            last_sample_time = self._last_sample_time_us[global_channel_index]
            last_receive_time = self._last_packet_receive_time_us[global_channel_index]
            reanchor_gap_us = self.REANCHOR_GAP_S * 1_000_000.0
            should_reanchor = (
                math.isnan(float(last_sample_time))
                or (
                    not math.isnan(float(last_receive_time))
                    and receive_time_us - float(last_receive_time) > reanchor_gap_us
                )
            )
            if should_reanchor:
                first_sample_time = receive_time_us - (sample_count - 1) * sample_interval_us
            else:
                first_sample_time = float(last_sample_time) + sample_interval_us
            timestamps_us = first_sample_time + np.arange(sample_count, dtype=np.float64) * sample_interval_us
            self._last_sample_time_us[global_channel_index] = timestamps_us[-1]
            self._last_packet_receive_time_us[global_channel_index] = receive_time_us

            rms_buffer = self._rms_buffers[global_channel_index]
            rms_position = int(self._rms_positions[global_channel_index])
            rms_sum = float(self._rms_sums[global_channel_index])
            for value in values:
                old_value = rms_buffer[rms_position]
                rms_sum += float(value * value - old_value * old_value)
                rms_buffer[rms_position] = value
                rms_position = (rms_position + 1) % rms_buffer.size
            self._rms_positions[global_channel_index] = rms_position
            self._rms_sums[global_channel_index] = max(0.0, rms_sum)
            rms_uv = math.sqrt(max(0.0, rms_sum) / rms_buffer.size)

            expected_serial = self.EXPECTED_SERIALS[sensor_index]
            modality_channel = global_channel_index + 1 if global_channel_index < 8 else global_channel_index - 7
            return ParsedBiosignalBatch(
                modality=packet_format.modality,
                sensor_index=sensor_index,
                global_channel_index=global_channel_index,
                modality_channel=modality_channel,
                local_channel=local_channel,
                expected_serial=expected_serial,
                packet_serial=self.packet_serial(frame),
                source=self.source_key(source, frame),
                packet_receive_time_s=packet_receive_time_s,
                values=values,
                sample_timestamps_us=timestamps_us,
                device_timestamp=int.from_bytes(
                    frame[packet_format.timestamp_offset:packet_format.timestamp_offset + 8], "big"
                ),
                rms_uv=rms_uv,
                amplitude_uv=math.sqrt(2.0) * rms_uv,
            )


class EmgEegUdpRecorder(QtCore.QObject):
    sample_batch_received = pyqtSignal(object)
    device_status_changed = pyqtSignal(str, bool, int, str)
    error_occurred = pyqtSignal(str)
    recording_failed = pyqtSignal(str)
    listener_status_changed = pyqtSignal(str)

    DEFAULT_PORT = 30300
    BROADCAST_PORT = 30200
    ONLINE_TIMEOUT_S = 3.0
    UI_FLUSH_INTERVAL_MS = 50
    UI_MAX_PENDING_BATCHES_PER_CHANNEL = 100
    CSV_HEADERS = [
        "world_time",
        "sync_timestamp",
        "channel",
        "value_uV",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parser = EmgEegPacketParser()
        self._running = False
        self._port = self.DEFAULT_PORT
        self._data_socket: Optional[socket.socket] = None
        self._info_socket: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._device_lock = threading.Lock()
        self._device_state: Dict[int, dict] = {}
        self._channel_state: Dict[int, dict] = {}
        self._last_online_state: Dict[int, bool] = {}
        self._pending_lock = threading.Lock()
        self._pending_batches: Dict[int, List[ParsedBiosignalBatch]] = {}
        self._ingest_lock = threading.Lock()

        self._recording_lock = threading.Lock()
        self._recording = False
        self._session_start_ts: Optional[float] = None
        self._write_queue: Optional[queue.Queue] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_files: Dict[str, object] = {}
        self._writer_objects: Dict[str, csv.writer] = {}
        self._writer_stop_event = threading.Event()
        self._last_write_error = ""
        self._dropped_write_batches = 0

        self._ui_timer = QtCore.QTimer(self)
        self._ui_timer.timeout.connect(self._flush_ui_batches)
        self._ui_timer.start(self.UI_FLUSH_INTERVAL_MS)
        self._health_timer = QtCore.QTimer(self)
        self._health_timer.timeout.connect(self._emit_device_statuses)
        self._health_timer.start(1000)

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._running

    @property
    def recording(self) -> bool:
        with self._recording_lock:
            return self._recording

    @property
    def writer_pending(self) -> bool:
        with self._recording_lock:
            return bool(self._writer_thread is not None and self._writer_thread.is_alive())

    @property
    def last_write_error(self) -> str:
        with self._recording_lock:
            return self._last_write_error

    def online_serials(self) -> set[str]:
        if not self._running:
            return set()
        now = time.monotonic()
        with self._device_lock:
            return {
                self.parser.EXPECTED_SERIALS[index]
                for index, state in self._device_state.items()
                if now - state["last_seen"] <= self.ONLINE_TIMEOUT_S
                and state.get("packet_serial") == self.parser.EXPECTED_SERIALS[index]
            }

    def online_channel_indices(self) -> set[int]:
        if not self._running:
            return set()
        now = time.monotonic()
        with self._device_lock:
            return {
                channel_index
                for channel_index, state in self._channel_state.items()
                if now - state["last_seen"] <= self.ONLINE_TIMEOUT_S
                and state.get("packet_serial") == state.get("expected_serial")
            }

    def start(self, port: int = DEFAULT_PORT) -> bool:
        port = int(port)
        if self._running and self._port == port:
            return True
        if self._running:
            self.stop()

        data_socket = None
        info_socket = None
        try:
            data_socket = self._make_udp_listener(port)
            info_socket = self._make_udp_listener(port + 1)
        except OSError as exc:
            if data_socket is not None:
                data_socket.close()
            if info_socket is not None:
                info_socket.close()
            self.error_occurred.emit(
                f"EMG/EEG UDP 端口 {port} 启动失败：{exc}。请关闭独立 emg_program 后重试。"
            )
            with self._device_lock:
                self._device_state.clear()
                self._channel_state.clear()
                self._last_online_state.clear()
            self.listener_status_changed.emit("error")
            return False

        self._port = port
        self._data_socket = data_socket
        self._info_socket = info_socket
        self._stop_event.clear()
        self.parser.reset_listener_state()
        with self._device_lock:
            self._device_state.clear()
            self._channel_state.clear()
            self._last_online_state.clear()
        self._running = True
        self._threads = [
            threading.Thread(target=self._receive_data_loop, name="emg-eeg-udp", daemon=True),
            threading.Thread(target=self._receive_info_loop, name="emg-eeg-info", daemon=True),
            threading.Thread(target=self._broadcast_loop, name="emg-eeg-broadcast", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self.listener_status_changed.emit(f"listening:{port}")
        return True

    @staticmethod
    def _make_udp_listener(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.settimeout(0.5)
        sock.bind(("0.0.0.0", port))
        return sock

    def stop(self):
        self.stop_recording()
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        for sock in (self._data_socket, self._info_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._data_socket = None
        self._info_socket = None
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads.clear()
        self.listener_status_changed.emit("stopped")
        self._emit_device_statuses(force_offline=True)

    def start_recording(self, session_dir: str, session_start_ts: float) -> Dict[str, str]:
        self.stop_recording()
        if self.writer_pending:
            raise RuntimeError("上一轮 EMG/EEG 数据仍在写入，暂时不能开始新采集")
        paths = {
            "emg": os.path.join(session_dir, "emg.csv"),
            "eeg": os.path.join(session_dir, "eeg.csv"),
        }
        opened_files: Dict[str, object] = {}
        opened_writers: Dict[str, csv.writer] = {}
        try:
            for modality, path in paths.items():
                file_obj = open(path, "w", newline="", encoding="utf-8")
                opened_files[modality] = file_obj
                writer = csv.writer(file_obj)
                writer.writerow(self.CSV_HEADERS)
                opened_writers[modality] = writer
        except Exception:
            for file_obj in opened_files.values():
                file_obj.close()
            raise

        write_queue: queue.Queue = queue.Queue(maxsize=20000)
        session_start_ts = float(session_start_ts)
        self._writer_stop_event.clear()
        writer_thread = threading.Thread(
            target=self._write_loop,
            args=(write_queue, opened_files, opened_writers, session_start_ts),
            name="emg-eeg-writer",
            # Keep the process alive until queued experimental data is closed.
            daemon=False,
        )
        with self._ingest_lock:
            self.parser.reset_session_timing()
            with self._recording_lock:
                self._writer_files = opened_files
                self._writer_objects = opened_writers
                self._write_queue = write_queue
                self._session_start_ts = session_start_ts
                self._writer_thread = writer_thread
                self._dropped_write_batches = 0
                self._last_write_error = ""
                self._recording = True
            writer_thread.start()
        return paths

    def request_stop_recording(self) -> bool:
        with self._ingest_lock:
            with self._recording_lock:
                writer_thread = self._writer_thread
                if not self._recording and writer_thread is None:
                    return False
                self._recording = False
                write_queue = self._write_queue
                already_requested = self._writer_stop_event.is_set()
                self._writer_stop_event.set()
                if write_queue is not None and not already_requested:
                    try:
                        # The same locks protect the final data enqueue and the
                        # sentinel, so no valid batch can land behind it.
                        write_queue.put_nowait(None)
                    except queue.Full:
                        # The writer exits after draining the full queue because
                        # _writer_stop_event is set.
                        pass
        return writer_thread is not None

    def stop_recording(self, timeout_s: float = 30.0) -> bool:
        self.request_stop_recording()
        with self._recording_lock:
            writer_thread = self._writer_thread
        if writer_thread is None:
            return True
        if writer_thread is not None and writer_thread.is_alive():
            writer_thread.join(timeout=max(0.0, float(timeout_s)))
        if writer_thread.is_alive():
            return False
        with self._recording_lock:
            if self._writer_thread is writer_thread:
                self._writer_thread = None
                self._write_queue = None
                self._session_start_ts = None
                self._writer_files = {}
                self._writer_objects = {}
            write_error = self._last_write_error
        return not bool(write_error)

    def process_datagram(
        self,
        data: bytes,
        source: object = None,
        receive_time_s: Optional[float] = None,
    ) -> List[ParsedBiosignalBatch]:
        batches: List[ParsedBiosignalBatch] = []
        formats = {item.header: item for item in self.parser.PACKET_FORMATS}
        with self._ingest_lock:
            for offset in range(max(0, len(data) - 1)):
                packet_format = formats.get(data[offset:offset + 2])
                if packet_format is None or offset + packet_format.packet_length > len(data):
                    continue
                frame = data[offset:offset + packet_format.packet_length]
                batch = self.parser.parse_frame(frame, source=source, receive_time_s=receive_time_s)
                if batch is None:
                    continue
                batches.append(batch)
                self._accept_batch(batch)
        return batches

    def _receive_data_loop(self):
        while self._running and not self._stop_event.is_set():
            sock = self._data_socket
            if sock is None:
                break
            try:
                data, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.process_datagram(data, source=source, receive_time_s=time.time())
            except Exception as exc:
                self.error_occurred.emit(f"EMG/EEG 数据解析异常：{exc}")

    def _receive_info_loop(self):
        while self._running and not self._stop_event.is_set():
            sock = self._info_socket
            if sock is None:
                break
            try:
                data, source = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 8 or self.parser.calculate_crc(data, len(data) - 1) != data[-1]:
                continue
            # Device-info packets use the same serial bytes and source-IP mapping.
            try:
                with self.parser._state_lock:
                    sensor_index = self.parser._resolve_sensor_index(data, source, allow_auto_assign=True)
                if 0 <= sensor_index < len(self.parser.EXPECTED_SERIALS):
                    with self._device_lock:
                        state = self._device_state.setdefault(sensor_index, {})
                        state["info_source"] = str(source[0]) if isinstance(source, tuple) else str(source)
            except Exception:
                continue

    def _accept_batch(self, batch: ParsedBiosignalBatch):
        now = time.monotonic()
        with self._device_lock:
            state = self._device_state.setdefault(batch.sensor_index, {"count": 0})
            state["last_seen"] = now
            state["source"] = batch.source
            state["packet_serial"] = batch.packet_serial
            state["count"] = int(state.get("count", 0)) + 1
            self._channel_state[batch.global_channel_index] = {
                "last_seen": now,
                "packet_serial": batch.packet_serial,
                "expected_serial": batch.expected_serial,
            }

        with self._pending_lock:
            pending_batches = self._pending_batches.setdefault(batch.global_channel_index, [])
            pending_batches.append(batch)
            if len(pending_batches) > self.UI_MAX_PENDING_BATCHES_PER_CHANNEL:
                del pending_batches[:-self.UI_MAX_PENDING_BATCHES_PER_CHANNEL]

        fatal_queue_error = ""
        with self._recording_lock:
            if not self._recording or self._write_queue is None:
                return
            session_start_ts = self._session_start_ts
            if session_start_ts is None or batch.packet_receive_time_s < session_start_ts:
                return
            first_recorded_index = int(
                np.searchsorted(batch.sample_timestamps_us, session_start_ts * 1_000_000.0, side="left")
            )
            if first_recorded_index >= len(batch.values):
                return
            record_batch = batch
            if first_recorded_index:
                record_batch = replace(
                    batch,
                    values=batch.values[first_recorded_index:].copy(),
                    sample_timestamps_us=batch.sample_timestamps_us[first_recorded_index:].copy(),
                )
            try:
                self._write_queue.put_nowait(record_batch)
            except queue.Full:
                self._dropped_write_batches += 1
                if not self._last_write_error:
                    fatal_queue_error = (
                        f"EMG/EEG 磁盘写入队列已满，已丢弃 {self._dropped_write_batches} 个数据帧"
                    )
                    self._last_write_error = fatal_queue_error
                    self._recording = False
                    self._writer_stop_event.set()
        if fatal_queue_error:
            self.error_occurred.emit(fatal_queue_error)
            self.recording_failed.emit(fatal_queue_error)

    def _flush_ui_batches(self):
        with self._pending_lock:
            pending = self._pending_batches
            self._pending_batches = {}
        for channel_index, batches in pending.items():
            if not batches:
                continue
            latest = batches[-1]
            payload = {
                "modality": latest.modality,
                "global_channel_index": channel_index,
                "modality_channel": latest.modality_channel,
                "serial_number": latest.expected_serial,
                "packet_serial_number": latest.packet_serial,
                "source": latest.source,
                "timestamps": np.concatenate([batch.sample_timestamps_us for batch in batches]) / 1_000_000.0,
                "values": np.concatenate([batch.values for batch in batches]),
                "rms_uv": latest.rms_uv,
                "amplitude_uv": latest.amplitude_uv,
            }
            self.sample_batch_received.emit(payload)

    def _emit_device_statuses(self, force_offline: bool = False):
        now = time.monotonic()
        with self._device_lock:
            states = {index: dict(state) for index, state in self._device_state.items()}
        for sensor_index, serial in enumerate(self.parser.EXPECTED_SERIALS):
            state = states.get(sensor_index, {})
            online = bool(
                not force_offline
                and "last_seen" in state
                and now - float(state["last_seen"]) <= self.ONLINE_TIMEOUT_S
                and state.get("packet_serial") == serial
            )
            count = int(state.get("count", 0))
            source = str(state.get("source", ""))
            packet_serial = str(state.get("packet_serial", ""))
            if packet_serial and packet_serial != serial:
                source = f"{source}（包内序列号 {packet_serial} 不匹配）"
            self._last_online_state[sensor_index] = online
            self.device_status_changed.emit(serial, online, count, source)

    def _write_loop(
        self,
        write_queue: queue.Queue,
        writer_files: Dict[str, object],
        writer_objects: Dict[str, csv.writer],
        session_start_ts: float,
    ):
        pending_rows = 0
        last_flush = time.monotonic()
        failure_message = ""
        try:
            while True:
                try:
                    batch = write_queue.get(timeout=0.5)
                except queue.Empty:
                    batch = None
                    queue_drained = True
                else:
                    queue_drained = batch is None
                if batch is None:
                    if self._writer_stop_event.is_set() and (queue_drained or write_queue.empty()):
                        break
                if isinstance(batch, ParsedBiosignalBatch):
                    self._write_batch(batch, writer_objects, session_start_ts)
                    pending_rows += len(batch.values)
                now = time.monotonic()
                if pending_rows and (pending_rows >= 2000 or now - last_flush >= 1.0):
                    for file_obj in writer_files.values():
                        file_obj.flush()
                    pending_rows = 0
                    last_flush = now
        except Exception as exc:
            failure_message = f"EMG/EEG 数据写入失败：{exc}"
        finally:
            close_errors = []
            for file_obj in writer_files.values():
                try:
                    file_obj.flush()
                    file_obj.close()
                except Exception as exc:
                    close_errors.append(str(exc))
            if close_errors:
                close_message = "EMG/EEG 文件收尾失败：" + "; ".join(close_errors)
                failure_message = f"{failure_message}；{close_message}" if failure_message else close_message

            if failure_message:
                first_failure = False
                with self._recording_lock:
                    if self._write_queue is write_queue:
                        self._recording = False
                        if not self._last_write_error:
                            self._last_write_error = failure_message
                            first_failure = True
                        elif failure_message not in self._last_write_error:
                            self._last_write_error = f"{self._last_write_error}；{failure_message}"
                        self._writer_stop_event.set()
                self.error_occurred.emit(failure_message)
                if first_failure:
                    self.recording_failed.emit(failure_message)

    def _write_batch(
        self,
        batch: ParsedBiosignalBatch,
        writer_objects: Dict[str, csv.writer],
        session_start_ts: float,
    ):
        writer = writer_objects.get(batch.modality)
        if writer is None:
            return
        rows = []
        for timestamp_us, value in zip(batch.sample_timestamps_us, batch.values):
            timestamp_s = float(timestamp_us) / 1_000_000.0
            rows.append([
                self._format_world_time(timestamp_s),
                f"{timestamp_s - session_start_ts:.6f}",
                batch.modality_channel,
                format(float(value), ".12g"),
            ])
        writer.writerows(rows)

    @staticmethod
    def _format_world_time(timestamp_s: float) -> str:
        whole_seconds = int(timestamp_s)
        milliseconds = int((timestamp_s - whole_seconds) * 1000.0) % 1000
        return datetime.fromtimestamp(whole_seconds).strftime("%H:%M:%S:") + f"{milliseconds:03d}"

    def _broadcast_loop(self):
        broadcasters: Dict[Tuple[str, str, str], socket.socket] = {}
        next_interface_refresh = 0.0
        try:
            while self._running and not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_interface_refresh:
                    active_interfaces = set(self._active_ipv4_interfaces())
                    for interface_key in list(broadcasters):
                        if interface_key not in active_interfaces:
                            broadcasters.pop(interface_key).close()
                    for interface_key in active_interfaces:
                        if interface_key in broadcasters:
                            continue
                        local_ip, _netmask, _broadcast_ip = interface_key
                        broadcaster = None
                        try:
                            broadcaster = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            broadcaster.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                            broadcaster.bind((local_ip, 0))
                            broadcasters[interface_key] = broadcaster
                        except OSError:
                            if broadcaster is not None:
                                broadcaster.close()
                    next_interface_refresh = now + 5.0

                for (local_ip, netmask, broadcast_ip), broadcaster in list(broadcasters.items()):
                    message = self._build_broadcast_message(local_ip, netmask, self._port)
                    try:
                        broadcaster.sendto(message, (broadcast_ip, self.BROADCAST_PORT))
                    except OSError:
                        pass
                self._stop_event.wait(1.0)
        finally:
            for broadcaster in broadcasters.values():
                broadcaster.close()

    def _build_broadcast_message(self, local_ip: str, netmask: str, port: int) -> bytes:
        message = bytearray(17)
        message[0:2] = b"\xFF\xFF"
        message[2:6] = socket.inet_aton(local_ip)
        message[6:10] = socket.inet_aton(netmask)
        message[10] = 1
        message[11:13] = int(port).to_bytes(2, "big")
        message[13] = 1
        message[14:16] = int(port).to_bytes(2, "big")
        message[16] = self.parser.calculate_crc(message, 16)
        return bytes(message)

    @staticmethod
    def _active_ipv4_interfaces() -> List[Tuple[str, str, str]]:
        results: List[Tuple[str, str, str]] = []
        up_flag = QtNetwork.QNetworkInterface.InterfaceFlag.IsUp
        running_flag = QtNetwork.QNetworkInterface.InterfaceFlag.IsRunning
        loopback_flag = QtNetwork.QNetworkInterface.InterfaceFlag.IsLoopBack
        ipv4 = QtNetwork.QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
        for interface in QtNetwork.QNetworkInterface.allInterfaces():
            flags = interface.flags()
            if not flags & up_flag or not flags & running_flag or flags & loopback_flag:
                continue
            for entry in interface.addressEntries():
                if entry.ip().protocol() != ipv4 or entry.ip().isLoopback():
                    continue
                local_ip = entry.ip().toString()
                netmask = entry.netmask().toString()
                broadcast_ip = entry.broadcast().toString()
                if local_ip and netmask and broadcast_ip:
                    item = (local_ip, netmask, broadcast_ip)
                    if item not in results:
                        results.append(item)
        return results


class BiosignalChannelPlot(QWidget):
    def __init__(self, title: str, color: str, sample_rate: int = 1000, parent=None):
        super().__init__(parent)
        self.sample_rate = sample_rate
        self.buffer_size = 5 * sample_rate
        self.data_buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self.x_axis = np.arange(-self.buffer_size + 1, 1, dtype=np.float64) / sample_rate
        self._samples_since_rate_update = 0
        self._latest_rms = 0.0
        self._latest_amplitude = 0.0
        self._observed_rate = 0
        self._latest_dominant_frequency = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        header = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: 600; color: #333;")
        self.stats_label = QLabel("0 Hz · RMS 0.000 µV · 主频 0.0 Hz")
        self.stats_label.setStyleSheet("font-size: 10px; color: #666;")
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.stats_label)
        layout.addLayout(header)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        self.plot_widget.setDownsampling(mode="peak")
        self.plot_widget.setClipToView(True)
        self.plot_widget.setXRange(-5.0, 0.0, padding=0)
        self.plot_widget.setYRange(-500.0, 500.0, padding=0)
        self.plot_widget.setLabel("left", "µV")
        self.plot_widget.setLabel("bottom", "时间", "s")
        self.curve = self.plot_widget.plot(self.x_axis, self.data_buffer, pen=pg.mkPen(color=color, width=1))
        layout.addWidget(self.plot_widget, 1)
        self.setMinimumHeight(150)

    def add_batch(self, values: Sequence[float], rms_uv: float, amplitude_uv: float):
        values_array = np.asarray(values, dtype=np.float32)
        sample_count = int(values_array.size)
        if sample_count <= 0:
            return
        if sample_count >= self.buffer_size:
            self.data_buffer[:] = values_array[-self.buffer_size:]
        else:
            self.data_buffer[:-sample_count] = self.data_buffer[sample_count:]
            self.data_buffer[-sample_count:] = values_array
        self._samples_since_rate_update += sample_count
        self._latest_rms = float(rms_uv)
        self._latest_amplitude = float(amplitude_uv)

    def clear(self):
        self.data_buffer.fill(0.0)
        self._samples_since_rate_update = 0
        self._latest_rms = 0.0
        self._latest_amplitude = 0.0
        self._observed_rate = 0
        self._latest_dominant_frequency = 0.0
        self.curve.setData(self.x_axis, self.data_buffer)

    def refresh(self, update_rate: bool = False):
        if update_rate:
            self._observed_rate = self._samples_since_rate_update
            self._samples_since_rate_update = 0
            self._latest_dominant_frequency = self._dominant_frequency()
        if self.isVisible():
            self.curve.setData(self.x_axis, self.data_buffer)
            peak = float(np.max(np.abs(self.data_buffer)))
            y_limit = max(500.0, peak * 1.1)
            self.plot_widget.setYRange(-y_limit, y_limit, padding=0)
        self.stats_label.setText(
            f"{self._observed_rate} Hz · RMS {self._latest_rms:.3f} µV · "
            f"幅值 {self._latest_amplitude:.3f} µV · 主频 {self._latest_dominant_frequency:.1f} Hz"
        )

    def _dominant_frequency(self) -> float:
        fft_length = 1024
        data = self.data_buffer[-fft_length:].astype(np.float64, copy=True)
        if not np.any(data):
            return 0.0
        data *= np.hanning(fft_length)
        spectrum = np.abs(np.fft.rfft(data))
        frequencies = np.fft.rfftfreq(fft_length, d=1.0 / self.sample_rate)
        return float(frequencies[int(np.argmax(spectrum))])


class BiosignalPanel(QWidget):
    COLORS = (
        "#138A36", "#E58A00", "#C62828", "#1565C0", "#7B1FA2", "#795548",
        "#00838F", "#C2185B", "#455A64", "#5D4037", "#0277BD",
    )

    def __init__(
        self,
        title: str,
        channel_titles: Sequence[str],
        serials: Sequence[str],
        columns: int,
        parent=None,
    ):
        super().__init__(parent)
        self.serials = tuple(serials)
        self.channel_plots: List[BiosignalChannelPlot] = []
        self.status_labels: Dict[str, QLabel] = {}
        self._rate_tick = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 14px; font-weight: 700;")
        root.addWidget(title_label)

        status_row = QHBoxLayout()
        for serial in self.serials:
            status = QLabel(f"● {serial} 等待数据")
            status.setStyleSheet("color: #777;")
            status_row.addWidget(status)
            self.status_labels[serial] = status
        status_row.addStretch(1)
        root.addLayout(status_row)

        grid = QGridLayout()
        grid.setSpacing(6)
        for index, channel_title in enumerate(channel_titles):
            plot = BiosignalChannelPlot(channel_title, self.COLORS[index % len(self.COLORS)])
            self.channel_plots.append(plot)
            grid.addWidget(plot, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        root.addLayout(grid, 1)

        self._render_timer = QtCore.QTimer(self)
        self._render_timer.timeout.connect(self._refresh_plots)
        self._render_timer.start(50)

    def add_batch(self, channel_index: int, payload: dict):
        if not 0 <= channel_index < len(self.channel_plots):
            return
        self.channel_plots[channel_index].add_batch(
            payload.get("values", []),
            payload.get("rms_uv", 0.0),
            payload.get("amplitude_uv", 0.0),
        )

    def update_device_status(self, serial: str, online: bool, count: int, source: str):
        label = self.status_labels.get(serial)
        if label is None:
            return
        if online:
            detail = f" · {source}" if source else ""
            label.setText(f"● {serial} 在线 · {count} 帧{detail}")
            label.setStyleSheet("color: #138A36;")
        else:
            label.setText(f"● {serial} 等待数据")
            label.setStyleSheet("color: #777;")

    def clear(self):
        for plot in self.channel_plots:
            plot.clear()

    def _refresh_plots(self):
        self._rate_tick = (self._rate_tick + 1) % 20
        update_rate = self._rate_tick == 0
        for plot in self.channel_plots:
            plot.refresh(update_rate=update_rate)
