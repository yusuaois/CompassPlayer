"""
bilibili_config.py
------------------
B 站网页 / 接口相关的硬编码常量，集中在此处便于 B 站改版时统一更新
"""

import re

# HTTP 请求头
REQUEST_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {"User-Agent": REQUEST_UA, "Referer": "https://www.bilibili.com"}

# 弹幕 XML 接口 URL 模板（format 参数：cid）
DANMAKU_XML_URL = "https://comment.bilibili.com/{cid}.xml"
DANMAKU_XML_FALLBACK_URL = "https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"

# 视频分 P 信息接口 URL 模板（format 参数：bvid）
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist?bvid={bvid}"

# 视频 BV 号正则（从播放页 URL 提取）
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 分 P 参数正则
PAGE_RE = re.compile(r"[?&]p=(\d+)")

# CC 字幕文本容器 CSS 选择器
SUBTITLE_SELECTOR = ".bili-subtitle-x-subtitle-panel-text"
