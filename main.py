import sys
import os

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtWidgets import QApplication, QMainWindow

from page1 import Page1Widget
from page2 import Page2Widget


class MainWindow(QMainWindow):
    REMOTE_BUTTON_KEY_CODES = (
        16777330,  # Qt.Key_VolumeUp, Ulanzi MT-44 B shutter button 1.
        16777328,  # Qt.Key_VolumeDown, Ulanzi MT-44 B shutter button 2.
    )

    def __init__(self):
        super().__init__()
        self._remote_button_keys = set(self.REMOTE_BUTTON_KEY_CODES)
        self._last_remote_button_ms = 0
        self.setWindowTitle("UpStudio FOG Data Tool")
        self.resize(1600, 950)
        self.tabs = QtWidgets.QTabWidget(self)
        self.page1 = Page1Widget(self)
        self.page2 = Page2Widget(self)
        self.tabs.addTab(self.page1, "Page1 采集")
        self.tabs.addTab(self.page2, "Page2 标注")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self.page1.activate_page()

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _on_tab_changed(self, index: int):
        if self.tabs.widget(index) is not self.page2:
            return
        session_dir = self._current_or_latest_session_dir()
        if session_dir:
            self.page2.preload_session_dir(session_dir)

    def _current_or_latest_session_dir(self) -> str:
        session_dir = getattr(self.page1, "session_dir", "")
        if session_dir and os.path.isdir(session_dir):
            return session_dir

        base_dir = self.page1.base_dir_input.text().strip() if hasattr(self.page1, "base_dir_input") else ""
        if not base_dir or not os.path.isdir(base_dir):
            return ""

        candidates = [
            os.path.join(base_dir, name)
            for name in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, name))
        ]
        if not candidates:
            return ""
        return max(candidates, key=os.path.getmtime)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.KeyPress and self._handle_remote_button_key(event):
            return True
        return super().eventFilter(watched, event)

    def _handle_remote_button_key(self, event) -> bool:
        key = event.key()
        if key not in self._remote_button_keys:
            return False
        if event.isAutoRepeat():
            return True

        now_ms = QtCore.QDateTime.currentMSecsSinceEpoch()
        if now_ms - self._last_remote_button_ms < 180:
            return True
        self._last_remote_button_ms = now_ms

        try:
            key_name = QtCore.Qt.Key(key).name
        except (TypeError, ValueError):
            key_name = str(key)
        self.page1.handle_remote_button_click(key_name, key)
        event.accept()
        return True

    def closeEvent(self, event):
        self.page1.stop_collection()
        self.page1.deactivate_page()
        self.page2.deactivate_page()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
