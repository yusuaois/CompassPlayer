"""
danmaku_overlay.py
-------------------
把当前 B 站视频的弹幕，按目标窗口尺寸独立排版后，浮动渲染在目标窗口上方

对外接口（由 OverlayManager 调用）：
  on_attach / on_target_moved / on_detach / set_enabled / on_focus_changed

弹幕轨道（第几行）由本模块自行排版：B 站官方数据不含轨道信息
全程点击穿透，不影响目标窗口的鼠标操作

性能注意（勿退化）：
- 文字用 QPainter.drawText() 画，走 Qt 字形缓存；不要改用 QPainterPath 描边
- 无弹幕在动时跳过整帧；有弹幕时只对"旧位置 ∪ 新位置"调用 update(region)，
  不要改回无参数的 update()
"""

import bisect
import time
import urllib.parse

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from bilibili import bilibili_danmaku
from ui.window_picker import set_click_through

SCROLL_MODES = bilibili_danmaku.SCROLL_MODES
REVERSE_MODES = bilibili_danmaku.REVERSE_MODES
BOTTOM_MODES = bilibili_danmaku.BOTTOM_MODES
TOP_MODES = bilibili_danmaku.TOP_MODES

_FONT_PX_BY_SIZE = {18: 16, 25: 20, 36: 28}  # B 站字号档位 -> 参考像素（720p 基准）
_REF_HEIGHT = 720
_VIDEO_POLL_MS = 300
_SEEK_JUMP_THRESHOLD = 1.2  # 秒；轮询时间跳变超过此值视为用户拖动进度条
_MIN_LANE_GAP = 0.4  # 秒；同一轨道相邻弹幕的最小间隔

_VIDEO_STATE_JS = (
    "(function(){var v=document.querySelector('video');"
    " if(!v) return '';"
    " return v.currentTime.toFixed(3)+'|'+(v.paused?0:1)+'|'"
    "+encodeURIComponent(document.location.href);})();"
)


class VideoTimeSync(QObject):
    """周期性查询页面 <video> 的播放进度，顺带检测视频是否切换"""

    time_updated = Signal(float, bool)
    video_changed = Signal(str)

    def __init__(self, run_js, interval_ms: int = _VIDEO_POLL_MS):
        super().__init__()
        self._run_js = run_js
        self._interval_ms = interval_ms
        self._last_bvid = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self, known_bvid=None):
        self._last_bvid = known_bvid
        self._timer.start(self._interval_ms)

    def stop(self):
        self._timer.stop()

    def _poll(self):
        result = self._run_js(_VIDEO_STATE_JS)
        if not result:
            return
        try:
            t_str, playing_str, href_enc = result.split("|")
            href = urllib.parse.unquote(href_enc)
        except (ValueError, AttributeError):
            return

        bvid = bilibili_danmaku.extract_bvid(href)
        if bvid and bvid != self._last_bvid:
            self._last_bvid = bvid
            self.video_changed.emit(href)
            return  # 等新弹幕加载完再派发时间

        try:
            self.time_updated.emit(float(t_str), playing_str == "1")
        except ValueError:
            pass


class _ScrollItem:
    __slots__ = ("color", "font", "line_h", "reverse", "text", "text_w", "x", "y")

    def __init__(self, text, color, font, metrics, reverse):
        self.text = text
        self.color = color
        self.font = font
        self.reverse = reverse
        self.text_w = metrics.horizontalAdvance(text)
        self.line_h = metrics.height()
        self.x = 0.0
        self.y = 0.0

    def rect(self):
        pad = 3
        return QRect(
            int(self.x) - pad,
            int(self.y) - pad,
            int(self.text_w) + pad * 2,
            int(self.line_h) + pad * 2,
        )


class _FixedItem:
    __slots__ = ("color", "expire_at", "font", "line_h", "text", "text_w", "y")

    def __init__(self, text, color, font, metrics, expire_at):
        self.text = text
        self.color = color
        self.font = font
        self.expire_at = expire_at
        self.text_w = metrics.horizontalAdvance(text)
        self.line_h = metrics.height()
        self.y = 0.0

    def rect(self, widget_width):
        pad = 3
        x = (widget_width - self.text_w) / 2
        return QRect(
            int(x) - pad,
            int(self.y) - pad,
            int(self.text_w) + pad * 2,
            int(self.line_h) + pad * 2,
        )


class DanmakuOverlay(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._items = []
        self._next_index = 0
        self._scroll_lanes_free_at = []
        self._reverse_lanes_free_at = []
        self._top_slots = []
        self._bottom_slots = []

        self._active_scroll = []
        self._active_top = []
        self._active_bottom = []

        self._font_cache = {}
        self._metrics_cache = {}

        self._last_video_time = 0.0
        self._is_playing = False
        self._attached = False
        self._enabled = False
        self._has_focus = False
        self._click_through_applied = False
        self._last_tick_ts = None

        self._tick_timer = QTimer(self)
        self._tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick_timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # 由 OverlayManager 调用的生命周期接口
    # ------------------------------------------------------------------
    def on_attach(self, rect):
        self._attached = True
        self.setGeometry(*rect)
        self._reset_lanes()

    def on_target_moved(self, rect):
        if not self._attached:
            return
        if (rect[0], rect[1], rect[2], rect[3]) != (
            self.x(),
            self.y(),
            self.width(),
            self.height(),
        ):
            self.setGeometry(*rect)
            self._reset_lanes()

    def on_detach(self):
        self._attached = False
        self._enabled = False
        self._has_focus = False
        self._sync_visibility()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._sync_visibility()

    def is_enabled(self) -> bool:
        """返回用户开关的意图，不代表当前是否可见"""
        return self._enabled

    def on_focus_changed(self, has_focus: bool):
        self._has_focus = has_focus
        self._sync_visibility()

    def _sync_visibility(self):
        should_show = self._attached and self._enabled and self._has_focus
        if should_show:
            # 丢弃隐藏期间积压的弹幕，从当前视频进度重新开始
            self._reflow_from(self._last_video_time)
            if not self._click_through_applied:
                self.show()
                set_click_through(int(self.winId()), True)
                self._click_through_applied = True
            else:
                self.show()
            self.raise_()
            # 重置计时基准，避免恢复显示时 dt 过大导致弹幕位置突跳
            self._last_tick_ts = time.monotonic()
            self._tick_timer.start(int(self._dcfg()["tick_ms"]))
        else:
            self._tick_timer.stop()
            self._active_scroll = []
            self._active_top = []
            self._active_bottom = []
            self.hide()

    # ------------------------------------------------------------------
    # 弹幕数据
    # ------------------------------------------------------------------
    def load_items(self, items):
        self._items = items
        self._last_video_time = 0.0
        self._reflow_from(0.0)

    def on_time_update(self, current_time: float, is_playing: bool):
        jumped = (
            current_time < self._last_video_time - _SEEK_JUMP_THRESHOLD
            or current_time > self._last_video_time + _SEEK_JUMP_THRESHOLD
        )
        if jumped:
            self._reflow_from(current_time)
        else:
            self._spawn_due(current_time)
        self._last_video_time = current_time
        self._is_playing = is_playing

    # ------------------------------------------------------------------
    # 字体缓存
    # ------------------------------------------------------------------
    def _font_for_px(self, px):
        font = self._font_cache.get(px)
        if font is None:
            font = QFont()
            font.setPixelSize(px)
            font.setBold(True)
            self._font_cache[px] = font
        return font

    def _metrics_for_px(self, px):
        metrics = self._metrics_cache.get(px)
        if metrics is None:
            metrics = QFontMetrics(self._font_for_px(px))
            self._metrics_cache[px] = metrics
        return metrics

    # ------------------------------------------------------------------
    # 轨道 / 排版
    # ------------------------------------------------------------------
    def _dcfg(self):
        return self.config["danmaku"]

    def _lane_count(self):
        px = self._font_px(25)
        lane_h = max(1, px + 6)
        area = max(0.05, min(1.0, float(self._dcfg()["display_area"])))
        h = max(1, round(self.height() * area))
        return max(1, min(int(self._dcfg()["max_lanes"]), h // lane_h))

    def _font_px(self, bili_font_size):
        base = _FONT_PX_BY_SIZE.get(bili_font_size, 20)
        h = max(self.height(), 1)
        scale = (h / _REF_HEIGHT) * float(self._dcfg()["font_scale"])
        return max(10, round(base * scale))

    def _cross_seconds(self):
        speed = max(0.1, float(self._dcfg()["speed"]))
        return max(1.0, float(self._dcfg()["cross_seconds"]) / speed)

    def _reset_lanes(self):
        lanes = self._lane_count()
        self._scroll_lanes_free_at = [-1e9] * lanes
        self._reverse_lanes_free_at = [-1e9] * lanes
        slot_count = max(1, min(4, lanes))
        self._top_slots = [-1e9] * slot_count
        self._bottom_slots = [-1e9] * slot_count

    def _reflow_from(self, video_time):
        """seek 或重新加载后调用：清空画面，将待生成指针对齐到 video_time"""
        self._active_scroll = []
        self._active_top = []
        self._active_bottom = []
        self._reset_lanes()
        self.update()
        times = [it.time for it in self._items]
        self._next_index = bisect.bisect_left(times, video_time)

    def _spawn_due(self, current_time):
        n = len(self._items)
        while (
            self._next_index < n and self._items[self._next_index].time <= current_time
        ):
            self._spawn_one(self._items[self._next_index], current_time)
            self._next_index += 1

    def _spawn_one(self, item, now):
        color = QColor(
            (item.color >> 16) & 0xFF, (item.color >> 8) & 0xFF, item.color & 0xFF
        )
        px = self._font_px(item.font_size)
        font = self._font_for_px(px)
        metrics = self._metrics_for_px(px)

        if item.mode in SCROLL_MODES or item.mode in REVERSE_MODES:
            reverse = item.mode in REVERSE_MODES
            lanes = (
                self._reverse_lanes_free_at if reverse else self._scroll_lanes_free_at
            )
            if not lanes:
                return
            lane = min(range(len(lanes)), key=lambda i: lanes[i])
            vis = _ScrollItem(item.text, color, font, metrics, reverse)
            lane_h = max(1, px + 6)
            vis.y = lane * lane_h + 2
            w = max(self.width(), 1)
            vis.x = float(-vis.text_w) if reverse else float(w)
            self._active_scroll.append(vis)

            speed_px = w / self._cross_seconds()
            clear_time = vis.text_w / max(speed_px, 1.0)
            lanes[lane] = now + clear_time + _MIN_LANE_GAP
        elif item.mode in TOP_MODES or item.mode in BOTTOM_MODES:
            slots = self._top_slots if item.mode in TOP_MODES else self._bottom_slots
            if not slots:
                return
            slot = min(range(len(slots)), key=lambda i: slots[i])
            fixed_seconds = float(self._dcfg()["fixed_seconds"])
            vis = _FixedItem(item.text, color, font, metrics, now + fixed_seconds)
            lane_h = max(1, px + 6)
            if item.mode in TOP_MODES:
                vis.y = slot * lane_h + 4
                self._active_top.append(vis)
            else:
                vis.y = self.height() - (slot + 1) * lane_h - 4
                self._active_bottom.append(vis)
            slots[slot] = vis.expire_at
            # 固定弹幕不滚动，生成后触发一次局部重绘让它立刻出现
            self.update(vis.rect(self.width()))

    # ------------------------------------------------------------------
    # 动画 tick：仅在有弹幕滚动时重绘，且只重绘变化区域
    # ------------------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        dt = now - (self._last_tick_ts or now)
        self._last_tick_ts = now

        if not self._is_playing or dt <= 0:
            return

        if not self._active_scroll and not self._active_top and not self._active_bottom:
            return

        dirty = QRegion()
        for vis in self._active_scroll:
            dirty += vis.rect()

        w = max(self.width(), 1)
        speed_px = w / self._cross_seconds()
        still = []
        for vis in self._active_scroll:
            if vis.reverse:
                vis.x += speed_px * dt
                if vis.x <= w:
                    still.append(vis)
            else:
                vis.x -= speed_px * dt
                if vis.x + vis.text_w >= 0:
                    still.append(vis)
        self._active_scroll = still

        kept_top = []
        for vis in self._active_top:
            if vis.expire_at > self._last_video_time:
                kept_top.append(vis)
            else:
                dirty += vis.rect(self.width())
        self._active_top = kept_top

        kept_bottom = []
        for vis in self._active_bottom:
            if vis.expire_at > self._last_video_time:
                kept_bottom.append(vis)
            else:
                dirty += vis.rect(self.width())
        self._active_bottom = kept_bottom

        for vis in self._active_scroll:
            dirty += vis.rect()

        if not dirty.isEmpty():
            self.update(dirty)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        opacity = float(self._dcfg()["opacity"])
        painter.setOpacity(max(0.1, min(1.0, opacity)))

        for vis in self._active_scroll:
            self._draw_item(painter, vis, vis.x, vis.y, vis.text_w)
        for vis in self._active_top:
            x = (self.width() - vis.text_w) / 2
            self._draw_item(painter, vis, x, vis.y, vis.text_w)
        for vis in self._active_bottom:
            x = (self.width() - vis.text_w) / 2
            self._draw_item(painter, vis, x, vis.y, vis.text_w)

    @staticmethod
    def _draw_item(painter, vis, x, y, w):
        painter.setFont(vis.font)
        rect = QRectF(x, y, w + 2, vis.line_h)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.setPen(QColor(0, 0, 0, 220))
        painter.drawText(rect.translated(1, 1), flags, vis.text)
        painter.setPen(vis.color)
        painter.drawText(rect, flags, vis.text)
