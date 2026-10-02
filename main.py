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
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog
from webview.errors import WebViewException

import config as config_module
from bilibili import bilibili_config, bilibili_danmaku, subtitle_parser
from overlay import beacon_overlay, danmaku_overlay, overlay_manager
from ui import hotkey_manager, settings_dialog, webview_chrome, window_picker

webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
os.environ.setdefault("PYWEBVIEW_LOG", "1")

BASE_DIR = config_module.app_dir()
STORAGE_PATH = os.path.join(BASE_DIR, "webview_profile")

WINDOW_TITLE = "CompassPlayer"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_EX_LAYERED = 0x00080000
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_LWA_ALPHA = 0x00000002

_WH_MOUSE_LL = 14
_WM_MOUSEMOVE = 0x0200


class Controller:
    """跨线程共享状态：Qt 后台线程填充 bridge，主线程填充 window"""

    def __init__(self, hint_text):
        self.qt_ready = threading.Event()
        self.bridge = None
        self.window = None
        self.hint_text = hint_text
        self.immersive = False
        self.visible = True
        self.settings_dlg = None
        self.hover_peek_active = False
        self.hwnd = None
        self.window_picked = False
        self.map_active = False
        self.danmaku_active = False


class Bridge(QObject):
    """生活在 Qt 后台线程的 QObject：js_api 跨线程投递的入口"""

    navigate_requested = Signal(str)
    settings_requested = Signal()
    quit_requested = Signal()
    pick_window_requested = Signal()
    map_toggle_requested = Signal()
    danmaku_toggle_requested = Signal()
    danmaku_fetched = Signal(str, object)


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
        return None


def _chrome(controller, method, *args):
    """调用页面内注入工具栏（webview_chrome）的 window.__compassChrome 方法"""
    params = ", ".join(json.dumps(a) for a in args)
    return _run_js(
        controller,
        f"window.__compassChrome && window.__compassChrome.{method}({params});",
    )


def _sync_chrome(controller):
    """把窗口选择 / 地图 / 弹幕开关状态同步到工具栏按钮"""
    _chrome(
        controller,
        "setState",
        controller.window_picked,
        controller.map_active,
        controller.danmaku_active,
    )


def _bili_cookie(controller):
    """WebView2 当前页面的 Cookie 头（含 HttpOnly 的登录态），使弹幕请求与网页播放器同一身份"""
    try:
        jars = controller.window.get_cookies()
    except WebViewException as e:
        print("[main] 读取 Cookie 失败:", e)
        return ""
    return "; ".join(f"{name}={m.value}" for jar in jars for name, m in jar.items())


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
    _chrome(controller, "hide" if controller.immersive else "show")
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
    window_picker.set_click_through(hwnd, enable)
    child = _user32.FindWindowExW(hwnd, 0, None, None)
    while child:
        window_picker.set_click_through(child, enable)
        child = _user32.FindWindowExW(hwnd, child, None, None)


def _get_window_hwnd(controller):
    if not controller.hwnd:
        controller.hwnd = _user32.FindWindowW(None, WINDOW_TITLE)
    return controller.hwnd


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
    if active == controller.hover_peek_active:
        return
    controller.hover_peek_active = active
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
    """全局低层鼠标钩子：驱动沉浸模式悬停透视"""

    def __init__(self, controller, config):
        self.controller = controller
        self.config = config
        self._hook_id = None
        self._delegate = _HOOKPROC(self._proc)  # 必须持有引用，防止被 GC 回收

    def start(self):
        if self._hook_id:
            return
        self._hook_id = _user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._delegate, _kernel32.GetModuleHandleW(None), 0
        )

    def stop(self):
        if self._hook_id:
            _user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None

    def _proc(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam == _WM_MOUSEMOVE and self.controller.immersive:
            data = _MSLLHOOKSTRUCT.from_address(lParam)
            _apply_hover_peek(
                self.controller,
                self.config,
                _cursor_inside_window(self.controller, data.pt.x, data.pt.y),
            )
        return _user32.CallNextHookEx(self._hook_id, nCode, wParam, lParam)


def _on_settings_saved(controller, config, hotkeys):
    hotkeys.apply_hotkeys(config)
    controller.hint_text = webview_chrome.build_hint_text(config["hotkeys"])
    _chrome(controller, "setHint", controller.hint_text)


def _open_settings(controller, config, hotkeys):
    dialog = controller.settings_dlg
    if dialog is not None and dialog.isVisible():
        dialog.raise_()
        dialog.activateWindow()
        return

    def _on_closed(_result):
        controller.settings_dlg = None

    dialog = settings_dialog.SettingsDialog(config)
    dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dialog.saved.connect(lambda _c: _on_settings_saved(controller, config, hotkeys))
    dialog.finished.connect(_on_closed)
    controller.settings_dlg = dialog
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

    beacon = beacon_overlay.BeaconOverlay(config)
    danmaku = danmaku_overlay.DanmakuOverlay(config)
    manager = overlay_manager.OverlayManager(beacon, danmaku)
    hotkeys = hotkey_manager.HotkeyManager(config)

    def run_js(script):
        return _run_js(controller, script)

    hotkeys.play_pause_triggered.connect(lambda: run_js(bilibili_config.PLAY_PAUSE_JS))
    hotkeys.seek_backward_triggered.connect(
        lambda: run_js(bilibili_config.seek_js(-config["seek_seconds"]))
    )
    hotkeys.seek_forward_triggered.connect(
        lambda: run_js(bilibili_config.seek_js(config["seek_seconds"]))
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

    poller = subtitle_parser.SubtitlePoller(run_js)
    poller.direction_detected.connect(beacon.set_direction)

    video_sync = danmaku_overlay.VideoTimeSync(run_js)
    video_sync.time_updated.connect(danmaku.on_time_update)
    _danmaku_state = {"loaded_href": None}

    # ------------------------------------------------------------------
    # B 站弹幕设置同步
    # ------------------------------------------------------------------
    def _apply_bili_danmaku_settings():
        if not danmaku.is_enabled():
            return

        result = run_js(bilibili_config.DANMAKU_SETTINGS_JS)
        if result:
            danmaku.apply_bili_settings(json.loads(result))

    settings_sync_timer = QTimer()
    settings_sync_timer.timeout.connect(_apply_bili_danmaku_settings)
    settings_sync_timer.start(2000)

    # ------------------------------------------------------------------
    # 弹幕数据加载
    # ------------------------------------------------------------------
    def _fetch_danmaku_async(href):
        def _fetch():
            cookie = _bili_cookie(controller)
            try:
                items = bilibili_danmaku.fetch_danmaku_for_url(href, cookie)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
                print("[danmaku] 拉取弹幕失败:", e)
                items = []
            bridge.danmaku_fetched.emit(href, items)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_danmaku_fetched(href, items):
        if href != _danmaku_state["loaded_href"]:
            return  # 拉取期间已切换视频，丢弃过期结果
        if items:
            danmaku.load_items(items)
        else:
            _danmaku_state["loaded_href"] = None  # 再次开启弹幕时重试
            print("[danmaku] 未获取到弹幕（该视频可能没有弹幕，或接口暂时不可用）")

    def _load_danmaku(href):
        _danmaku_state["loaded_href"] = href
        danmaku.load_items([])
        _fetch_danmaku_async(href)

    def _current_href():
        return (
            run_js("(function(){return document.location.href;})();")
            or config["last_url"]
        )

    # ------------------------------------------------------------------
    # 窗口选择 / 地图 / 弹幕切换
    # ------------------------------------------------------------------
    def _set_picked(picked):
        """目标窗口挂载 / 解绑后重置映射开关，并同步工具栏"""
        controller.window_picked = picked
        controller.map_active = controller.danmaku_active = False
        video_sync.stop()
        _sync_chrome(controller)

    def _pick_window():
        dlg = window_picker.WindowPickerDialog(exclude_titles=(WINDOW_TITLE,))
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_hwnd:
            return
        if not manager.attach_to_window(dlg.selected_hwnd):
            print("[overlay] 目标窗口无效，未能挂载")
            return
        _set_picked(True)

    def _toggle_map_mapping():
        if not manager.is_attached():
            return
        controller.map_active = not beacon.is_enabled()
        beacon.set_enabled(controller.map_active)
        manager.restack()
        _sync_chrome(controller)
        if controller.map_active:
            run_js(bilibili_config.ENABLE_SUBTITLE_JS)

    def _toggle_danmaku_mapping():
        if not manager.is_attached():
            return
        if danmaku.is_enabled():
            controller.danmaku_active = False
            danmaku.set_enabled(False)
            video_sync.stop()
            _sync_chrome(controller)
            return
        href = _current_href()
        controller.danmaku_active = True
        danmaku.set_enabled(True)
        manager.restack()
        _sync_chrome(controller)
        video_sync.start(known_key=bilibili_danmaku.video_key(href))
        _apply_bili_danmaku_settings()
        if _danmaku_state["loaded_href"] != href:
            _load_danmaku(href)

    def _on_target_lost():
        _set_picked(False)
        print("[overlay] 目标窗口已关闭或最小化，已自动解绑")

    manager.target_lost.connect(_on_target_lost)

    # ------------------------------------------------------------------
    # Bridge 信号连接
    # ------------------------------------------------------------------
    bridge.navigate_requested.connect(lambda url: _navigate(controller, url))
    bridge.settings_requested.connect(
        lambda: _open_settings(controller, config, hotkeys)
    )
    bridge.pick_window_requested.connect(_pick_window)
    bridge.map_toggle_requested.connect(_toggle_map_mapping)
    bridge.danmaku_toggle_requested.connect(_toggle_danmaku_mapping)
    bridge.danmaku_fetched.connect(_on_danmaku_fetched)
    video_sync.video_changed.connect(_load_danmaku)

    mouse_hook = MouseHookManager(controller, config)
    mouse_hook.start()

    def _shutdown():
        mouse_hook.stop()
        settings_sync_timer.stop()
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
def main():
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print(
            "[main] ⚠ 未以管理员身份运行：游戏内全局热键可能失效（游戏进程通常以更高权限运行，"
        )
        print(
            "[main]    低权限的全局键盘钩子看不到游戏前台时的按键）。请右键 → 以管理员身份运行本程序。"
        )

    config = config_module.load_config()
    controller = Controller(webview_chrome.build_hint_text(config["hotkeys"]))

    qt_thread = threading.Thread(target=run_qt, args=(controller, config), daemon=True)
    qt_thread.start()
    controller.qt_ready.wait(timeout=10)

    geo = config["window_geometry"]
    api = webview_chrome.Api(lambda: controller.bridge)
    start_url = config["last_url"] or config["start_url"]
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
            window.evaluate_js(webview_chrome.build_chrome_js(controller.hint_text))
            _sync_chrome(controller)
            current = window.get_current_url()
            _chrome(controller, "setUrl", current)
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
