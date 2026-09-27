"""
subtitle_parser.py
-------------------
1. parse_direction()：把字幕文本中的方向词解析为罗盘角度（0°=正北，顺时针）
2. SubtitlePoller：用 QTimer 周期性查询 B 站 CC 字幕元素的当前文本，
   解析出方向词后发出 direction_detected 信号
"""

from PySide6.QtCore import QObject, QTimer, Signal

from bilibili import bilibili_config

# 方向词 -> 罗盘角度（0°=正北，顺时针），按键长从长到短排序，确保组合方向
# （如"东南"）优先于单字方向（"东"）匹配，避免误判
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

_READ_SUBTITLE_JS = (
    "(function(){var el=document.querySelector('"
    + bilibili_config.SUBTITLE_SELECTOR
    + "');return el ? (el.innerText||el.textContent||'').trim() : '';})()"
)


def parse_direction(text: str):
    """在文本中查找方向词，返回罗盘角度（0~359）；未找到返回 None"""
    if not text:
        return None
    for key in _SORTED_DIRECTION_KEYS:
        if key in text:
            return _DIRECTION_ANGLES[key]
    return None


class SubtitlePoller(QObject):
    """周期性轮询页面字幕文本，解析方向词并发出信号"""

    direction_detected = Signal(int)
    subtitle_updated = Signal(str)

    def __init__(self, run_js, interval_ms: int = 500):
        super().__init__()
        self._run_js = run_js
        self._last_text = ""
        self._last_angle = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(interval_ms)

    def _poll(self):
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
