"""
subtitle_parser.py
-------------------
1. parse_direction()：把字幕文本中的方向词解析为罗盘角度（0°=正北，顺时针）
2. SubtitlePoller：用 QTimer 周期性查询 B 站 CC 字幕元素的当前文本，
   解析出方向词后发出 direction_detected 信号
"""

from PySide6.QtCore import QObject, QTimer, Signal

from bilibili import bilibili_config

# 方向词 -> 罗盘角度（0°=正北，顺时针）；组合方向须排在单字方向之前，
# 确保"东南"优先于"东"匹配
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

_READ_SUBTITLE_JS = (
    "(function(){var el=document.querySelector('"
    + bilibili_config.SUBTITLE_SELECTOR
    + "');return el ? (el.innerText||el.textContent||'').trim() : '';})()"
)


def parse_direction(text: str):
    """在文本中查找方向词，返回罗盘角度（0~359）；未找到返回 None"""
    for word, angle in _DIRECTION_ANGLES.items():
        if word in text:
            return angle
    return None


class SubtitlePoller(QObject):
    """周期性轮询页面字幕文本，解析方向词并发出信号"""

    direction_detected = Signal(int)

    def __init__(self, run_js, interval_ms: int = 500):
        super().__init__()
        self._run_js = run_js
        self._last_text = ""
        self._last_angle = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(interval_ms)

    def _poll(self):
        text = (self._run_js(_READ_SUBTITLE_JS) or "").strip()
        if not text or text == self._last_text:
            return
        self._last_text = text

        angle = parse_direction(text)
        if angle is not None and angle != self._last_angle:
            self._last_angle = angle
            self.direction_detected.emit(angle)
