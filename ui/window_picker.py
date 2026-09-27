"""
window_picker.py
-----------------
纯 ctypes 实现的 Win32 顶层窗口枚举 / 几何查询 / 点击穿透工具，
以及"选择映射目标窗口"的弹窗
"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt
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

_user32 = ctypes.windll.user32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.GetDpiForWindow.restype = wintypes.UINT
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]

# WinEventHook：目标窗口移动/缩放时立即收到通知，overlay 零延迟跟随
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
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
    WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
_user32.UnhookWinEvent.restype = wintypes.BOOL
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
_BASE_DPI = 96.0

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0
CHILDID_SELF = 0


def _get_title(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def list_candidate_windows(exclude_titles=()):
    """枚举可作为 overlay 目标的顶层窗口：可见、有标题、非 Tool 窗、无 owner

    过滤条件会自动排除本程序自己的无标题 Qt::Tool 悬浮窗
    """
    exclude = set(exclude_titles)
    results = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindow(hwnd, GW_OWNER):
            return True
        if _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        title = _get_title(hwnd)
        if not title or title in exclude:
            return True
        results.append((hwnd, title))
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def is_window_usable(hwnd) -> bool:
    if not hwnd:
        return False
    return bool(_user32.IsWindow(hwnd)) and not bool(_user32.IsIconic(hwnd))


def get_window_dpi_scale(hwnd) -> float:
    """返回目标窗口所在显示器的缩放比例（100% → 1.0）"""
    try:
        dpi = _user32.GetDpiForWindow(hwnd)
    except OSError:
        dpi = 0
    return (dpi or _BASE_DPI) / _BASE_DPI


def get_window_client_rect_on_screen(hwnd):
    """返回目标窗口客户区在屏幕坐标系下的 (x, y, width, height)；失败返回 None

    使用客户区（不含标题栏/边框）以精确对齐游戏渲染画面
    返回值已换算为 Qt 逻辑像素（QWidget.setGeometry 所需），而非 Win32 物理像素
    """
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    scale = get_window_dpi_scale(hwnd)
    return (
        round(pt.x / scale),
        round(pt.y / scale),
        round(w / scale),
        round(h / scale),
    )


def set_click_through(hwnd, enable: bool):
    """开启/关闭窗口的点击穿透（鼠标事件透传到下方窗口）"""
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class _EventHook:
    """钩子句柄 + ctypes 回调对象的容器

    两者必须同时被持有：回调对象一旦被 GC 回收，系统触发钩子时会踩坏内存
    """

    __slots__ = ("callback_ref", "hook")

    def __init__(self, hook, callback_ref):
        self.hook = hook
        self.callback_ref = callback_ref


def install_location_hook(hwnd, on_move):
    """给指定窗口安装位置/大小变化钩子（EVENT_OBJECT_LOCATIONCHANGE）

    目标窗口移动或缩放时立即调用 on_move()（无参数，回调内自行查询最新矩形）
    返回 _EventHook，传给 uninstall_event_hook() 卸载；失败返回 None
    """
    pid = wintypes.DWORD()
    tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not tid:
        return None

    def _raw_callback(_hook, event, ev_hwnd, id_object, id_child, _id_thread, _time):
        if (
            event == EVENT_OBJECT_LOCATIONCHANGE
            and ev_hwnd == hwnd
            and id_object == OBJID_WINDOW
            and id_child == CHILDID_SELF
        ):
            try:
                on_move()
            except Exception as e:  # noqa: BLE001
                print("[overlay] 位置变化回调出错:", e)

    callback_ref = WINEVENTPROC(_raw_callback)
    hook = _user32.SetWinEventHook(
        EVENT_OBJECT_LOCATIONCHANGE,
        EVENT_OBJECT_LOCATIONCHANGE,
        None,
        callback_ref,
        pid.value,
        tid,
        WINEVENT_OUTOFCONTEXT,
    )
    if not hook:
        return None
    return _EventHook(hook, callback_ref)


def get_foreground_window():
    return _user32.GetForegroundWindow()


def install_foreground_hook(on_change):
    """全局监听 EVENT_SYSTEM_FOREGROUND（任意窗口切换到前台）

    调用 on_change()（无参数），回调内用 get_foreground_window() 查当前前台窗口
    返回值 / 失败情况同 install_location_hook
    """

    def _raw_callback(_hook, event, ev_hwnd, id_object, id_child, _id_thread, _time):
        if (
            event == EVENT_SYSTEM_FOREGROUND
            and id_object == OBJID_WINDOW
            and id_child == CHILDID_SELF
        ):
            try:
                on_change()
            except OSError as e:
                print("[overlay] 前台窗口变化回调出错:", e)

    callback_ref = WINEVENTPROC(_raw_callback)
    hook = _user32.SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND,
        EVENT_SYSTEM_FOREGROUND,
        None,
        callback_ref,
        0,
        0,
        WINEVENT_OUTOFCONTEXT,
    )
    if not hook:
        return None
    return _EventHook(hook, callback_ref)


def uninstall_event_hook(event_hook):
    if event_hook is None:
        return
    try:
        _user32.UnhookWinEvent(event_hook.hook)
    except OSError:
        pass


class WindowPickerDialog(QDialog):
    """选择 overlay 目标窗口的弹窗：列出当前可见顶层窗口，双击或确认即选中"""

    def __init__(self, exclude_titles=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要贴上悬浮层的窗口")
        self.resize(360, 420)
        self._exclude_titles = exclude_titles
        self.selected_hwnd = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "选中一个正在运行的窗口（通常是游戏窗口），指南针/弹幕会浮动显示在它上面："
            )
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
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
