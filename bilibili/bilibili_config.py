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

# Protobuf 弹幕分段接口（需 WBI 签名），每段覆盖 DANMAKU_SEGMENT_SECONDS 秒
DANMAKU_SEG_URL = "https://api.bilibili.com/x/v2/dm/wbi/web/seg.so"
DANMAKU_SEGMENT_SECONDS = 360

# 视频分 P 信息接口 URL 模板（format 参数：bvid）
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist?bvid={bvid}"

# WBI 签名：nav 接口返回当日 img_key / sub_key，按重排表混淆为 mixin_key
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
# fmt: off
WBI_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
# fmt: on

# 视频 BV 号正则（从播放页 URL 提取）
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 分 P 参数正则
PAGE_RE = re.compile(r"[?&]p=(\d+)")

# CC 字幕文本容器 CSS 选择器
SUBTITLE_SELECTOR = ".bili-subtitle-x-subtitle-panel-text"

# ---------------------------------------------------------------------------
# 弹幕设置同步
# ---------------------------------------------------------------------------
# 从 bpx_player_profile.dmSetting 读取用户弹幕偏好
# 字段名：
#   dmarea     整数 0-100（即百分比，需 /100 转为 0.0-1.0）
#   fontsize   浮点倍率，1.0 = 100%
#   speedplus  速度倍率，1.0 = 适中
#   typeScroll / typeTop / typeBottom / typeColor / typeTopBottom  布尔值
DANMAKU_SETTINGS_JS = (
    "(function(){"
    "try{"
    "var p=JSON.parse(localStorage.getItem('bpx_player_profile')||'{}');"
    "var dm=p.dmSetting||{};"
    "return JSON.stringify({"
    "opacity:dm.opacity,"
    "dmarea:dm.dmarea,"
    "fontsize:dm.fontsize,"
    "speedplus:dm.speedplus,"
    "typeScroll:dm.typeScroll,"
    "typeTop:dm.typeTop,"
    "typeBottom:dm.typeBottom,"
    "typeColor:dm.typeColor,"
    "typeTopBottom:dm.typeTopBottom"
    "});"
    "}catch(e){return '';}"
    "})()"
)

# ---------------------------------------------------------------------------
# 字幕自动开启
# ---------------------------------------------------------------------------
_SUBTITLE_BTN = ".bpx-player-ctrl-subtitle"
_SUBTITLE_ITEM = ".bpx-player-ctrl-subtitle-language-item"

ENABLE_SUBTITLE_JS = (
    "(function(){"
    "function poll(fn){var c=0,t=setInterval(function(){if(fn()||++c>=10)clearInterval(t);},50);}"
    "try{"
    "var p=document.querySelector('.bpx-player-container');"
    "if(p)p.dispatchEvent(new MouseEvent('mousemove',{bubbles:true}));"
    "poll(function(){"
    f"var btn=document.querySelector('{_SUBTITLE_BTN}');"
    "if(!btn)return false;"
    "btn.click();"
    "poll(function(){"
    f"var items=document.querySelectorAll('{_SUBTITLE_ITEM}');"
    "if(!items||!items.length)return false;"
    "var best=null,fb=null;"
    "for(var i=0;i<items.length;i++){"
    "var t=(items[i].textContent||'').trim();"
    "if(!t||t==='\u5173\u95ed'||t.indexOf('\u8bbe\u7f6e')>=0)continue;"
    "if(t.indexOf('\u4e2d\u6587')>=0){"
    "if(t.toUpperCase().indexOf('AI')>=0){best=items[i];break;}"
    "if(!fb)fb=items[i];"
    "}"
    "}"
    "var target=best||fb;"
    "if(target)target.click();"
    "return true;"
    "});"
    "return true;"
    "});"
    "}catch(e){}"
    "})()"
)
