"""
bilibili_danmaku.py
--------------------
直连 B 站 Protobuf 弹幕分段接口 (seg.so)，拉取指定视频的全部弹幕。

【协议说明】
1. 依赖 WBI 签名机制（详见 bilibili_config.WBI_MIXIN_TAB）
2. 使用轻量级手写 Protobuf 解码器，跳过官方 protobuf 依赖库
   根据 B 站 dm.proto 规范：
   - DmSegMobileReply (Tag 1 -> elems: DanmakuElem)
   - DanmakuElem (Tag 2 -> progress, Tag 3 -> mode, Tag 4 -> font_size, Tag 5 -> color, Tag 7 -> content)
"""

import functools
import json
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from hashlib import md5
from operator import attrgetter
from pathlib import PurePosixPath

from bilibili import bilibili_config

# ---------------------------------------------------------------------------
# 弹幕类型与模式定义
# ---------------------------------------------------------------------------
SCROLL_MODES = frozenset((1, 2, 3))  # 普通滚动 / 渐隐
REVERSE_MODES = frozenset((6,))  # 逆向滚动
BOTTOM_MODES = frozenset((4,))  # 底部固定
TOP_MODES = frozenset((5,))  # 顶部固定
SUPPORTED_MODES = SCROLL_MODES | REVERSE_MODES | BOTTOM_MODES | TOP_MODES

_FETCH_WORKERS = 6  # 并发拉取线程数

# ---------------------------------------------------------------------------
# Protobuf Wire Types & Tag 定义（参考 B 站官方 dm.proto）
# ---------------------------------------------------------------------------
_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LENGTH_DELIMITED = 2
_WIRE_FIXED32 = 5

_REPLY_ELEMS = 1  # DmSegMobileReply.elems (repeated DanmakuElem)
_ELEM_PROGRESS = 2  # DanmakuElem.progress (毫秒)
_ELEM_MODE = 3  # DanmakuElem.mode (弹幕类型)
_ELEM_FONT_SIZE = 4  # DanmakuElem.fontsize (字号)
_ELEM_COLOR = 5  # DanmakuElem.color (RGB24颜色)
_ELEM_CONTENT = 7  # DanmakuElem.content (文本)


class DanmakuItem:
    """弹幕数据实体类，使用 __slots__ 优化上万条弹幕时的内存占用"""

    __slots__ = ("color", "font_size", "mode", "text", "time")

    def __init__(self, time_s: float, mode: int, font_size: int, color: int, text: str):
        self.time = time_s  # 视频内出现时间（秒）
        self.mode = mode  # 弹幕模式
        self.font_size = font_size  # 字号
        self.color = color  # 24bit RGB 颜色值
        self.text = text  # 文本内容


def extract_bvid(url: str) -> str | None:
    m = bilibili_config.BVID_RE.search(url or "")
    return m.group(0) if m else None


def extract_page_number(url: str) -> int:
    m = bilibili_config.PAGE_RE.search(url or "")
    return int(m.group(1)) if m else 1


def video_key(url: str) -> tuple[str, int] | None:
    """根据播放页 URL 提取 (bvid, p)，以此判断是否切换了视频/分 P"""
    bvid = extract_bvid(url)
    return (bvid, extract_page_number(url)) if bvid else None


# ---------------------------------------------------------------------------
# 轻量级 Protobuf 零依赖解码器
# ---------------------------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _iter_fields(buf: bytes, pos: int, end: int):
    """高效遍历 Protobuf 二进制流中的 (Tag, Value)"""
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        wire = tag & 7
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire == _WIRE_LENGTH_DELIMITED:
            size, pos = _read_varint(buf, pos)
            value = (pos, pos + size)
            pos += size
        elif wire == _WIRE_FIXED64:
            value, pos = None, pos + 8
        elif wire == _WIRE_FIXED32:
            value, pos = None, pos + 4
        else:
            raise ValueError(f"不支持的 Protobuf wire type: {wire}")
        if pos > end:
            raise ValueError("Protobuf 二进制数据异常截断")
        yield tag >> 3, value


def _parse_elem(buf: bytes, start: int, end: int) -> DanmakuItem:
    progress = mode = font_size = color = 0
    text = ""
    for field, value in _iter_fields(buf, start, end):
        if field == _ELEM_PROGRESS:
            progress = value
        elif field == _ELEM_MODE:
            mode = value
        elif field == _ELEM_FONT_SIZE:
            font_size = value
        elif field == _ELEM_COLOR:
            color = value
        elif field == _ELEM_CONTENT:
            text = buf[value[0] : value[1]].decode("utf-8", "replace")
    return DanmakuItem(progress / 1000.0, mode, font_size, color, text)


def parse_danmaku_segment_pb(data: bytes) -> list[DanmakuItem]:
    """解析单段 seg.so 二进制流，提取 DanmakuItem 列表"""
    items = []
    try:
        for field, span in _iter_fields(data, 0, len(data)):
            if field == _REPLY_ELEMS:
                item = _parse_elem(data, *span)
                if item.mode in SUPPORTED_MODES:
                    items.append(item)
    except (IndexError, TypeError):
        raise ValueError("弹幕 Protobuf 二进制解析失败") from None
    return items


# ---------------------------------------------------------------------------
# B 站 API 与 WBI 签名请求
# ---------------------------------------------------------------------------
def _http_get(url: str, cookie: str, timeout: float) -> bytes:
    headers = dict(bilibili_config.REQUEST_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_page(get_fn, bvid: str, page: int) -> tuple[int, int]:
    body = json.loads(get_fn(bilibili_config.PAGELIST_URL.format(bvid=bvid)))
    if body.get("code") != 0:
        raise RuntimeError(f"pagelist 接口响应异常: {body.get('message')}")
    pages = body.get("data") or []
    if not pages:
        raise RuntimeError("未获取到视频分 P 信息")
    info = next((p for p in pages if p["page"] == page), pages[0])
    return int(info["cid"]), int(info["duration"])


def _fetch_mixin_key(get_fn) -> str:
    body = json.loads(get_fn(bilibili_config.NAV_URL))
    wbi_img = (body.get("data") or {}).get("wbi_img")
    if not wbi_img:
        raise RuntimeError("WBI 密钥获取失败")
    raw = "".join(PurePosixPath(wbi_img[k]).stem for k in ("img_url", "sub_url"))
    return "".join(raw[i] for i in bilibili_config.WBI_MIXIN_TAB)[:32]


def _sign_wbi(params: dict, mixin_key: str) -> str:
    """计算 WBI 签名并返回完整 Query 字符串"""
    query = urllib.parse.urlencode(sorted({**params, "wts": int(time.time())}.items()))
    w_rid = md5((query + mixin_key).encode(), usedforsecurity=False).hexdigest()
    return f"{query}&w_rid={w_rid}"


def _fetch_segment(get_fn, cid: int, index: int, mixin_key: str) -> list[DanmakuItem]:
    query = _sign_wbi({"type": 1, "oid": cid, "segment_index": index}, mixin_key)
    body = get_fn(f"{bilibili_config.DANMAKU_SEG_URL}?{query}")
    if body.startswith(b"{"):  # API 拒绝时通常返回 JSON 错误信息
        reason = body[:200].decode("utf-8", "replace")
        raise RuntimeError(f"弹幕分段 {index} 被拒绝: {reason}")
    return parse_danmaku_segment_pb(body)


def fetch_danmaku_for_url(
    url: str, cookie: str = "", timeout: float = 8.0
) -> list[DanmakuItem]:
    """
    传入视频网页 URL 与浏览器 Cookie，并发拉取该视频全部分段弹幕
    """
    key = video_key(url)
    if key is None:
        raise ValueError("当前页面不是有效的 B 站视频播放页")

    get_fn = functools.partial(_http_get, cookie=cookie, timeout=timeout)

    pool = ThreadPoolExecutor(_FETCH_WORKERS)
    try:
        page_future = pool.submit(_fetch_page, get_fn, *key)
        mixin_key = _fetch_mixin_key(get_fn)
        cid, duration = page_future.result()

        # 按照 360 秒/段 计算总分段数
        seg_count = max(
            1, math.ceil(duration / bilibili_config.DANMAKU_SEGMENT_SECONDS)
        )
        segments = pool.map(
            lambda idx: _fetch_segment(get_fn, cid, idx, mixin_key),
            range(1, seg_count + 1),
        )
        items = [item for seg in segments for item in seg]
    finally:
        pool.shutdown(cancel_futures=True)

    items.sort(key=attrgetter("time"))
    return items
