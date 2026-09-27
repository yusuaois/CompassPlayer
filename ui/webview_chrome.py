"""
webview_chrome.py
-----------------
注入到网页顶部的覆盖层（提示栏 + 地址栏 + 选择窗口/地图/弹幕/设置按钮），
以及 js_api 桥接对象

覆盖层通过 build_chrome_js() 生成自包含 JS，在每次页面加载完成后注入
"地图"/"弹幕"按钮默认隐藏，点"选择窗口"后才显示
"""

import json


def build_hint_text(hotkeys: dict) -> str:
    return (
        f"{hotkeys['play_pause']} 暂停/继续   "
        f"{hotkeys['seek_backward']}/{hotkeys['seek_forward']} 进度   "
        f"{hotkeys['opacity_down']}/{hotkeys['opacity_up']} 透明   "
        f"{hotkeys['toggle_visibility']} 隐/显   "
        f"{hotkeys['toggle_immersive']} 沉浸"
    )


_CHROME_HTML = """
<div style="display:flex;align-items:center;gap:6px;padding:2px 8px;background:#181818;color:#ddd;font:11px/1.4 sans-serif;box-sizing:border-box;width:100%;">
  <span id="__cc_hint__" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:0;"></span>
  <input id="__cc_url__" type="text" placeholder="输入网址后回车或点击跳转..."
         style="flex:1 1 auto;min-width:0;background:#2b2b2b;color:#00d2ff;border:1px solid #444;border-radius:3px;padding:1px 6px;font-size:11px;"/>
  <button id="__cc_go__"
          style="flex:0 0 auto;background:#00a1d6;color:#fff;border:none;border-radius:3px;padding:1px 8px;cursor:pointer;font-size:11px;">跳转</button>
  <button id="__cc_pick__" title="选择弹幕/指南针要贴上去的窗口"
          style="flex:0 0 auto;background:#3a3a3a;color:#fff;border:none;border-radius:3px;padding:1px 8px;cursor:pointer;font-size:11px;">选择窗口</button>
  <button id="__cc_map__" title="指南针映射：开关"
          style="display:none;flex:0 0 auto;background:#3a3a3a;color:#fff;border:none;border-radius:3px;padding:1px 8px;cursor:pointer;font-size:11px;">地图</button>
  <button id="__cc_danmaku__" title="弹幕映射：开关"
          style="display:none;flex:0 0 auto;background:#3a3a3a;color:#fff;border:none;border-radius:3px;padding:1px 8px;cursor:pointer;font-size:11px;">弹幕</button>
  <button id="__cc_settings__"
          style="flex:0 0 auto;background:#3a3a3a;color:#fff;border:none;border-radius:3px;padding:1px 6px;cursor:pointer;font-size:11px;">&#9881;</button>
</div>
"""

_CHROME_JS_TEMPLATE = """
(function () {
    if (window.__compassChrome) { window.__compassChrome.setHint(__HINT__); return; }
    var root = document.documentElement;
    var host = document.body || root;

    var bar = document.createElement('div');
    bar.id = '__compass_chrome__';
    bar.innerHTML = __HTML__;
    bar.style.cssText = 'position:fixed;top:0;left:0;width:100%;box-sizing:border-box;z-index:2147483647;';
    host.insertBefore(bar, host.firstChild);

    var barHeight = bar.offsetHeight || 22;
    var spacer = document.createElement('div');
    spacer.id = '__compass_spacer__';
    spacer.style.cssText = 'height:' + barHeight + 'px;';
    host.insertBefore(spacer, host.firstChild);

    var TOP_EPS = 4;
    var WIDE_RATIO = 0.5;
    var FORCE_SELECTORS = [];

    var origTop = new Map();

    function isOwn(el) {
        return !!(el.id && el.id.indexOf('__compass_') === 0);
    }
    function currentTopPx(el) {
        var t = parseFloat(window.getComputedStyle(el).top);
        return isNaN(t) ? el.getBoundingClientRect().top : t;
    }
    function looksLikeTopBar(el) {
        var tag = el.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK' ||
            tag === 'META' || tag === 'TITLE') { return false; }
        var cs = window.getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'sticky') { return false; }
        if (cs.display === 'none' || cs.visibility === 'hidden') { return false; }
        var rect = el.getBoundingClientRect();
        if (rect.width < window.innerWidth * WIDE_RATIO) { return false; }
        if (rect.top < -TOP_EPS || rect.top > barHeight + TOP_EPS) { return false; }
        return true;
    }
    function matchesForce(el) {
        for (var i = 0; i < FORCE_SELECTORS.length; i++) {
            try { if (el.matches && el.matches(FORCE_SELECTORS[i])) { return true; } } catch (e) {}
        }
        return false;
    }
    function isCandidate(el) {
        if (!el || el.nodeType !== 1 || isOwn(el)) { return false; }
        return looksLikeTopBar(el) || matchesForce(el);
    }
    function applyPush(el, base) {
        try {
            el.style.setProperty('--compass-orig-top', base + 'px');
            el.style.setProperty('top', 'calc(var(--compass-orig-top) + var(--compass-bar-h))', 'important');
        } catch (e) {}
    }
    function track(el) {
        if (origTop.has(el)) { return; }
        var base = currentTopPx(el);
        origTop.set(el, base);
        applyPush(el, base);
    }
    function discover(node) {
        try {
            if (!node || node.nodeType !== 1) { return; }
            if (isCandidate(node)) { track(node); }
            if (node.querySelectorAll) {
                var all = node.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    if (isCandidate(all[i])) { track(all[i]); }
                }
            }
        } catch (e) {}
    }
    function reconcile() {
        try {
            origTop.forEach(function (base, el) {
                if (!root.contains(el)) { origTop.delete(el); return; }
                var expectTop = 'calc(var(--compass-orig-top) + var(--compass-bar-h))';
                if (el.style.getPropertyValue('top') !== expectTop ||
                    el.style.getPropertyValue('--compass-orig-top') !== base + 'px') {
                    applyPush(el, base);
                }
            });
        } catch (e) {}
    }

    root.style.setProperty('--compass-bar-h', barHeight + 'px');
    discover(host);

    var pendingRoots = [];
    var scanScheduled = false;
    function scheduleScan(node) {
        pendingRoots.push(node);
        if (scanScheduled) { return; }
        scanScheduled = true;
        requestAnimationFrame(function () {
            scanScheduled = false;
            var roots = pendingRoots; pendingRoots = [];
            for (var i = 0; i < roots.length; i++) { discover(roots[i]); }
            reconcile();
        });
    }
    var mo = new MutationObserver(function (mutations) {
        for (var i = 0; i < mutations.length; i++) {
            var m = mutations[i];
            if (m.type === 'childList') {
                for (var j = 0; j < m.addedNodes.length; j++) { scheduleScan(m.addedNodes[j]); }
            } else if (m.type === 'attributes' && m.target && m.target.nodeType === 1) {
                scheduleScan(m.target);
            }
        }
    });
    mo.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
    setInterval(reconcile, 1500);

    var hintEl = document.getElementById('__cc_hint__');
    var urlEl = document.getElementById('__cc_url__');
    hintEl.textContent = __HINT__;
    function go() {
        var u = urlEl.value.trim();
        if (u) { window.pywebview.api.navigate(u); }
    }
    document.getElementById('__cc_go__').addEventListener('click', go);
    urlEl.addEventListener('keydown', function (e) { if (e.key === 'Enter') { go(); } });
    document.getElementById('__cc_settings__').addEventListener('click', function () {
        window.pywebview.api.open_settings();
    });
    var pickBtn = document.getElementById('__cc_pick__');
    var mapBtn = document.getElementById('__cc_map__');
    var danmakuBtn = document.getElementById('__cc_danmaku__');
    pickBtn.addEventListener('click', function () {
        window.pywebview.api.pick_window();
    });
    mapBtn.addEventListener('click', function () {
        window.pywebview.api.toggle_map_mapping();
    });
    danmakuBtn.addEventListener('click', function () {
        window.pywebview.api.toggle_danmaku_mapping();
    });
    window.__compassChrome = {
        hide: function () {
            bar.style.display = 'none'; spacer.style.display = 'none';
            root.style.setProperty('--compass-bar-h', '0px');
        },
        show: function () {
            bar.style.display = 'block'; spacer.style.display = 'block';
            root.style.setProperty('--compass-bar-h', barHeight + 'px');
        },
        setHint: function (t) { hintEl.textContent = t; },
        setUrl: function (u) { urlEl.value = u; },
        setWindowPicked: function (picked) {
            mapBtn.style.display = picked ? 'block' : 'none';
            danmakuBtn.style.display = picked ? 'block' : 'none';
            if (!picked) {
                mapBtn.style.background = '#3a3a3a';
                danmakuBtn.style.background = '#3a3a3a';
            }
        },
        setMapActive: function (active) {
            mapBtn.style.background = active ? '#00a1d6' : '#3a3a3a';
        },
        setDanmakuActive: function (active) {
            danmakuBtn.style.background = active ? '#00a1d6' : '#3a3a3a';
        }
    };
    document.addEventListener('click', function (e) {
        var a = e.target && e.target.closest ? e.target.closest('a') : null;
        if (a && a.getAttribute('target') === '_blank') {
            e.preventDefault();
            if (a.href) { window.location.href = a.href; }
        }
    }, true);
    window.open = function (u) { if (u) { window.location.href = u; } return null; };
})();
"""


def build_chrome_js(hint_text: str) -> str:
    return _CHROME_JS_TEMPLATE.replace("__HINT__", json.dumps(hint_text)).replace(
        "__HTML__", json.dumps(_CHROME_HTML)
    )


class Api:
    """js_api：JS 通过 window.pywebview.api.* 调用这里的公有方法

    回调运行在 pywebview 的 WebView2 线程上，仅转发为 bridge 的 Qt 信号
    """

    def __init__(self, bridge_getter):
        self._bridge = bridge_getter

    def navigate(self, url):
        b = self._bridge()
        if b is not None:
            b.navigate_requested.emit(url)

    def open_settings(self):
        b = self._bridge()
        if b is not None:
            b.settings_requested.emit()

    def toggle_immersive(self):
        b = self._bridge()
        if b is not None:
            b.immersive_requested.emit()

    def pick_window(self):
        b = self._bridge()
        if b is not None:
            b.pick_window_requested.emit()

    def toggle_map_mapping(self):
        b = self._bridge()
        if b is not None:
            b.map_toggle_requested.emit()

    def toggle_danmaku_mapping(self):
        b = self._bridge()
        if b is not None:
            b.danmaku_toggle_requested.emit()
