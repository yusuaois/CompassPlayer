"""
main.py
-------
CompassPlayer 程序入口。

架构说明（受 pywebview 6.x 约束：`webview.start()` 必须在 Python 主线程调用）：
- 主线程：`webview.start(gui="edgechromium")`，承载 WebView2（微软 Edge 内核）浏览器窗口，
  它自带 H.264/HEVC/AAC 解码器，因此 B 站能正常播放（原 QtWebEngine 因缺专利编解码器而黑屏报错）。
- 后台线程：QApplication + 指南针悬浮窗(BeaconOverlay) + 全局热键(HotkeyManager)
  + 字幕轮询(SubtitlePoller) + 设置弹窗。
- 双向通信：
  - Qt -> 页面：window.evaluate_js()（pywebview 内部用 WinForms Invoke marshaling，线程安全）。
  - 页面 -> Qt：js_api 回调 bridge 的 Qt 信号，PySide6 自动排队投递到 Qt 后台线程。
"""

import ctypes
import json
import os
import sys
import threading

import webview
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication
from webview.errors import WebViewException

import config as config_module
from beacon_overlay import BeaconOverlay
from hotkey_manager import HotkeyManager
from settings_dialog import SettingsDialog
from subtitle_parser import SubtitlePoller
from webview_chrome import Api, build_chrome_js, build_hint_text

BASE_DIR = config_module.app_dir()
STORAGE_PATH = os.path.join(
    BASE_DIR, "webview_profile"
)  # WebView2 用户数据目录（Cookie/登录态）

WINDOW_TITLE = "CompassPlayer"

# 控制页面 <video> 的 JS
PLAY_PAUSE_JS = (
    "(function(){var v=document.querySelector('video');"
    " if(v){ v.paused ? v.play() : v.pause(); }})();"
)


def seek_js(delta: int) -> str:
    return (
        "(function(){var v=document.querySelector('video');"
        " if(v){ v.currentTime += (" + str(delta) + "); }})();"
    )


class Controller:
    """跨线程共享状态：Qt 后台线程填充 bridge，主线程填充 window。"""

    def __init__(self):
        self.qt_ready = threading.Event()
        self.bridge = None  # QObject，生活在 Qt 线程
        self.window = None  # pywebview window
        self.hint_text = "CompassPlayer"
        self.immersive = False
        self.visible = True
        self._settings_dialog = None


class Bridge(QObject):
    """生活在 Qt 后台线程的 QObject：js_api 跨线程投递的入口。"""

    navigate_requested = Signal(str)
    settings_requested = Signal()
    immersive_requested = Signal()
    quit_requested = Signal()


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


def _toggle_immersive(controller):
    controller.immersive = not controller.immersive
    fn = "hide" if controller.immersive else "show"
    _run_js(
        controller, "window.__compassChrome && window.__compassChrome." + fn + "();"
    )
    _set_frameless(controller, controller.immersive)


def _set_frameless(controller, frameless):
    """运行时切换无边框：去掉/恢复原生标题栏与边框（pywebview 的 frameless 仅创建时生效）。"""
    hwnd = _find_window_hwnd()
    if not hwnd:
        return
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    if frameless:
        style &= ~(WS_CAPTION | WS_THICKFRAME)
    else:
        style |= WS_CAPTION | WS_THICKFRAME
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    user32.SetWindowPos(
        hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
    )


def _find_window_hwnd():
    user32 = ctypes.windll.user32
    return user32.FindWindowW(None, WINDOW_TITLE)


def _set_window_opacity(controller, opacity):
    """整窗透明度（尽力而为）：通过 Win32 layered window 作用于顶层窗口。

    注意：WebView2 用 DirectComposition 渲染，整窗 alpha 可能不生效；不生效时该功能降级。
    """
    hwnd = _find_window_hwnd()
    if not hwnd:
        print("[main] 未找到窗口句柄，透明度调节不可用")
        return
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    LWA_ALPHA = 0x00000002
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    user32.SetLayeredWindowAttributes(hwnd, 0, int(opacity * 255), LWA_ALPHA)


def _adjust_opacity(controller, config, delta):
    opacity = config["opacity"] + delta
    opacity = max(config["min_opacity"], min(config["max_opacity"], opacity))
    config["opacity"] = round(opacity, 2)
    _set_window_opacity(controller, config["opacity"])


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
    # 单例 + 置顶浮窗：点击多次只存在一个，且显示在主窗口之上
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
    """请求退出：通过 bridge 信号投递到 Qt 线程执行清理并退出事件循环。"""
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
    hotkeys = HotkeyManager(config)

    def run_js(script):
        return _run_js(controller, script)

    # 视频控制热键
    hotkeys.play_pause_triggered.connect(lambda: run_js(PLAY_PAUSE_JS))
    hotkeys.seek_backward_triggered.connect(
        lambda: run_js(seek_js(-config["seek_seconds"]))
    )
    hotkeys.seek_forward_triggered.connect(
        lambda: run_js(seek_js(config["seek_seconds"]))
    )

    # 窗口控制热键
    hotkeys.opacity_down_triggered.connect(
        lambda: _adjust_opacity(controller, config, -config["opacity_step"])
    )
    hotkeys.opacity_up_triggered.connect(
        lambda: _adjust_opacity(controller, config, config["opacity_step"])
    )
    hotkeys.toggle_visibility_triggered.connect(lambda: _toggle_visibility(controller))
    hotkeys.toggle_immersive_triggered.connect(lambda: _toggle_immersive(controller))

    # 字幕轮询 -> 指南针
    poller = SubtitlePoller(run_js)
    poller.direction_detected.connect(beacon.set_direction)

    # js_api -> bridge 信号 -> Qt 线程动作
    bridge.navigate_requested.connect(lambda url: _navigate(controller, url))
    bridge.settings_requested.connect(
        lambda: _open_settings(controller, config, hotkeys)
    )
    bridge.immersive_requested.connect(lambda: _toggle_immersive(controller))

    def _shutdown():
        try:
            beacon.close()
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

    # 1. 启动 Qt 后台线程
    qt_thread = threading.Thread(target=run_qt, args=(controller, config), daemon=True)
    qt_thread.start()
    controller.qt_ready.wait(timeout=10)

    # 2. 创建 WebView2 窗口（在主线程）
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
            # 窗口隐藏/销毁的过渡态会返回离屏坐标或极小尺寸，跳过以免污染配置
            if w < 100 or h < 100 or x < -30000 or y < -30000:
                return
            config["window_geometry"] = {"x": x, "y": y, "width": w, "height": h}
        except (TypeError, AttributeError):
            pass

    def on_loaded(*args):
        # 每次导航完成后：注入覆盖层 + 同步地址栏 + 记住当前页面
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
        # 窗口被关闭（右上角 X）时立即请求退出，避免指南针/进程残留
        config_module.save_config(config)
        _request_quit(controller)

    window.events.closed += on_closed

    # 3. 启动 pywebview（主线程，阻塞直到窗口关闭）
    webview.start(
        gui="edgechromium",
        private_mode=False,  # 保留 Cookie/localStorage，登录态持久化
        storage_path=STORAGE_PATH,
        debug=False,
    )

    # 4. 清理：保存配置、退出 Qt（几何信息已由 resized/moved/loaded 事件持续更新）
    config_module.save_config(config)
    _request_quit(controller)
    qt_thread.join(timeout=5)
    print("[main] 已退出")


if __name__ == "__main__":
    main()
