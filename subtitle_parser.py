"""
subtitle_parser.py
-------------------
负责：
1. parse_direction()：把字幕文本中的方向词解析为罗盘角度（0°=正北，顺时针）。
2. SubtitlePoller：用 QTimer 周期性地通过 run_js() 直接查询 B 站 CC 字幕元素
   的当前文本，解析出方向词后发出 direction_detected 信号。

注意：B 站页面 DOM 结构可能随版本更新变化。SUBTITLE_SELECTOR 是当前的字幕容器
class，如果实际运行时抓不到字幕，请打开开发者工具（F12），在字幕出现时查看
真实 class 并更新此常量。
"""

from PySide6.QtCore import QObject, QTimer, Signal

# 当前 B 站 CC 字幕文本所在元素的 class（B 站改版后需同步更新）
SUBTITLE_SELECTOR = ".bili-subtitle-x-subtitle-panel-text"

# ---------------------------------------------------------------------------
# 方向词 -> 罗盘角度（0°=正北，顺时针）。键按长度从长到短排序后匹配，
# 确保"东南"等组合方向优先于单字"东"被识别，避免误判。
# ---------------------------------------------------------------------------
_DIRECTION_ANGLES = {
    "东北": 45,
    "东南": 135,
    "西南": 225,
    "西北": 315,
    "正东": 90,
    "正南": 180,
    "正西": 270,
    "正北": 0,
    "东": 90,
    "南": 180,
    "西": 270,
    "北": 0,
}
_SORTED_DIRECTION_KEYS = sorted(_DIRECTION_ANGLES.keys(), key=len, reverse=True)


def parse_direction(text: str):
    """在文本中查找方向词，返回对应罗盘角度（int，0~359）；未找到返回 None。"""
    if not text:
        return None
    for key in _SORTED_DIRECTION_KEYS:
        if key in text:
            return _DIRECTION_ANGLES[key]
    return None


# Python 端每次轮询时直接查询字幕元素当前文本（不依赖 MutationObserver，更稳；
# 即便 B 站替换字幕节点也能读到）。
_READ_SUBTITLE_JS = (
    "(function(){var el=document.querySelector('" + SUBTITLE_SELECTOR + "');"
    "return el ? (el.innerText||el.textContent||'').trim() : '';})()"
)


class SubtitlePoller(QObject):
    """周期性轮询页面里暂存的字幕文本，解析出方向后发出信号。"""

    direction_detected = Signal(int)  # 解析到新方向时发出（罗盘角度 0~359）
    subtitle_updated = Signal(str)  # 原始字幕文本变化时发出，便于调试/扩展

    def __init__(self, run_js, interval_ms: int = 500):
        super().__init__()
        self._run_js = run_js
        self._last_text = ""
        self._last_angle = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(interval_ms)

    def _poll(self):
        # run_js 约定为“不抛异常、失败返回 None”，无需在此兜底
        self._on_text_read(self._run_js(_READ_SUBTITLE_JS))

    def _on_text_read(self, text):
        text = (text or "").strip()
        if not text or text == self._last_text:
            return
        self._last_text = text
        self.subtitle_updated.emit(text)

        angle = parse_direction(text)
        if angle is not None and angle != self._last_angle:
            self._last_angle = angle
            self.direction_detected.emit(angle)
