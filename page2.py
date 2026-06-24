import csv
import os
import time
from typing import Dict, List, Optional, Tuple

import cv2
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QComboBox,
)


class LabelTimelineWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration_ms = 0
        self.current_ms = 0
        self.prefog_ms = 3000
        self.show_prefog = True
        self.fog_intervals: List[Tuple[int, int]] = []
        self.experiment_intervals: List[Tuple[int, Optional[int]]] = []
        self.setMinimumHeight(50)
        self.setMaximumHeight(58)
        self.setToolTip("标签时间轴: normal=灰色, pre-fog=黄色, fog=红色, experiment=蓝色")

    def set_state(
        self,
        duration_ms: int,
        current_ms: int,
        intervals: List[Tuple[int, int]],
        prefog_ms: int,
        show_prefog: bool,
        experiment_intervals: Optional[List[Tuple[int, Optional[int]]]] = None,
    ):
        self.duration_ms = max(0, int(duration_ms))
        self.current_ms = max(0, min(int(current_ms), self.duration_ms))
        self.fog_intervals = list(intervals)
        self.prefog_ms = max(0, int(prefog_ms))
        self.show_prefog = show_prefog
        self.experiment_intervals = list(experiment_intervals or [])
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(4, 22, -4, -8)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        radius = rect.height() / 2
        base_color = QtGui.QColor("#D7DBE2")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base_color)
        painter.drawRoundedRect(rect, radius, radius)

        if self.duration_ms > 0:
            experiment_color = QtGui.QColor(37, 99, 235, 75)
            experiment_tick = QtGui.QColor("#2563EB")
            prefog_color = QtGui.QColor(255, 198, 66, 150)
            fog_color = QtGui.QColor(220, 53, 69, 175)
            for start_ms, end_ms in self.experiment_intervals:
                self._draw_segment(painter, rect, start_ms, end_ms if end_ms is not None else self.duration_ms, experiment_color)
            if self.show_prefog:
                for start_ms, end_ms in self.fog_intervals:
                    pre_start = max(0, start_ms - self.prefog_ms)
                    self._draw_segment(painter, rect, pre_start, start_ms, prefog_color)
            for start_ms, end_ms in self.fog_intervals:
                self._draw_segment(painter, rect, start_ms, end_ms, fog_color)
            for start_ms, end_ms in self.experiment_intervals:
                self._draw_tick(painter, rect, start_ms, experiment_tick, f"S {self._format_ms(start_ms)}")
                if end_ms is not None:
                    self._draw_tick(painter, rect, end_ms, experiment_tick, f"E {self._format_ms(end_ms)}")

            x = rect.left() + rect.width() * (self.current_ms / self.duration_ms)
            pen = QtGui.QPen(QtGui.QColor("#1A1A1A"), 2)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(x, rect.top() - 4), QtCore.QPointF(x, rect.bottom() + 4))

        painter.end()

    def _draw_segment(self, painter, rect, start_ms: int, end_ms: int, color: QtGui.QColor):
        if self.duration_ms <= 0 or end_ms <= start_ms:
            return
        start_ms = max(0, min(start_ms, self.duration_ms))
        end_ms = max(0, min(end_ms, self.duration_ms))
        if end_ms <= start_ms:
            return
        x1 = rect.left() + rect.width() * (start_ms / self.duration_ms)
        x2 = rect.left() + rect.width() * (end_ms / self.duration_ms)
        segment = QtCore.QRectF(x1, rect.top(), max(1.0, x2 - x1), rect.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(segment, rect.height() / 2, rect.height() / 2)

    def _draw_tick(self, painter, rect, value_ms: int, color: QtGui.QColor, label: str = ""):
        if self.duration_ms <= 0:
            return
        value_ms = max(0, min(int(value_ms), self.duration_ms))
        x = rect.left() + rect.width() * (value_ms / self.duration_ms)
        pen = QtGui.QPen(color, 3)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(x, rect.top() - 5), QtCore.QPointF(x, rect.bottom() + 5))
        if label:
            painter.setPen(QtGui.QPen(color, 1))
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(label)
            text_x = max(2, min(int(x - text_width / 2), self.width() - text_width - 2))
            painter.drawText(text_x, 14, label)

    @staticmethod
    def _format_ms(value_ms: int) -> str:
        value_ms = max(0, int(value_ms))
        minutes = value_ms // 60000
        seconds = (value_ms % 60000) // 1000
        millis = value_ms % 1000
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


class Page2Widget(QWidget):
    VIDEO_GRID_COLUMNS = 3
    VIDEO_SPECS = [
        ("camera1", "Camera 1", "camera1.mp4", "camera1_record_requested"),
        ("camera2", "Camera 2", "camera2.mp4", "camera2_record_requested"),
        ("camera3", "Camera 3", "camera3.mp4", "camera3_record_requested"),
        ("camera4", "Camera 4", "camera4.mp4", "camera4_record_requested"),
        ("d435i_rgb", "D435i RGB", "D435i/RGB.mp4", "d435i_record_requested"),
        ("d435i_stereo", "D435i Stereo", "D435i/Stereo.mp4", "d435i_record_requested"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page_2")
        self.session_dir = ""
        self.videos: Dict[str, dict] = {}
        self.duration_ms = 0
        self.primary_step_ms = 33
        self.playing = False
        self._seeking = False
        self._play_started_at = 0.0
        self._play_start_ms = 0
        self.fog_start_ms: Optional[int] = None
        self.fog_intervals: List[Tuple[int, int]] = []
        self.experiment_intervals: List[Tuple[int, Optional[int]]] = []
        self._updating_interval_table = False

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick_playback)
        self.timer.setInterval(33)

        self._setup_ui()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        source_group = QGroupBox("数据源")
        source_layout = QGridLayout(source_group)
        self.dir_input = QLineEdit(os.path.abspath("data"))
        self.btn_browse = QPushButton("选择保存目录")
        self.btn_browse.clicked.connect(self.choose_session_dir)
        self.btn_load = QPushButton(f"加载{len(self.VIDEO_SPECS)}路视频")
        self.btn_load.clicked.connect(lambda: self.load_session_videos())
        source_layout.addWidget(QLabel("会话目录"), 0, 0)
        source_layout.addWidget(self.dir_input, 0, 1)
        source_layout.addWidget(self.btn_browse, 0, 2)
        source_layout.addWidget(self.btn_load, 0, 3)
        root.addWidget(source_group)

        middle = QHBoxLayout()
        video_group = QGroupBox(f"{len(self.VIDEO_SPECS)}路视频同步标注")
        video_layout = QVBoxLayout(video_group)
        self.video_grid = QGridLayout()
        self.video_grid.setSpacing(8)
        for col in range(self.VIDEO_GRID_COLUMNS):
            self.video_grid.setColumnStretch(col, 1)
        row_count = (len(self.VIDEO_SPECS) + self.VIDEO_GRID_COLUMNS - 1) // self.VIDEO_GRID_COLUMNS
        for row in range(row_count):
            self.video_grid.setRowStretch(row, 1)
        self.video_labels: Dict[str, QLabel] = {}
        self.status_labels: Dict[str, QLabel] = {}
        for index, (key, title, _rel_path, _event_name) in enumerate(self.VIDEO_SPECS):
            panel = QGroupBox(title)
            panel_layout = QVBoxLayout(panel)
            video_label = QLabel("未加载")
            video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            video_label.setMinimumSize(320, 180)
            video_label.setStyleSheet("background: #111; color: #ddd;")
            status_label = QLabel("missing")
            status_label.setStyleSheet("color: #666;")
            panel_layout.addWidget(video_label, 1)
            panel_layout.addWidget(status_label)
            self.video_labels[key] = video_label
            self.status_labels[key] = status_label
            self.video_grid.addWidget(panel, index // self.VIDEO_GRID_COLUMNS, index % self.VIDEO_GRID_COLUMNS)
        video_layout.addLayout(self.video_grid, 1)

        self.label_timeline = LabelTimelineWidget()
        video_layout.addWidget(self.label_timeline)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setMinimumHeight(34)
        self.slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 10px; background: #D7DBE2; border-radius: 5px; }"
            "QSlider::sub-page:horizontal { background: #4C78A8; border-radius: 5px; }"
            "QSlider::handle:horizontal { width: 24px; height: 24px; margin: -8px 0; "
            "background: #FFFFFF; border: 2px solid #1A1A1A; border-radius: 12px; }"
        )
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.installEventFilter(self)
        video_layout.addWidget(self.slider)

        controls = QHBoxLayout()
        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_step_back = QPushButton("-1帧")
        self.btn_step_back.clicked.connect(lambda: self.step_frames(-1))
        self.btn_step_forward = QPushButton("+1帧")
        self.btn_step_forward.clicked.connect(lambda: self.step_frames(1))
        self.time_label = QLabel("00:00.000 / 00:00.000")
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_step_back)
        controls.addWidget(self.btn_step_forward)
        controls.addStretch(1)
        controls.addWidget(self.time_label)
        video_layout.addLayout(controls)
        middle.addWidget(video_group, 3)

        label_group = QGroupBox("标签")
        label_layout = QVBoxLayout(label_group)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("标签模式"))
        self.label_mode_combo = QComboBox()
        self.label_mode_combo.addItem("三分类: normal / pre-fog / fog", 3)
        self.label_mode_combo.addItem("二分类: normal / fog", 2)
        self.label_mode_combo.currentIndexChanged.connect(self._on_label_mode_changed)
        mode_row.addWidget(self.label_mode_combo, 1)
        label_layout.addLayout(mode_row)

        prefog_row = QHBoxLayout()
        prefog_row.addWidget(QLabel("pre-fog 时长"))
        self.prefog_spin = QDoubleSpinBox()
        self.prefog_spin.setRange(0.0, 30.0)
        self.prefog_spin.setSingleStep(0.5)
        self.prefog_spin.setDecimals(1)
        self.prefog_spin.setValue(3.0)
        self.prefog_spin.setSuffix(" s")
        self.prefog_spin.valueChanged.connect(self._on_prefog_duration_changed)
        prefog_row.addWidget(self.prefog_spin)
        label_layout.addLayout(prefog_row)

        self.current_label = QLabel("当前标签: normal (0)")
        label_layout.addWidget(self.current_label)

        label_layout.addWidget(QLabel("实验时间"))
        self.experiment_table = QTableWidget(0, 4)
        self.experiment_table.setHorizontalHeaderLabels(["#", "Experiment Start", "Experiment End", "Duration"])
        self.experiment_table.verticalHeader().setVisible(False)
        self.experiment_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.experiment_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.experiment_table.setMaximumHeight(110)
        label_layout.addWidget(self.experiment_table)

        self.btn_fog_start = QPushButton("FOG 开始")
        self.btn_fog_start.clicked.connect(self.mark_fog_start)
        self.btn_fog_end = QPushButton("FOG 结束并添加")
        self.btn_fog_end.clicked.connect(self.mark_fog_end)
        self.btn_delete_interval = QPushButton("删除选中区间")
        self.btn_delete_interval.clicked.connect(self.delete_selected_interval)
        self.btn_clear = QPushButton("清空 FOG 区间")
        self.btn_clear.clicked.connect(self.clear_intervals)
        self.btn_export = QPushButton("生成标签文件")
        self.btn_export.clicked.connect(self.export_labeled_imu)
        for button in (
            self.btn_fog_start,
            self.btn_fog_end,
            self.btn_delete_interval,
            self.btn_clear,
            self.btn_export,
        ):
            button.setMinimumHeight(36)
            label_layout.addWidget(button)

        self.interval_table = QTableWidget(0, 4)
        self.interval_table.setHorizontalHeaderLabels(["#", "FOG Start", "FOG End", "Pre-FOG Start"])
        self.interval_table.verticalHeader().setVisible(False)
        self.interval_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
            | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
            | QtWidgets.QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.interval_table.itemChanged.connect(self._on_interval_item_changed)
        self.interval_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        label_layout.addWidget(self.interval_table, 1)
        middle.addWidget(label_group, 1)
        root.addLayout(middle, 1)

    def choose_session_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择会话保存目录", self.dir_input.text())
        if not directory:
            return
        self.dir_input.setText(directory)
        self.load_session_videos()

    def preload_session_dir(self, session_dir: str):
        if not session_dir or not os.path.isdir(session_dir):
            return
        session_dir = os.path.abspath(session_dir)
        current_dir = os.path.abspath(self.session_dir) if self.session_dir else ""
        if current_dir == session_dir and self.videos:
            return
        self.dir_input.setText(session_dir)
        self.load_session_videos(show_warnings=False)

    def load_session_videos(self, show_warnings: bool = True):
        self._close_captures()
        self._reset_annotations()
        self.session_dir = self.dir_input.text().strip()
        if not os.path.isdir(self.session_dir):
            if show_warnings:
                QMessageBox.warning(self, "加载视频", f"目录不存在:\n{self.session_dir}")
            return

        events = self._read_session_events()
        max_end_ms = 0
        first_fps = None
        loaded = 0
        for key, title, rel_path, event_name in self.VIDEO_SPECS:
            path = os.path.join(self.session_dir, rel_path)
            label = self.video_labels[key]
            status = self.status_labels[key]
            if not os.path.exists(path):
                label.setText("未找到视频")
                status.setText(f"missing: {rel_path}")
                status.setStyleSheet("color: #b00020;")
                continue
            capture = cv2.VideoCapture(path)
            if not capture.isOpened():
                label.setText("无法打开视频")
                status.setText(f"open failed: {rel_path}")
                status.setStyleSheet("color: #b00020;")
                continue
            fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
            frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            duration_ms = int(frame_count / fps * 1000) if fps > 0 else 0
            offset_ms = self._video_offset_ms(key, event_name, events)
            self.videos[key] = {
                "title": title,
                "path": path,
                "rel_path": rel_path,
                "event_name": event_name,
                "capture": capture,
                "fps": fps,
                "duration_ms": duration_ms,
                "offset_ms": offset_ms,
            }
            loaded += 1
            max_end_ms = max(max_end_ms, offset_ms + duration_ms)
            first_fps = first_fps or fps
            status.setText(f"loaded | offset {offset_ms / 1000:.3f}s | {duration_ms / 1000:.3f}s")
            status.setStyleSheet("color: #138a36;")

        self.duration_ms = max_end_ms
        self.primary_step_ms = max(1, int(1000 / (first_fps or 30.0)))
        self.timer.setInterval(self.primary_step_ms)
        self.slider.setRange(0, max(0, self.duration_ms))
        self.fog_start_ms = None
        remote_interval_count = self._load_remote_fog_intervals()
        experiment_count = self._load_remote_experiment_events()
        self._seek_all(0)
        self._refresh_interval_table()
        self._refresh_experiment_table()
        self.slider.setFocus()
        if loaded == 0:
            if show_warnings:
                QMessageBox.warning(self, "加载视频", "未加载到任何视频。")
        elif remote_interval_count or experiment_count:
            self.current_label.setText(
                f"Loaded {remote_interval_count} FOG interval(s), {experiment_count} experiment interval(s)"
            )

    def _video_offset_ms(self, key: str, event_name: str, events: Dict[str, float]) -> int:
        if key.startswith("d435i_"):
            first_frame_ts = self._first_d435i_frame_timestamp_s()
            if first_frame_ts is not None:
                return int(first_frame_ts * 1000)
        if key.startswith("camera") and key[6:].isdigit():
            started_event = f"{key}_recording_started"
            if started_event in events:
                return int(float(events[started_event]) * 1000)
        return int(float(events.get(event_name, 0.0)) * 1000)

    def _first_d435i_frame_timestamp_s(self) -> Optional[float]:
        path = os.path.join(self.session_dir, "D435i", "frames.csv")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    return float(row.get("pc_timestamp", "0"))
        except (OSError, ValueError):
            return None
        return None

    def _load_remote_fog_intervals(self) -> int:
        edited_path = os.path.join(self.session_dir, "fog_intervals_edited.csv")
        path = edited_path if os.path.exists(edited_path) else os.path.join(self.session_dir, "remote_fog_intervals.csv")
        if not os.path.exists(path):
            return 0

        count = 0
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    start_ms = int(float(row.get("start_timestamp", "0")) * 1000)
                    end_ms = int(float(row.get("end_timestamp", "0")) * 1000)
                except ValueError:
                    continue
                if end_ms <= start_ms:
                    continue
                self.fog_intervals.append((start_ms, end_ms))
                count += 1
        self.fog_intervals.sort()
        return count

    def _load_remote_experiment_events(self) -> int:
        path = os.path.join(self.session_dir, "remote_fog_events.csv")
        if not os.path.exists(path):
            return 0

        starts: Dict[int, int] = {}
        ends: Dict[int, int] = {}
        fallback_index = 0
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                event_type = (row.get("event_type") or "").strip()
                if event_type not in ("experiment_start", "experiment_end", "experiment_end_auto_stop"):
                    continue
                try:
                    timestamp_ms = int(float(row.get("relative_timestamp", "0")) * 1000)
                except ValueError:
                    continue
                try:
                    interval_index = int(row.get("interval_index") or 0)
                except ValueError:
                    interval_index = 0
                if interval_index <= 0:
                    fallback_index += 1
                    interval_index = fallback_index
                if event_type == "experiment_start":
                    starts[interval_index] = timestamp_ms
                else:
                    ends[interval_index] = timestamp_ms

        intervals: List[Tuple[int, Optional[int]]] = []
        for interval_index in sorted(set(starts) | set(ends)):
            start_ms = starts.get(interval_index)
            end_ms = ends.get(interval_index)
            if start_ms is None:
                continue
            if end_ms is not None and end_ms < start_ms:
                end_ms = start_ms
            intervals.append((start_ms, end_ms))
        self.experiment_intervals = intervals
        return len(intervals)

    def toggle_play(self):
        if not self.videos:
            return
        self.playing = not self.playing
        self.btn_play.setText("暂停" if self.playing else "播放")
        if self.playing:
            self._play_started_at = time.monotonic()
            self._play_start_ms = self.current_time_ms()
            self.timer.start()
        else:
            self.timer.stop()

    def step_frames(self, frames: int):
        if not self.videos:
            return
        self.playing = False
        self.timer.stop()
        self.btn_play.setText("播放")
        self._seek_all(self.current_time_ms() + frames * self.primary_step_ms)
        self.slider.setFocus()

    def mark_fog_start(self):
        current_ms = self.current_time_ms()
        self.fog_start_ms = current_ms
        self.current_label.setText(f"FOG 起点: {self._format_ms(current_ms)}")

    def mark_fog_end(self):
        if self.fog_start_ms is None:
            QMessageBox.warning(self, "FOG 标注", "请先点击 FOG 开始。")
            return
        end_ms = self.current_time_ms()
        start_ms = self.fog_start_ms
        if end_ms < start_ms:
            start_ms, end_ms = end_ms, start_ms
        if end_ms == start_ms:
            QMessageBox.warning(self, "FOG 标注", "FOG 区间时长不能为 0。")
            return
        self.fog_intervals.append((start_ms, end_ms))
        self.fog_intervals.sort()
        self.fog_start_ms = None
        self._refresh_interval_table()
        self._update_current_label(self.current_time_ms())

    def delete_selected_interval(self):
        row = self.interval_table.currentRow()
        if row < 0 or row >= len(self.fog_intervals):
            return
        del self.fog_intervals[row]
        self._refresh_interval_table()
        self._update_current_label(self.current_time_ms())

    def clear_intervals(self):
        self.fog_intervals.clear()
        self.fog_start_ms = None
        self._refresh_interval_table()
        self._update_current_label(self.current_time_ms())

    def _reset_annotations(self):
        self.fog_intervals.clear()
        self.experiment_intervals.clear()
        self.fog_start_ms = None
        if hasattr(self, "experiment_table"):
            self.experiment_table.setRowCount(0)
        self.interval_table.setRowCount(0)
        self._set_slider_value(0)
        self._update_time_label(0)
        self._update_current_label(0)
        self._refresh_label_timeline(0)

    def export_labeled_imu(self):
        session_dir = self.dir_input.text().strip()
        imu_path = os.path.join(session_dir, "imu.csv")
        if not os.path.exists(imu_path):
            QMessageBox.warning(self, "导出标签", f"未找到 IMU 文件:\n{imu_path}")
            return
        output_path = os.path.join(session_dir, "imu_labeled.csv")
        prefog_s = float(self.prefog_spin.value())
        include_prefog = self._include_prefog()
        with open(imu_path, "r", newline="", encoding="utf-8") as src:
            reader = csv.DictReader(src)
            if not reader.fieldnames or "pc_timestamp" not in reader.fieldnames:
                QMessageBox.warning(self, "导出标签", "imu.csv 中未找到 pc_timestamp 列。")
                return
            fieldnames = [name for name in reader.fieldnames if name != "label"] + ["label"]
            rows = []
            for row in reader:
                try:
                    ts = float(row.get("pc_timestamp", "0"))
                except ValueError:
                    ts = 0.0
                row = dict(row)
                row["label"] = str(self._label_for_time(ts, prefog_s, include_prefog))
                rows.append(row)

        with open(output_path, "w", newline="", encoding="utf-8") as dst:
            writer = csv.DictWriter(dst, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        # 生成关键时间点文件: experiment start/end, FOG start/end, pre-fog start
        time_output_path = os.path.join(session_dir, "time_labeled.csv")
        time_fieldnames = ["timestamp", "event_type"]
        with open(time_output_path, "w", newline="", encoding="utf-8") as tdst:
            twriter = csv.DictWriter(tdst, fieldnames=time_fieldnames)
            twriter.writeheader()
            twriter.writerows(self._time_label_events(include_prefog))

        edited_intervals_path = os.path.join(session_dir, "fog_intervals_edited.csv")
        self._save_current_fog_intervals(edited_intervals_path)

        label_text = "normal=0, pre-fog=1, fog=2" if include_prefog else "normal=0, fog=2"
        QMessageBox.information(
            self,
            "导出完成",
            f"已生成:\n{output_path}\n{time_output_path}\n{edited_intervals_path}\n\n{label_text}",
        )

    def _time_label_events(self, include_prefog: bool) -> List[dict]:
        events = []
        prefog_ms = int(float(self.prefog_spin.value()) * 1000)
        for start_ms, end_ms in self.experiment_intervals:
            events.append({"timestamp": start_ms / 1000.0, "event_type": "experiment_start"})
            if end_ms is not None:
                events.append({"timestamp": end_ms / 1000.0, "event_type": "experiment_end"})
        for start_ms, end_ms in self.fog_intervals:
            if include_prefog:
                pre_start = max(0, start_ms - prefog_ms)
                events.append({"timestamp": pre_start / 1000.0, "event_type": "pre_fog_start"})
            events.append({"timestamp": start_ms / 1000.0, "event_type": "fog_start"})
            events.append({"timestamp": end_ms / 1000.0, "event_type": "fog_end"})
        events.sort(key=lambda row: (float(row["timestamp"]), str(row["event_type"])))
        return [
            {"timestamp": f"{float(row['timestamp']):.6f}", "event_type": row["event_type"]}
            for row in events
        ]

    def _save_current_fog_intervals(self, output_path: str):
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["interval_index", "start_timestamp", "end_timestamp", "duration_s"],
            )
            writer.writeheader()
            for index, (start_ms, end_ms) in enumerate(self.fog_intervals, start=1):
                start_s = start_ms / 1000.0
                end_s = end_ms / 1000.0
                writer.writerow({
                    "interval_index": index,
                    "start_timestamp": f"{start_s:.6f}",
                    "end_timestamp": f"{end_s:.6f}",
                    "duration_s": f"{max(0.0, end_s - start_s):.6f}",
                })

    def _label_for_time(self, timestamp_s: float, prefog_s: float, include_prefog: bool = True) -> int:
        for start_ms, end_ms in self.fog_intervals:
            start_s = start_ms / 1000.0
            end_s = end_ms / 1000.0
            if start_s <= timestamp_s <= end_s:
                return 2
        if not include_prefog:
            return 0
        for start_ms, _end_ms in self.fog_intervals:
            start_s = start_ms / 1000.0
            prefog_start_s = max(0.0, start_s - prefog_s)
            if prefog_start_s <= timestamp_s < start_s:
                return 1
        return 0

    def _tick_playback(self):
        if not self.playing:
            return
        elapsed_ms = int((time.monotonic() - self._play_started_at) * 1000)
        target_ms = self._play_start_ms + elapsed_ms
        if target_ms >= self.duration_ms:
            target_ms = self.duration_ms
            self.playing = False
            self.timer.stop()
            self.btn_play.setText("播放")
        self._seek_all(target_ms)

    def _seek_all(self, target_ms: int):
        target_ms = max(0, min(int(target_ms), self.duration_ms))
        for key, info in self.videos.items():
            self._seek_video(key, info, target_ms)
        self._set_slider_value(target_ms)
        self._update_time_label(target_ms)
        self._update_current_label(target_ms)
        self._refresh_label_timeline(target_ms)

    def _seek_video(self, key: str, info: dict, session_ms: int):
        video_ms = session_ms - int(info["offset_ms"])
        label = self.video_labels[key]
        if video_ms < 0:
            label.setText("等待开始")
            label.setPixmap(QtGui.QPixmap())
            return
        if video_ms > int(info["duration_ms"]):
            label.setText("视频结束")
            label.setPixmap(QtGui.QPixmap())
            return
        capture = info["capture"]
        capture.set(cv2.CAP_PROP_POS_MSEC, video_ms)
        ok, frame = capture.read()
        if not ok:
            label.setText("读取失败")
            label.setPixmap(QtGui.QPixmap())
            return
        self._show_frame(label, frame)

    def _show_frame(self, label: QLabel, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _channels = rgb.shape
        image = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888).copy()
        pixmap = QtGui.QPixmap.fromImage(image)
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def current_time_ms(self) -> int:
        return self.slider.value()

    def _slider_pressed(self):
        self._seeking = True

    def _slider_released(self):
        self._seeking = False
        self._seek_all(self.slider.value())

    def _slider_changed(self, value: int):
        if self._seeking:
            self._update_time_label(value)
            self._update_current_label(value)
            self._refresh_label_timeline(value)

    def _set_slider_value(self, value: int):
        old = self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(old)

    def _refresh_interval_table(self):
        self._updating_interval_table = True
        try:
            self.interval_table.setRowCount(len(self.fog_intervals))
            prefog_ms = int(float(self.prefog_spin.value()) * 1000)
            include_prefog = self._include_prefog()
            for row, (start_ms, end_ms) in enumerate(self.fog_intervals):
                pre_start = max(0, start_ms - prefog_ms)
                values = [
                    str(row + 1),
                    self._format_ms(start_ms),
                    self._format_ms(end_ms),
                    self._format_ms(pre_start) if include_prefog else "-",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col in (1, 2):
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.interval_table.setItem(row, col, item)
        finally:
            self._updating_interval_table = False
        self._refresh_label_timeline(self.current_time_ms())

    def _refresh_experiment_table(self):
        if not hasattr(self, "experiment_table"):
            return
        self.experiment_table.setRowCount(len(self.experiment_intervals))
        for row, (start_ms, end_ms) in enumerate(self.experiment_intervals):
            duration = "-" if end_ms is None else self._format_ms(max(0, end_ms - start_ms))
            values = [
                str(row + 1),
                self._format_ms(start_ms),
                self._format_ms(end_ms) if end_ms is not None else "-",
                duration,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.experiment_table.setItem(row, col, item)

    def _on_interval_item_changed(self, item: QTableWidgetItem):
        if self._updating_interval_table or item is None:
            return
        row = item.row()
        col = item.column()
        if col not in (1, 2) or row < 0 or row >= len(self.fog_intervals):
            return

        new_ms = self._parse_time_text(item.text())
        if new_ms is None:
            QMessageBox.warning(self, "标签时间", "请输入合法时间，例如 00:12.345 或 12.345。")
            self._refresh_interval_table()
            return
        if self.duration_ms > 0 and new_ms > self.duration_ms:
            QMessageBox.warning(self, "标签时间", "标签时间不能超过视频总时长。")
            self._refresh_interval_table()
            return

        start_ms, end_ms = self.fog_intervals[row]
        if col == 1:
            start_ms = new_ms
        else:
            end_ms = new_ms
        if end_ms <= start_ms:
            QMessageBox.warning(self, "标签时间", "FOG 结束时间必须大于开始时间。")
            self._refresh_interval_table()
            return

        updated_interval = (start_ms, end_ms)
        self.fog_intervals[row] = updated_interval
        self.fog_intervals.sort()
        self._refresh_interval_table()
        try:
            self.interval_table.selectRow(self.fog_intervals.index(updated_interval))
        except ValueError:
            pass
        self._update_current_label(self.current_time_ms())
        self._refresh_label_timeline(self.current_time_ms())

    def _on_prefog_duration_changed(self, *_args):
        self._refresh_interval_table()
        self._update_current_label(self.current_time_ms())
        self._refresh_label_timeline(self.current_time_ms())

    def _update_current_label(self, current_ms: int):
        label = self._label_for_time(current_ms / 1000.0, float(self.prefog_spin.value()), self._include_prefog())
        names = {0: "normal", 1: "pre-fog", 2: "fog"}
        self.current_label.setText(f"当前标签: {names[label]} ({label})")

    def _update_time_label(self, current_ms: int):
        self.time_label.setText(f"{self._format_ms(current_ms)} / {self._format_ms(self.duration_ms)}")

    def _include_prefog(self) -> bool:
        return int(self.label_mode_combo.currentData() or 3) == 3

    def _on_label_mode_changed(self, *_args):
        self.prefog_spin.setEnabled(self._include_prefog())
        self._refresh_interval_table()
        self._update_current_label(self.current_time_ms())
        self._refresh_label_timeline(self.current_time_ms())

    def _refresh_label_timeline(self, current_ms: int):
        self.label_timeline.set_state(
            self.duration_ms,
            current_ms,
            self.fog_intervals,
            int(float(self.prefog_spin.value()) * 1000),
            self._include_prefog(),
            self.experiment_intervals,
        )

    def keyPressEvent(self, event):
        if self._handle_seek_key(event):
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.slider and event.type() == QtCore.QEvent.Type.KeyPress:
            if self._handle_seek_key(event):
                return True
        return super().eventFilter(watched, event)

    def _handle_seek_key(self, event) -> bool:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1 if event.key() == Qt.Key.Key_Left else 1
            step_ms = self.primary_step_ms
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                step_ms = 1000
            self.playing = False
            self.timer.stop()
            self.btn_play.setText("播放")
            self._seek_all(self.current_time_ms() + direction * step_ms)
            event.accept()
            return True
        return False

    def _read_session_events(self) -> Dict[str, float]:
        path = os.path.join(self.session_dir, "session_events.csv")
        events: Dict[str, float] = {}
        if not os.path.exists(path):
            return events
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    event = row.get("event", "")
                    if not event:
                        continue
                    events[event] = float(row.get("relative_timestamp", 0.0))
        except Exception:
            return {}
        return events

    @staticmethod
    def _format_ms(value_ms: int) -> str:
        value_ms = max(0, int(value_ms))
        minutes = value_ms // 60000
        seconds = (value_ms % 60000) // 1000
        millis = value_ms % 1000
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"

    @staticmethod
    def _parse_time_text(text: str) -> Optional[int]:
        value = (text or "").strip().lower().replace("，", ".")
        if not value:
            return None
        try:
            if value.endswith("ms"):
                milliseconds = float(value[:-2].strip())
                return int(round(milliseconds)) if milliseconds >= 0 else None
            if value.endswith("s"):
                value = value[:-1].strip()
            if ":" in value:
                parts = value.split(":")
                if len(parts) == 2:
                    minutes = int(parts[0])
                    seconds = float(parts[1])
                    total_seconds = minutes * 60 + seconds
                elif len(parts) == 3:
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = float(parts[2])
                    total_seconds = hours * 3600 + minutes * 60 + seconds
                else:
                    return None
            else:
                total_seconds = float(value)
            if total_seconds < 0:
                return None
            return int(round(total_seconds * 1000))
        except ValueError:
            return None

    def _close_captures(self):
        self.timer.stop()
        self.playing = False
        self.btn_play.setText("播放")
        for info in self.videos.values():
            capture = info.get("capture")
            if capture is not None:
                capture.release()
        self.videos.clear()
        self.duration_ms = 0
        self.slider.setRange(0, 0)
        self._refresh_label_timeline(0)
        for key, label in self.video_labels.items():
            label.setPixmap(QtGui.QPixmap())
            label.setText("未加载")
            self.status_labels[key].setText("missing")
            self.status_labels[key].setStyleSheet("color: #666;")

    def deactivate_page(self):
        self._close_captures()

    def closeEvent(self, event):
        self._close_captures()
        super().closeEvent(event)
