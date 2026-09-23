"""
hotkey_manager.py
-----------------
负责全局快捷键监听（基于 keyboard 库的低层 hook），并把按键触发转为 Qt 信号

使用 keyboard.hook 直接按 event.name 匹配按键，比 add_hotkey 的组合匹配更可靠：
在按住其他键（例如游戏中按住 W 前进）的同时再按下热键也能被监听到
"""

import keyboard
from PySide6.QtCore import QObject, Signal


class HotkeyManager(QObject):
    """全局快捷键管理器：根据配置注册/注销热键，并把每次触发转成 Qt 信号"""

    play_pause_triggered = Signal()
    seek_backward_triggered = Signal()
    seek_forward_triggered = Signal()
    opacity_down_triggered = Signal()
    opacity_up_triggered = Signal()
    toggle_visibility_triggered = Signal()
    toggle_immersive_triggered = Signal()

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self._hook = None
        self._key_to_callback = {}
        self.apply_hotkeys(config)

    def apply_hotkeys(self, config: dict):
        self.config = config
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
        self._key_to_callback = {}
        for key, callback in bindings:
            if key and key not in self._key_to_callback:
                self._key_to_callback[keyboard.key_to_scan_codes(key)[0]] = callback
        self._install_hook()

    def _install_hook(self):
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except KeyError:
                pass
            self._hook = None
        if not self._key_to_callback:
            return
        self._hook = keyboard.hook(self._on_event)

    def _on_event(self, event):
        # 仅在按下时触发一次；按住不放产生的重复 down 事件同样会命中（用于按住连播）
        if event.event_type == keyboard.KEY_DOWN:
            callback = self._key_to_callback.get(event.scan_code)
            if callback is not None:
                callback()

    def shutdown(self):
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except KeyError:
                pass
        self._hook = None
        self._key_to_callback.clear()
