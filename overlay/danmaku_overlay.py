"""
danmaku_overlay.py
-------------------
把当前 B 站视频的弹幕，按目标窗口尺寸独立排版后，浮动渲染在目标窗口上方

对外接口（由 OverlayManager 调用）：
  on_attach / on_target_moved / on_detach / set_enabled / on_focus_changed
全程点击穿透，不影响目标窗口的鼠标操作
"""

import bisect
import math
import time
import urllib.parse

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from bilibili import bilibili_danmaku
from ui.window_picker import set_click_through

SCROLL_MODES = bilibili_danmaku.SCROLL_MODES
REVERSE_MODES = bilibili_danmaku.REVERSE_MODES
BOTTOM_MODES = bilibili_danmaku.BOTTOM_MODES
TOP_MODES = bilibili_danmaku.TOP_MODES

_FONT_PX_BY_SIZE = {18: 16, 25: 20, 36: 28}  # B 站字号档位 -> 参考像素（720p 基准）
_REF_HEIGHT = 720
_LANE_PADDING = 6  # 轨道高度 = 字号像素 + 该值
_VIDEO_POLL_MS = 300
_SEEK_JUMP_THRESHOLD = 1.2  # 秒；轮询时间跳变超过此值视为拖动进度条
_MIN_LANE_GAP = 0.4  # 秒；同一轨道前一条弹幕尾部完全入场后，再间隔该时长才放下一条
_MAX_FIXED_SLOTS = 4  # 顶部 / 底部固定弹幕各自的最大行数
_SHADOW = QColor(0, 0, 0, 220)

_VIDEO_STATE_JS = (
    "(function(){var v=document.querySelector('bwp-video')||document.querySelector('video');"
    " if(!v) return '';"
    " return v.currentTime.toFixed(3)+'|'+(v.paused?0:1)+'|'"
    "+encodeURIComponent(document.location.href);})();"
)


def _map_bili_speedplus(raw) -> float:
    """
    将 B 站弹幕速度设置映射为浮点倍率
    0.4, 0.7, 1.0, 1.3, 1.6
    此处增加边界保护，防止异常值导致弹幕不动或飞出屏幕
    """
    try:
        return max(0.1, min(3.0, float(raw)))
    except (TypeError, ValueError):
        return 1.0


class VideoTimeSync(QObject):
    """周期性查询页面 <video> 的播放进度，顺带检测视频是否切换"""

    time_updated = Signal(float, bool)
    video_changed = Signal(str)

    def __init__(self, run_js, interval_ms: int = _VIDEO_POLL_MS):
        super().__init__()
        self._run_js = run_js
        self._interval_ms = interval_ms
        self._last_video_key = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self, known_key=None):
        self._last_video_key = known_key
        self._timer.start(self._interval_ms)

    def stop(self):
        self._timer.stop()

    def _poll(self):
        result = self._run_js(_VIDEO_STATE_JS)
        if not result:
            return
        try:
            t_str, playing_str, href_enc = result.split("|")
            current_time = float(t_str)
        except (ValueError, AttributeError):
            return
        href = urllib.parse.unquote(href_enc)

        key = bilibili_danmaku.video_key(href)
        if key and key != self._last_video_key:
            self._last_video_key = key
            self.video_changed.emit(href)
            return
        self.time_updated.emit(current_time, playing_str == "1")


class _Sprite:
    """一条正在显示的弹幕：文字与阴影在生成时预渲染为 pixmap，每帧只贴图"""

    __slots__ = ("expire_at", "pixmap", "reverse", "width", "x", "y")

    def __init__(self, pixmap, width, x, y, reverse=False, expire_at=0.0):
        self.pixmap = pixmap
        self.width = width
        self.x = x
        self.y = y
        self.reverse = reverse
        self.expire_at = expire_at


def _render_text(text, color, font, metrics, dpr):
    """把文字 + 1px 阴影画到透明 pixmap 上，返回 (pixmap, 逻辑宽度)"""
    w = metrics.horizontalAdvance(text) + 2
    h = metrics.height() + 1
    pixmap = QPixmap(math.ceil(w * dpr), math.ceil(h * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setFont(font)
    baseline = metrics.ascent()
    painter.setPen(_SHADOW)
    painter.drawText(QPointF(1, baseline + 1), text)
    painter.setPen(color)
    painter.drawText(QPointF(0, baseline), text)
    painter.end()
    return pixmap, w


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

        self._density_second = None
        self._density_count = 0

        # 各轨道 / 固定槽位"可再次放入弹幕"的视频时间
        self._scroll_lanes = []
        self._reverse_lanes = []
        self._top_slots = []
        self._bottom_slots = []

        self._scrolling = []
        self._fixed = []

        self._font_cache = {}

        self._last_video_time = 0.0
        self._is_playing = False
        self._attached = False
        self._enabled = False
        self._has_focus = False
        self._click_through_applied = False
        self._last_tick_ts = None

        # 由 apply_bili_settings() 按用户的 B 站设置收窄
        self._enabled_modes = frozenset(bilibili_danmaku.SUPPORTED_MODES)
        self._color_enabled = True

        self._tick_timer = QTimer(self)
        self._tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick_timer.timeout.connect(self._tick)
        
        self._last_settings_signature = None

    # ------------------------------------------------------------------
    # 由 OverlayManager 调用的生命周期接口
    # ------------------------------------------------------------------
    def on_attach(self, rect):
        self._attached = True
        self.setGeometry(*rect)
        self._reset_layout()

    def on_target_moved(self, rect):
        if not self._attached:
            return
        if tuple(rect) != (self.x(), self.y(), self.width(), self.height()):
            size_changed = (rect[2], rect[3]) != (self.width(), self.height())
            self.setGeometry(*rect)
            if size_changed:
                self._font_cache.clear()
                self._reflow_from(self._last_video_time)

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
        if self._attached and self._enabled and self._has_focus:
            # 丢弃隐藏期间积压的弹幕，从当前视频进度重新开始
            self._reflow_from(self._last_video_time)
            self.show()
            if not self._click_through_applied:
                set_click_through(int(self.winId()), True)
                self._click_through_applied = True
            self.raise_()
            self._last_tick_ts = time.monotonic()
            self._tick_timer.start(int(self._dcfg()["tick_ms"]))
        else:
            self._tick_timer.stop()
            self._scrolling.clear()
            self._fixed.clear()
            self.hide()

    # ------------------------------------------------------------------
    # 同步 B 站弹幕设置
    # ------------------------------------------------------------------
    def apply_bili_settings(self, settings: dict):
        """将 B 站 localStorage 中的弹幕偏好（bpx_player_profile.dmSetting）应用到本 overlay"""
        if not settings:
            return
        
        signature = str(sorted(settings.items()))
        if signature == self._last_settings_signature:
            return
        self._last_settings_signature = signature
        
        dcfg = self._dcfg()

        def put(src_key, dst_key, convert):
            raw = settings.get(src_key)
            if raw is None:
                return
            try:
                dcfg[dst_key] = convert(float(raw))
            except (TypeError, ValueError):
                pass

        put("opacity", "opacity", lambda v: max(0.1, min(1.0, v)))
        put("dmarea", "display_area", lambda v: max(0.05, min(1.0, v / 100)))
        put("fontsize", "font_scale", lambda v: max(0.5, min(2.0, v)))
        put("speedplus", "speed", _map_bili_speedplus)

        # typeTopBottom 是顶 / 底弹幕的总开关，typeTop / typeBottom 是各自的开关
        both = settings.get("typeTopBottom") is not False
        modes = set(bilibili_danmaku.SUPPORTED_MODES)
        if settings.get("typeScroll") is False:
            modes -= SCROLL_MODES | REVERSE_MODES
        if not (both and settings.get("typeTop") is not False):
            modes -= TOP_MODES
        if not (both and settings.get("typeBottom") is not False):
            modes -= BOTTOM_MODES
        self._enabled_modes = frozenset(modes)
        self._color_enabled = settings.get("typeColor") is not False

        self._font_cache.clear()
        self._reflow_from(self._last_video_time)

    # ------------------------------------------------------------------
    # 弹幕数据与播放进度
    # ------------------------------------------------------------------
    def load_items(self, items):
        self._items = items
        self._reflow_from(self._last_video_time)

    def on_time_update(self, current_time: float, is_playing: bool):
        if abs(current_time - self._last_video_time) > _SEEK_JUMP_THRESHOLD:
            self._reflow_from(current_time)
        else:
            self._spawn_due(current_time)
        self._last_video_time = current_time
        self._is_playing = is_playing

    # ------------------------------------------------------------------
    # 排版
    # ------------------------------------------------------------------
    def _dcfg(self):
        return self.config["danmaku"]

    def _max_active(self):
        try:
            return max(1, int(self._dcfg().get("max_active", 18)))
        except (TypeError, ValueError):
            return 18

    def _font_px(self, bili_font_size):
        base = _FONT_PX_BY_SIZE.get(bili_font_size, 20)
        scale = max(self.height(), 1) / _REF_HEIGHT * float(self._dcfg()["font_scale"])
        return max(10, round(base * scale))

    def _font(self, px):
        cached = self._font_cache.get(px)
        if cached is None:
            font = QFont()
            font.setPixelSize(px)
            font.setBold(True)
            cached = self._font_cache[px] = (font, QFontMetrics(font))
        return cached

    def _lane_height(self):
        return self._font_px(25) + _LANE_PADDING

    def _scroll_speed(self):
        """滚动速度（逻辑像素 / 秒）：所有滚动弹幕同速，同轨道内不会追尾"""
        speed = max(0.1, float(self._dcfg()["speed"]))
        cross = max(1.0, float(self._dcfg()["cross_seconds"]) / speed)
        return max(self.width(), 1) / cross

    def _reset_layout(self):
        area = max(0.05, min(1.0, float(self._dcfg()["display_area"])))
        usable = max(1, round(self.height() * area))
        lanes = max(
            1, min(int(self._dcfg()["max_lanes"]), usable // self._lane_height())
        )
        slots = min(_MAX_FIXED_SLOTS, lanes)
        self._scroll_lanes = [-math.inf] * lanes
        self._reverse_lanes = [-math.inf] * lanes
        self._top_slots = [-math.inf] * slots
        self._bottom_slots = [-math.inf] * slots

    def _reflow_from(self, video_time):
        self._scrolling.clear()
        self._fixed.clear()
        self._reset_layout()
        self._density_second = None
        self._density_count = 0
        self._next_index = bisect.bisect_left(
            self._items, video_time, key=lambda it: it.time
        )
        self.update()

    def _spawn_due(self, now):
        items = self._items

        try:
            max_per_second = max(
                1,
                int(self._dcfg().get("max_per_second", 3)),
            )
        except (TypeError, ValueError):
            max_per_second = 3

        while self._next_index < len(items) and items[self._next_index].time <= now:
            item = items[self._next_index]
            self._next_index += 1

            second = int(item.time)

            if second != self._density_second:
                self._density_second = second
                self._density_count = 0

            if self._density_count >= max_per_second:
                continue

            # 超过这个时间才被轮询到，已经没有展示价值，避免集中补发。
            if now - item.time > 0.5:
                continue

            if self._spawn_one(
                item,
                event_time=item.time,
                video_now=now,
            ):
                self._density_count += 1

    @staticmethod
    def _take_free(slots, now):
        """返回首个空闲轨道的下标；全部被占用返回 None（该条弹幕丢弃）"""
        for i, free_at in enumerate(slots):
            if free_at <= now:
                return i
        return None

    def _spawn_one(self, item, event_time, video_now):
        if not item.text:
            return False

        if item.mode not in self._enabled_modes:
            return False

        if not self._color_enabled and item.color != 0xFFFFFF:
            return False

        # 无论每秒限制如何，始终保证同屏活跃弹幕总数有硬上限。
        if len(self._scrolling) + len(self._fixed) >= self._max_active():
            return False

        is_scroll = item.mode in SCROLL_MODES or item.mode in REVERSE_MODES

        if is_scroll:
            reverse = item.mode in REVERSE_MODES
            slots = self._reverse_lanes if reverse else self._scroll_lanes
        else:
            slots = self._top_slots if item.mode in TOP_MODES else self._bottom_slots

        # 轨道分配使用“原始弹幕时间”，而不是轮询到的当前时间。
        # 这样同一批迟到被轮询到的弹幕仍按视频时间正确避让。
        index = self._take_free(slots, event_time)
        if index is None:
            return False

        font, metrics = self._font(self._font_px(item.font_size))
        color = QColor(
            (item.color >> 16) & 0xFF,
            (item.color >> 8) & 0xFF,
            item.color & 0xFF,
        )

        pixmap, width = _render_text(
            item.text,
            color,
            font,
            metrics,
            self.devicePixelRatioF(),
        )

        lane_h = self._lane_height()

        if is_scroll:
            speed_px = self._scroll_speed()

            # VideoTimeSync 是周期性轮询，弹幕实际出现时可能已过去 0~300ms。
            # 根据视频时间差补偿初始 X，避免所有弹幕固定晚半拍出现。
            late_seconds = max(0.0, video_now - event_time)

            if reverse:
                x = -float(width) + speed_px * late_seconds
                if x >= self.width():
                    return False
            else:
                x = float(self.width()) - speed_px * late_seconds
                if x + width <= 0:
                    return False

            self._scrolling.append(
                _Sprite(
                    pixmap,
                    width,
                    x,
                    index * lane_h + 2,
                    reverse=reverse,
                )
            )

            # 前一条弹幕的尾部完全进入屏幕后，才能允许下一条进入相同轨道。
            slots[index] = event_time + width / max(speed_px, 1.0) + _MIN_LANE_GAP

        else:
            expire_at = event_time + float(self._dcfg()["fixed_seconds"])

            # 固定弹幕如果已经过期，不再补发。
            if expire_at <= video_now:
                return False

            if item.mode in TOP_MODES:
                y = index * lane_h + 4
            else:
                y = self.height() - (index + 1) * lane_h - 4

            x = (self.width() - width) / 2

            self._fixed.append(
                _Sprite(
                    pixmap,
                    width,
                    x,
                    y,
                    expire_at=expire_at,
                )
            )
            slots[index] = expire_at

        return True

    # ------------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        dt = now - (self._last_tick_ts or now)
        self._last_tick_ts = now
        if not self._is_playing or dt <= 0 or not (self._scrolling or self._fixed):
            return

        # 分层透明窗口每次重绘都会整窗提交，局部脏区没有收益，直接整窗 update
        step = self._scroll_speed() * dt
        w = self.width()
        for s in self._scrolling:
            s.x += step if s.reverse else -step
        self._scrolling = [s for s in self._scrolling if -s.width <= s.x <= w]
        self._fixed = [s for s in self._fixed if s.expire_at > self._last_video_time]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(max(0.1, min(1.0, float(self._dcfg()["opacity"]))))
        for s in self._scrolling:
            painter.drawPixmap(QPointF(s.x, s.y), s.pixmap)
        for s in self._fixed:
            painter.drawPixmap(QPointF(s.x, s.y), s.pixmap)
