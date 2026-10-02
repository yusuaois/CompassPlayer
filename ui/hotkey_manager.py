"""
hotkey_manager.py
-----------------
全局快捷键监听（keyboard.hook）并把触发转为 Qt 信号

使用 keyboard.hook 按 scan_code 匹配，而非 add_hotkey 的组合匹配，
确保游戏内按住其他键时仍能响应热键
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
        hotkeys = config["hotkeys"]
        bindings = [
            (hotkeys["play_pause"], self.play_pause_triggered.emit),
            (hotkeys["seek_backward"], self.seek_backward_triggered.emit),
            (hotkeys["seek_forward"], self.seek_forward_triggered.emit),
            (hotkeys["opacity_down"], self.opacity_down_triggered.emit),
            (hotkeys["opacity_up"], self.opacity_up_triggered.emit),
            (hotkeys["toggle_visibility"], self.toggle_visibility_triggered.emit),
            (hotkeys["toggle_immersive"], self.toggle_immersive_triggered.emit),
        ]
        self._key_to_callback = {
            keyboard.key_to_scan_codes(key)[0]: callback
            for key, callback in bindings
            if key
        }
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
