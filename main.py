"""
main.py
-------
CompassPlayer 程序入口

架构（受 pywebview 6.x 约束）：
- 主线程：webview.start()，承载 WebView2 窗口（支持 H.264/HEVC/AAC）
- 后台线程：QApplication + BeaconOverlay + HotkeyManager + SubtitlePoller
- 双向通信：
  - Qt → 页面：window.evaluate_js()（线程安全）
  - 页面 → Qt：js_api 回调 bridge 的 Qt 信号，自动排队投递到 Qt 线程
"""

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes

import webview
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog
from webview.errors import WebViewException

import config as config_module
from bilibili import bilibili_danmaku
from bilibili.subtitle_parser import SubtitlePoller
from overlay.beacon_overlay import BeaconOverlay
from overlay.danmaku_overlay import DanmakuOverlay, VideoTimeSync
from overlay.overlay_manager import OverlayManager
from ui.hotkey_manager import HotkeyManager
from ui.settings_dialog import SettingsDialog
from ui.webview_chrome import Api, build_chrome_js, build_hint_text
from ui.window_picker import WindowPickerDialog

webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
os.environ.setdefault("PYWEBVIEW_LOG", "1")

BASE_DIR = config_module.app_dir()
STORAGE_PATH = os.path.join(BASE_DIR, "webview_profile")

WINDOW_TITLE = "CompassPlayer"

PLAY_PAUSE_JS = (
    "(function(){var v=document.querySelector('video');"
    " if(v){ v.paused ? v.play() : v.pause(); }})();"
)

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_LWA_ALPHA = 0x00000002

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200


def seek_js(delta: int) -> str:
    return (
        "(function(){var v=document.querySelector('video');"
        " if(v){ v.currentTime += (" + str(delta) + "); }})();"
    )


class Controller:
    """跨线程共享状态：Qt 后台线程填充 bridge，主线程填充 window"""

    def __init__(self):
        self.qt_ready = threading.Event()
        self.bridge = None
        self.window = None
        self.hint_text = "CompassPlayer"
        self.immersive = False
        self.visible = True
        self._settings_dialog = None
        self._hover_peek_active = False
        self._cached_hwnd = None


class Bridge(QObject):
    """生活在 Qt 后台线程的 QObject：js_api 跨线程投递的入口"""

    navigate_requested = Signal(str)
    settings_requested = Signal()
    immersive_requested = Signal()
    quit_requested = Signal()
    pick_window_requested = Signal()
    map_toggle_requested = Signal()
    danmaku_toggle_requested = Signal()
    danmaku_fetched = Signal(object)


# ---------------------------------------------------------------------------
# 工具函数（供 Qt 线程内的信号槽调用）
# ---------------------------------------------------------------------------
def _run_js(controller, script):
    w = controller.window
    if w is None:
        return None
    try:
        return w.evaluate_js(script)
    except WebViewException as e:
        print("[main] evaluate_js 失败:", e)
        controller.window = None
        return None


def _navigate(controller, url):
    url = (url or "").strip()
    if not url:
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _run_js(controller, "window.location.href = " + json.dumps(url) + ";")


def _toggle_visibility(controller):
    w = controller.window
    if w is None:
        return
    try:
        if controller.visible:
            w.hide()
        else:
            w.show()
        controller.visible = not controller.visible
    except WebViewException as e:
        print("[main] 切换显隐失败:", e)


def _toggle_immersive(controller, config):
    controller.immersive = not controller.immersive
    fn = "hide" if controller.immersive else "show"
    _run_js(
        controller, "window.__compassChrome && window.__compassChrome." + fn + "();"
    )
    _set_frameless(controller, controller.immersive)
    if not controller.immersive:
        _apply_hover_peek(controller, config, False)


def _set_frameless(controller, frameless):
    """运行时切换无边框（去掉/恢复原生标题栏与边框）"""
    hwnd = _get_window_hwnd(controller)
    if not hwnd:
        return
    style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
    if frameless:
        style &= ~(_WS_CAPTION | _WS_THICKFRAME)
    else:
        style |= _WS_CAPTION | _WS_THICKFRAME
    _user32.SetWindowLongW(hwnd, _GWL_STYLE, style)
    _user32.SetWindowPos(
        hwnd,
        0,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
    )


def _find_window_hwnd():
    return _user32.FindWindowW(None, WINDOW_TITLE)


def _set_window_opacity(controller, opacity):
    """通过 Win32 layered window 设置整窗透明度（WebView2 的 DirectComposition 渲染
    可能使此功能降级为无效）"""
    hwnd = _get_window_hwnd(controller)
    if not hwnd:
        print("[main] 未找到窗口句柄，透明度调节不可用")
        return
    style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_LAYERED)
    _user32.SetLayeredWindowAttributes(hwnd, 0, int(opacity * 255), _LWA_ALPHA)


def _adjust_opacity(controller, config, delta):
    opacity = config["opacity"] + delta
    opacity = max(config["min_opacity"], min(config["max_opacity"], opacity))
    config["opacity"] = round(opacity, 2)
    _set_window_opacity(controller, config["opacity"])


def _set_click_through(controller, enable):
    """开启/关闭整个 WebView2 窗口（含子控件）的点击穿透"""
    hwnd = _get_window_hwnd(controller)
    if not hwnd:
        return

    def apply(h):
        style = _user32.GetWindowLongW(h, _GWL_EXSTYLE)
        if enable:
            style |= _WS_EX_TRANSPARENT | _WS_EX_LAYERED
        else:
            style &= ~_WS_EX_TRANSPARENT
        _user32.SetWindowLongW(h, _GWL_EXSTYLE, style)

    apply(hwnd)
    child = _user32.FindWindowExW(hwnd, 0, None, None)
    while child:
        apply(child)
        child = _user32.FindWindowExW(hwnd, child, None, None)


def _get_window_hwnd(controller):
    if not controller._cached_hwnd:
        controller._cached_hwnd = _find_window_hwnd()
    return controller._cached_hwnd


def _cursor_inside_window(controller, x, y):
    hwnd = _get_window_hwnd(controller)
    if not hwnd:
        return False
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    return rect.left <= x <= rect.right and rect.top <= y <= rect.bottom


def _apply_hover_peek(controller, config, active):
    """沉浸模式下：鼠标悬停 → 最小透明度 + 点击穿透；移开 → 恢复"""
    if active == controller._hover_peek_active:
        return
    controller._hover_peek_active = active
    _set_click_through(controller, active)
    if active:
        _set_window_opacity(controller, config["min_opacity"])
    else:
        _set_window_opacity(controller, config["opacity"])


# ---------------------------------------------------------------------------
# 全局低层鼠标钩子（WH_MOUSE_LL）：沉浸模式下实时检测鼠标划入/划出窗口
# ---------------------------------------------------------------------------
class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.FindWindowW.restype = ctypes.c_void_p
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = ctypes.c_void_p
_user32.FindWindowExW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
_user32.SetLayeredWindowAttributes.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.BYTE,
    wintypes.DWORD,
]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    _HOOKPROC,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.restype = wintypes.LPARAM
_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class MouseHookManager:
    """全局低层鼠标钩子：驱动沉浸模式悬停透视（无轮询"""

    def __init__(self, controller, config):
        self.controller = controller
        self.config = config
        self._hook_id = None
        self._delegate = _HOOKPROC(self._proc)  # 必须持有引用，防止被 GC 回收

    def start(self):
        if self._hook_id:
            return
        self._hook_id = _user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._delegate, _kernel32.GetModuleHandleW(None), 0
        )

    def stop(self):
        if self._hook_id:
            _user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None

    def _proc(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam == WM_MOUSEMOVE and self.controller.immersive:
            data = _MSLLHOOKSTRUCT.from_address(lParam)
            _apply_hover_peek(
                self.controller,
                self.config,
                _cursor_inside_window(self.controller, data.pt.x, data.pt.y),
            )
        return _user32.CallNextHookEx(self._hook_id, nCode, wParam, lParam)


def _on_settings_saved(controller, config, hotkeys):
    hotkeys.apply_hotkeys(config)
    hint = build_hint_text(config["hotkeys"])
    controller.hint_text = hint
    _run_js(
        controller,
        "window.__compassChrome && window.__compassChrome.setHint("
        + json.dumps(hint)
        + ");",
    )


def _open_settings(controller, config, hotkeys):
    dialog = controller._settings_dialog
    if dialog is not None and dialog.isVisible():
        dialog.raise_()
        dialog.activateWindow()
        return

    def _on_closed(_result):
        controller._settings_dialog = None

    dialog = SettingsDialog(config)
    dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dialog.saved.connect(lambda _c: _on_settings_saved(controller, config, hotkeys))
    dialog.finished.connect(_on_closed)
    controller._settings_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _request_quit(controller):
    """通过 bridge 信号投递到 Qt 线程执行清理并退出事件循环"""
    b = controller.bridge
    if b is not None:
        try:
            b.quit_requested.emit()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Qt 后台线程
# ---------------------------------------------------------------------------
def run_qt(controller, config):
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    bridge = Bridge()
    controller.bridge = bridge

    beacon = BeaconOverlay(config)
    danmaku = DanmakuOverlay(config)
    manager = OverlayManager(beacon, danmaku)
    hotkeys = HotkeyManager(config)

    def run_js(script):
        return _run_js(controller, script)

    hotkeys.play_pause_triggered.connect(lambda: run_js(PLAY_PAUSE_JS))
    hotkeys.seek_backward_triggered.connect(
        lambda: run_js(seek_js(-config["seek_seconds"]))
    )
    hotkeys.seek_forward_triggered.connect(
        lambda: run_js(seek_js(config["seek_seconds"]))
    )
    hotkeys.opacity_down_triggered.connect(
        lambda: _adjust_opacity(controller, config, -config["opacity_step"])
    )
    hotkeys.opacity_up_triggered.connect(
        lambda: _adjust_opacity(controller, config, config["opacity_step"])
    )
    hotkeys.toggle_visibility_triggered.connect(lambda: _toggle_visibility(controller))
    hotkeys.toggle_immersive_triggered.connect(
        lambda: _toggle_immersive(controller, config)
    )

    poller = SubtitlePoller(run_js)
    poller.direction_detected.connect(beacon.set_direction)

    video_sync = VideoTimeSync(run_js)
    video_sync.time_updated.connect(danmaku.on_time_update)
    _danmaku_state = {"loaded_href": None}

    def _set_map_button(active):
        run_js(
            "window.__compassChrome && window.__compassChrome.setMapActive("
            + ("true" if active else "false")
            + ");"
        )

    def _set_danmaku_button(active):
        run_js(
            "window.__compassChrome && window.__compassChrome.setDanmakuActive("
            + ("true" if active else "false")
            + ");"
        )

    def _fetch_danmaku_async(url):
        def _fetch():
            try:
                items = bilibili_danmaku.fetch_danmaku_for_url(url)
            except (OSError, ValueError, KeyError, TypeError) as e:
                print("[danmaku] 拉取弹幕失败:", e)
                items = []
            bridge.danmaku_fetched.emit(items)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_danmaku_fetched(items):
        danmaku.load_items(items)
        if not items:
            print("[danmaku] 未获取到弹幕（该视频可能没有弹幕，或接口暂时不可用）")

    def _on_video_changed(href):
        _danmaku_state["loaded_href"] = href
        danmaku.load_items([])
        _fetch_danmaku_async(href)

    def _current_href():
        return (
            run_js("(function(){return document.location.href;})();")
            or config.get("last_url")
            or ""
        )

    def _pick_window():
        dlg = WindowPickerDialog(exclude_titles=(WINDOW_TITLE,))
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_hwnd:
            return
        if not manager.attach_to_window(dlg.selected_hwnd):
            print("[overlay] 目标窗口无效，未能挂载")
            return
        video_sync.stop()
        _set_map_button(False)
        _set_danmaku_button(False)
        run_js(
            "window.__compassChrome && window.__compassChrome.setWindowPicked(true);"
        )

    def _toggle_map_mapping():
        if not manager.is_attached():
            return
        new_state = not beacon.is_enabled()
        beacon.set_enabled(new_state)
        manager.restack()
        _set_map_button(new_state)

    def _toggle_danmaku_mapping():
        if not manager.is_attached():
            return
        if danmaku.is_enabled():
            danmaku.set_enabled(False)
            video_sync.stop()
            _set_danmaku_button(False)
            return
        href = _current_href()
        danmaku.set_enabled(True)
        manager.restack()
        _set_danmaku_button(True)
        video_sync.start(known_bvid=bilibili_danmaku.extract_bvid(href))
        if _danmaku_state["loaded_href"] != href:
            _danmaku_state["loaded_href"] = href
            danmaku.load_items([])
            _fetch_danmaku_async(href)

    def _on_target_lost():
        video_sync.stop()
        _set_map_button(False)
        _set_danmaku_button(False)
        run_js(
            "window.__compassChrome && window.__compassChrome.setWindowPicked(false);"
        )
        print("[overlay] 目标窗口已关闭或最小化，已自动解绑")

    manager.target_lost.connect(_on_target_lost)
    video_sync.video_changed.connect(_on_video_changed)

    bridge.navigate_requested.connect(lambda url: _navigate(controller, url))
    bridge.settings_requested.connect(
        lambda: _open_settings(controller, config, hotkeys)
    )
    bridge.immersive_requested.connect(lambda: _toggle_immersive(controller, config))
    bridge.pick_window_requested.connect(_pick_window)
    bridge.map_toggle_requested.connect(_toggle_map_mapping)
    bridge.danmaku_toggle_requested.connect(_toggle_danmaku_mapping)
    bridge.danmaku_fetched.connect(_on_danmaku_fetched)

    mouse_hook = MouseHookManager(controller, config)
    mouse_hook.start()

    def _shutdown():
        mouse_hook.stop()
        try:
            video_sync.stop()
            manager.shutdown()
            beacon.close()
            danmaku.close()
            hotkeys.shutdown()
        except RuntimeError:
            pass
        app.quit()

    bridge.quit_requested.connect(_shutdown)

    controller.qt_ready.set()
    app.exec()


# ---------------------------------------------------------------------------
# 主线程
# ---------------------------------------------------------------------------
def _is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except (AttributeError, OSError):
        return True


def main():
    if not _is_admin():
        print(
            "[main] ⚠ 未以管理员身份运行：游戏内全局热键可能失效（游戏进程通常以更高权限运行，"
        )
        print(
            "[main]    低权限的全局键盘钩子看不到游戏前台时的按键）。请右键 → 以管理员身份运行本程序。"
        )

    config = config_module.load_config()
    controller = Controller()
    controller.hint_text = build_hint_text(config["hotkeys"])

    qt_thread = threading.Thread(target=run_qt, args=(controller, config), daemon=True)
    qt_thread.start()
    controller.qt_ready.wait(timeout=10)

    geo = config["window_geometry"]
    api = Api(lambda: controller.bridge)
    start_url = config.get("last_url") or config["start_url"]
    window = webview.create_window(
        WINDOW_TITLE,
        start_url,
        js_api=api,
        width=geo["width"],
        height=geo["height"],
        x=geo["x"],
        y=geo["y"],
        on_top=True,
    )
    controller.window = window

    def _save_geometry():
        try:
            x, y, w, h = window.x, window.y, window.width, window.height
            if w < 100 or h < 100 or x < -30000 or y < -30000:
                return
            config["window_geometry"] = {"x": x, "y": y, "width": w, "height": h}
        except (TypeError, AttributeError):
            pass

    def on_loaded(*args):
        try:
            window.evaluate_js(build_chrome_js(controller.hint_text))
            current = window.get_current_url()
            window.evaluate_js(
                "window.__compassChrome && window.__compassChrome.setUrl("
                + json.dumps(current)
                + ");"
            )
            if current and current.startswith(("http://", "https://")):
                config["last_url"] = current
                config_module.save_config(config)
        except (WebViewException, AttributeError) as e:
            print("[main] 注入失败:", e)
        _save_geometry()

    window.events.loaded += on_loaded
    window.events.resized += lambda *a: _save_geometry()
    window.events.moved += lambda *a: _save_geometry()

    def on_closed(*args):
        controller.window = None
        config_module.save_config(config)
        _request_quit(controller)

    window.events.closed += on_closed

    webview.start(
        gui="edgechromium",
        private_mode=False,
        storage_path=STORAGE_PATH,
        debug=True,
    )

    config_module.save_config(config)
    _request_quit(controller)
    qt_thread.join(timeout=5)
    print("[main] 已退出")


if __name__ == "__main__":
    main()
