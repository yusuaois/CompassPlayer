"""
bilibili_danmaku.py
--------------------
从 B 站弹幕接口拉取指定视频的弹幕列表（时间、类型、字号、颜色、文本）

接口使用 deflate 压缩；弹幕轨道（避让）不含在官方数据中，由
danmaku_overlay.py 按目标窗口尺寸独立排版，单视频弹幕上限约 500~8000 条
"""

import json
import urllib.error
import urllib.request
import zlib
from xml.etree import ElementTree

from bilibili import bilibili_config

# 弹幕类型（弹幕 XML p 属性第 2 项）
# 1/2/3 普通滚动  6 逆向滚动  5 顶部固定  4 底部固定
# 7/8/9（高级/代码/BAS）暂不支持
SCROLL_MODES = frozenset((1, 2, 3))
REVERSE_MODES = frozenset((6,))
BOTTOM_MODES = frozenset((4,))
TOP_MODES = frozenset((5,))
SUPPORTED_MODES = SCROLL_MODES | REVERSE_MODES | BOTTOM_MODES | TOP_MODES


class DanmakuItem:
    __slots__ = ("color", "font_size", "mode", "text", "time")

    def __init__(self, time_s, mode, font_size, color, text):
        self.time = time_s
        self.mode = mode
        self.font_size = font_size
        self.color = color
        self.text = text


def extract_bvid(url: str):
    m = bilibili_config.BVID_RE.search(url or "")
    return m.group(0) if m else None


def extract_page_number(url: str) -> int:
    m = bilibili_config.PAGE_RE.search(url or "")
    return int(m.group(1)) if m else 1


def _http_get(url: str, timeout=8) -> bytes:
    req = urllib.request.Request(url, headers=bilibili_config.REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_cid(bvid: str, page: int = 1, timeout=8) -> int:
    """返回指定视频指定分 P 的 cid"""
    url = bilibili_config.PAGELIST_URL.format(bvid=bvid)
    data = json.loads(_http_get(url, timeout=timeout).decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"pagelist 接口返回错误: {data.get('message')}")
    pages = data.get("data") or []
    if not pages:
        raise RuntimeError("pagelist 接口未返回分 P 信息")
    for item in pages:
        if item.get("page") == page:
            return int(item["cid"])
    return int(pages[0]["cid"])


def _deflate_decode(raw: bytes) -> bytes:
    """尝试多种 wbits 解压 deflate 数据（接口响应格式不固定）"""
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS, zlib.MAX_WBITS + 16):
        try:
            return zlib.decompress(raw, wbits)
        except zlib.error:
            continue
    return raw


def fetch_danmaku_xml(cid: int, timeout=8) -> bytes:
    last_err = None
    for url in (
        bilibili_config.DANMAKU_XML_URL.format(cid=cid),
        bilibili_config.DANMAKU_XML_FALLBACK_URL.format(cid=cid),
    ):
        try:
            return _deflate_decode(_http_get(url, timeout=timeout))
        except (urllib.error.URLError, OSError) as e:
            last_err = e
    raise last_err


def parse_danmaku_xml(xml_bytes: bytes):
    root = ElementTree.fromstring(xml_bytes)
    items = []
    for d in root.findall("d"):
        parts = (d.get("p") or "").split(",")
        if len(parts) < 4:
            continue
        try:
            time_s = float(parts[0])
            mode = int(parts[1])
            font_size = int(parts[2])
            color = int(parts[3])
        except ValueError:
            continue
        if mode not in SUPPORTED_MODES:
            continue
        items.append(DanmakuItem(time_s, mode, font_size, color, d.text or ""))
    items.sort(key=lambda it: it.time)
    return items


def fetch_danmaku_for_url(url: str, timeout=8):
    """从播放页 URL 拉取弹幕，返回按时间排序的 DanmakuItem 列表

    若当前页面不是 B 站视频播放页或网络/接口失败，抛出异常
    """
    bvid = extract_bvid(url)
    if not bvid:
        raise ValueError("当前页面不是 B 站视频播放页（未找到 BV 号）")
    page = extract_page_number(url)
    cid = fetch_cid(bvid, page, timeout=timeout)
    xml_bytes = fetch_danmaku_xml(cid, timeout=timeout)
    return parse_danmaku_xml(xml_bytes)
