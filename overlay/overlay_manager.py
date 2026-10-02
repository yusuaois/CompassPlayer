"""
overlay_manager.py
-------------------
统一管理贴在目标窗口上的子 overlay

子 overlay 需实现：
  on_attach(rect) / on_target_moved(rect) / on_detach()
  set_enabled(bool) / is_enabled() -> bool
  on_focus_changed(bool)

self.overlays 的顺序即渲染顺序（从下到上）；位置跟随与前台切换均由 WinEventHook
事件驱动，目标窗口被关闭/最小化也在前台切换事件中发现
"""

from PySide6.QtCore import QObject, QTimer, Signal

from ui.window_picker import (
    get_foreground_window,
    get_window_client_rect_on_screen,
    install_foreground_hook,
    install_location_hook,
    is_window_usable,
    uninstall_event_hook,
)


class OverlayManager(QObject):
    target_lost = Signal()

    def __init__(self, beacon, danmaku):
        super().__init__()
        # 渲染顺序从下到上，指南针始终在弹幕之上
        self.beacon = beacon
        self.danmaku = danmaku
        self.overlays = [self.danmaku, self.beacon]

        self._hwnd = None
        self._location_hook = None
        self._pending_refresh = False

        # 前台钩子是全局的，与挂载状态无关，生命周期同 OverlayManager
        self._focus_hook = install_foreground_hook(self._on_foreground_changed)

    def is_attached(self) -> bool:
        return self._hwnd is not None

    def attach_to_window(self, hwnd) -> bool:
        """挂载到新目标窗口；若已挂载，先解绑旧窗口（子 overlay 开关状态一并重置）"""
        if not is_window_usable(hwnd):
            return False
        rect = get_window_client_rect_on_screen(hwnd)
        if rect is None:
            return False

        self.detach()

        self._hwnd = hwnd
        for overlay in self.overlays:
            overlay.on_attach(rect)
        self.restack()

        self._location_hook = install_location_hook(hwnd, self._on_hook_moved)
        self._sync_focus()
        return True

    def detach(self):
        if self._hwnd is None:
            return
        uninstall_event_hook(self._location_hook)
        self._location_hook = None
        self._hwnd = None
        self._pending_refresh = False
        for overlay in self.overlays:
            overlay.on_detach()

    def shutdown(self):
        """程序退出时调用，一并卸载前台钩子"""
        self.detach()
        uninstall_event_hook(self._focus_hook)
        self._focus_hook = None

    def restack(self):
        """按 self.overlays 顺序重新排 z-order，确保叠加顺序不受开关先后影响"""
        for overlay in self.overlays:
            overlay.raise_()

    # ------------------------------------------------------------------
    # 位置跟随（事件驱动）
    # ------------------------------------------------------------------
    def _on_hook_moved(self):
        # 在系统钩子调用栈内只做最少的事，实际处理丢回 Qt 事件循环
        if self._pending_refresh:
            return
        self._pending_refresh = True
        QTimer.singleShot(0, self._refresh_geometry)

    def _refresh_geometry(self):
        self._pending_refresh = False
        if self._hwnd is None:
            return
        rect = get_window_client_rect_on_screen(self._hwnd)
        if rect is None:
            return
        for overlay in self.overlays:
            overlay.on_target_moved(rect)

    # ------------------------------------------------------------------
    # 前台窗口跟踪
    # ------------------------------------------------------------------
    def _on_foreground_changed(self):
        QTimer.singleShot(0, self._sync_focus)

    def _sync_focus(self):
        if self._hwnd is None:
            return
        if not is_window_usable(self._hwnd):
            self.detach()
            self.target_lost.emit()
            return
        has_focus = get_foreground_window() == self._hwnd
        for overlay in self.overlays:
            overlay.on_focus_changed(has_focus)
