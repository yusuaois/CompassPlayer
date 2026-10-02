"""
window_picker.py
-----------------
Win32 顶层窗口枚举 / 客户区几何（换算为 Qt 逻辑坐标）/ 点击穿透 / WinEventHook 工具，
以及"选择映射目标窗口"的弹窗
"""

import ctypes
from ctypes import wintypes
from typing import NamedTuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
# WinEventHook：目标窗口移动/缩放、前台窗口切换时立即收到通知
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)

_user32 = ctypes.WinDLL("user32")


def _declare(name, restype, *argtypes):
    func = getattr(_user32, name)
    func.restype, func.argtypes = restype, list(argtypes)


_declare("EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_declare("IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_declare("IsWindow", wintypes.BOOL, wintypes.HWND)
_declare("IsIconic", wintypes.BOOL, wintypes.HWND)
_declare("GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
_declare("GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_declare("GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_declare("GetWindowLongW", ctypes.c_long, wintypes.HWND, ctypes.c_int)
_declare("SetWindowLongW", ctypes.c_long, wintypes.HWND, ctypes.c_int, ctypes.c_long)
_declare("GetClientRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_declare("ClientToScreen", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))
_declare(
    "SetWinEventHook",
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
    WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
)
_declare("UnhookWinEvent", wintypes.BOOL, wintypes.HANDLE)
_declare(
    "GetWindowThreadProcessId",
    wintypes.DWORD,
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
)
_declare("GetForegroundWindow", wintypes.HWND)

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0
CHILDID_SELF = 0


def _get_title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(_user32.GetWindowTextLengthW(hwnd) + 1)
    _user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def list_candidate_windows(exclude_titles=()):
    """枚举可作为 overlay 目标的顶层窗口：可见、有标题、非 Tool 窗、无 owner

    过滤条件会自动排除本程序自己的无标题 Qt::Tool 悬浮窗
    """
    exclude = set(exclude_titles)
    results = []

    def _cb(hwnd, _lparam):
        if (
            _user32.IsWindowVisible(hwnd)
            and not _user32.GetWindow(hwnd, GW_OWNER)
            and not (_user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
        ):
            title = _get_title(hwnd)
            if title and title not in exclude:
                results.append((hwnd, title))
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def is_window_usable(hwnd) -> bool:
    return bool(_user32.IsWindow(hwnd)) and not _user32.IsIconic(hwnd)


def _native_to_logical_rect(x, y, w, h):
    """Win32 物理像素矩形 → Qt 全局逻辑像素矩形"""
    cx, cy = x + w / 2, y + h / 2

    def gap(screen):
        g, dpr = screen.geometry(), screen.devicePixelRatio()
        dx = max(g.x() - cx, 0, cx - (g.x() + g.width() * dpr))
        dy = max(g.y() - cy, 0, cy - (g.y() + g.height() * dpr))
        return dx * dx + dy * dy

    screen = min(QGuiApplication.screens(), key=gap)
    origin, dpr = screen.geometry().topLeft(), screen.devicePixelRatio()
    return (
        round(origin.x() + (x - origin.x()) / dpr),
        round(origin.y() + (y - origin.y()) / dpr),
        round(w / dpr),
        round(h / dpr),
    )


def get_window_client_rect_on_screen(hwnd) -> tuple[int, int, int, int] | None:
    """返回目标窗口客户区在 Qt 全局逻辑坐标系下的 (x, y, width, height)；失败返回 None

    使用客户区（不含标题栏/边框）以精确对齐游戏渲染画面
    返回值已换算为 Qt 逻辑像素（QWidget.setGeometry 所需），而非 Win32 物理像素
    """
    rect, pt = wintypes.RECT(), wintypes.POINT(0, 0)
    if not (
        _user32.GetClientRect(hwnd, ctypes.byref(rect))
        and _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    ):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return _native_to_logical_rect(pt.x, pt.y, w, h)


def set_click_through(hwnd, enable: bool):
    """开启/关闭窗口的点击穿透（鼠标事件透传到下方窗口）"""
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class _EventHook(NamedTuple):
    """WinEvent 钩子句柄 + ctypes 回调对象

    回调对象必须与句柄同生命周期：被 GC 回收后系统再触发钩子会访问已释放的内存
    """

    hook: int
    callback_ref: object


def _install_event_hook(event, on_event, pid=0, tid=0):
    """注册单个事件的 WinEventHook，仅转发窗口自身的事件；失败返回 None

    on_event(hwnd) 在安装钩子的线程中被调用，该线程须运行消息循环
    """

    def _raw_callback(_hook, _event, hwnd, id_object, id_child, _id_thread, _time):
        if id_object == OBJID_WINDOW and id_child == CHILDID_SELF:
            on_event(hwnd)

    callback_ref = WINEVENTPROC(_raw_callback)
    hook = _user32.SetWinEventHook(
        event, event, None, callback_ref, pid, tid, WINEVENT_OUTOFCONTEXT
    )
    return _EventHook(hook, callback_ref) if hook else None


def install_location_hook(hwnd, on_move) -> _EventHook | None:
    """给指定窗口安装位置/大小变化钩子（EVENT_OBJECT_LOCATIONCHANGE）

    目标窗口移动或缩放时立即调用 on_move()（无参数，回调内自行查询最新矩形）
    返回 _EventHook，传给 uninstall_event_hook() 卸载；失败返回 None
    """
    pid = wintypes.DWORD()
    tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not tid:
        return None

    def _on_event(event_hwnd):
        if event_hwnd == hwnd:
            on_move()

    return _install_event_hook(EVENT_OBJECT_LOCATIONCHANGE, _on_event, pid.value, tid)


def get_foreground_window():
    return _user32.GetForegroundWindow()


def install_foreground_hook(on_change) -> _EventHook | None:
    """全局监听 EVENT_SYSTEM_FOREGROUND（任意窗口切换到前台）

    调用 on_change()（无参数），回调内用 get_foreground_window() 查当前前台窗口
    返回值 / 失败情况同 install_location_hook
    """
    return _install_event_hook(EVENT_SYSTEM_FOREGROUND, lambda _hwnd: on_change())


def uninstall_event_hook(event_hook):
    """卸载钩子；须在安装它的线程调用，传入 None 时无操作"""
    if event_hook is not None:
        _user32.UnhookWinEvent(event_hook.hook)


class WindowPickerDialog(QDialog):
    """选择 overlay 目标窗口的弹窗：列出当前可见顶层窗口，双击或确认即选中"""

    def __init__(self, exclude_titles=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要贴上悬浮层的窗口")
        self.resize(360, 420)
        self._exclude_titles = (*exclude_titles, self.windowTitle())
        self.selected_hwnd = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "选中一个正在运行的窗口（通常是游戏窗口），指南针/弹幕会浮动显示在它上面："
            )
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(self._reload)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload()

    def _reload(self):
        self._list.clear()
        for hwnd, title in list_candidate_windows(self._exclude_titles):
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, hwnd)
            self._list.addItem(item)

    def accept(self):
        item = self._list.currentItem()
        if item is not None:
            self.selected_hwnd = item.data(Qt.ItemDataRole.UserRole)
        super().accept()
