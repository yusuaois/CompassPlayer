"""
hotkey_manager.py
-----------------
全局快捷键监听（keyboard.hook）并把触发转为 Qt 信号

使用 keyboard.hook 按 scan_code 匹配，完全支持用户自定义改键
"""

import keyboard
from PySide6.QtCore import QObject, Signal


class HotkeyManager(QObject):
    play_pause_triggered = Signal()
    seek_backward_triggered = Signal()
    seek_forward_triggered = Signal()
    opacity_down_triggered = Signal()
    opacity_up_triggered = Signal()
    toggle_visibility_triggered = Signal()
    toggle_immersive_triggered = Signal()

    def __init__(self, config: dict):
        super().__init__()
        self._hook = None
        self._key_to_callback = {}
        self.apply_hotkeys(config)

    def apply_hotkeys(self, config: dict):
        hotkeys = config.get("hotkeys", {})
        bindings = [
            (hotkeys.get("play_pause"), self.play_pause_triggered.emit),
            (hotkeys.get("seek_backward"), self.seek_backward_triggered.emit),
            (hotkeys.get("seek_forward"), self.seek_forward_triggered.emit),
            (hotkeys.get("opacity_down"), self.opacity_down_triggered.emit),
            (hotkeys.get("opacity_up"), self.opacity_up_triggered.emit),
            (hotkeys.get("toggle_visibility"), self.toggle_visibility_triggered.emit),
            (hotkeys.get("toggle_immersive"), self.toggle_immersive_triggered.emit),
        ]

        self._key_to_callback = {}
        for key_name, callback in bindings:
            if not key_name:
                continue
            try:
                scodes = keyboard.key_to_scan_codes(key_name)
                for sc in scodes:
                    self._key_to_callback[sc] = callback
            except Exception as e:  # noqa: BLE001
                print(f"[hotkey] 警告: 无法解析按键 [{key_name}]: {e}")

        self._unhook()
        if self._key_to_callback:
            self._hook = keyboard.hook(self._on_event)

    def _unhook(self):
        if self._hook is not None:
            keyboard.unhook(self._hook)
            self._hook = None

    def _on_event(self, event):
        if event.event_type == keyboard.KEY_DOWN:
            callback = self._key_to_callback.get(event.scan_code)
            if callback is not None:
                callback()

    def shutdown(self):
        self._unhook()
        self._key_to_callback.clear()
