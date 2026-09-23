"""
settings_dialog.py
-------------------
按键自定义配置弹窗（QDialog）
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import config as config_module

ACTION_LABELS = {
    "play_pause": "播放 / 暂停",
    "seek_backward": "视频快退",
    "seek_forward": "视频快进",
    "opacity_down": "降低透明度",
    "opacity_up": "提高透明度",
    "toggle_visibility": "显示 / 隐藏窗口",
    "toggle_immersive": "切换沉浸模式",
}


class KeyCaptureButton(QPushButton):
    key_captured = Signal(str)

    def __init__(self, current_key: str, parent=None):
        super().__init__(current_key, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._listening = False
        self.clicked.connect(self._start_listening)

    def _start_listening(self):
        self._listening = True
        self.setText("请按下按键...")
        self.setFocus()

    def keyPressEvent(self, event):
        if not self._listening:
            super().keyPressEvent(event)
            return

        key_name = self._event_to_key_name(event)
        if key_name:
            self._listening = False
            self.setText(key_name)
            self.key_captured.emit(key_name)

    @staticmethod
    def _event_to_key_name(event):
        text = event.text()
        if text and text.isprintable() and text != " ":
            return text.lower()

        key = event.key()
        if key == Qt.Key.Key_Space:
            return "space"
        if key == Qt.Key.Key_QuoteLeft:
            return "`"
        if key == Qt.Key.Key_Minus:
            return "-"
        if key == Qt.Key.Key_Equal:
            return "="

        seq = QKeySequence(key).toString()
        return seq.lower() if seq else None


class SettingsDialog(QDialog):
    saved = Signal(dict)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CompassPlayer  —  按键设置")
        self.config = config
        self._pending_hotkeys = dict(config["hotkeys"])

        outer_layout = QVBoxLayout(self)

        hotkey_group = QGroupBox("全局快捷键（点击按钮后按下新按键即可重新绑定）")
        form = QFormLayout()
        for action, label_text in ACTION_LABELS.items():
            current_key = config["hotkeys"].get(action, "")
            btn = KeyCaptureButton(current_key)
            btn.key_captured.connect(
                lambda key, a=action: self._on_key_captured(a, key)
            )
            form.addRow(QLabel(label_text), btn)
        hotkey_group.setLayout(form)
        outer_layout.addWidget(hotkey_group)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        outer_layout.addWidget(button_box)

    def _on_key_captured(self, action: str, key: str):
        self._pending_hotkeys[action] = key

    def _on_save(self):
        self.config["hotkeys"] = self._pending_hotkeys
        config_module.save_config(self.config)
        self.saved.emit(self.config)
        self.accept()
