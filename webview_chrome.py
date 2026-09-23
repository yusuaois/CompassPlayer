"""
webview_chrome.py
-----------------
注入到网页顶部的覆盖层（提示栏 + 地址栏 + 设置按钮），以及 js_api 桥接对象

覆盖层通过 build_chrome_js() 生成一段自包含 JS，在每次页面加载完成后注入；
注入后页面里会有一个 window.__compassChrome 对象，供 Python 端 hide/show/setHint/setUrl
同时重写 window.open 与拦截 <a target="_blank">，强制新链接在当前窗口打开
"""

import json


def build_hint_text(hotkeys: dict) -> str:
    """生成顶部提示栏里的快捷键提示文案"""
    return (
        f"{hotkeys['play_pause']} 暂停/继续   "
        f"{hotkeys['seek_backward']}/{hotkeys['seek_forward']} 进度   "
        f"{hotkeys['opacity_down']}/{hotkeys['opacity_up']} 透明   "
        f"{hotkeys['toggle_visibility']} 隐/显   "
        f"{hotkeys['toggle_immersive']} 沉浸"
    )


# 覆盖层 HTML（作为 JS 字符串注入，故用 HTML 实体避免引号/编码问题）
_CHROME_HTML = """
<div style="display:flex;align-items:center;gap:6px;padding:2px 8px;background:#181818;color:#ddd;font:11px/1.4 sans-serif;box-sizing:border-box;width:100%;">
  <span id="__cc_hint__" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:0;"></span>
  <input id="__cc_url__" type="text" placeholder="输入网址后回车或点击跳转..."
         style="flex:1 1 auto;min-width:0;background:#2b2b2b;color:#00d2ff;border:1px solid #444;border-radius:3px;padding:1px 6px;font-size:11px;"/>
  <button id="__cc_go__"
          style="flex:0 0 auto;background:#00a1d6;color:#fff;border:none;border-radius:3px;padding:1px 8px;cursor:pointer;font-size:11px;">跳转</button>
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

    var TOP_EPS = 4;         // 允许的顶部误差（像素），可调
    var WIDE_RATIO = 0.5;    // 至少占视口宽度这个比例才算"顶栏"，可调
    var FORCE_SELECTORS = []; // 通用识别兜不住时的手动补丁，见回复正文

    var origTop = new Map(); // element -> 原始 top 像素数值，纯 JS 内存持有

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
    setInterval(reconcile, 1500); // 低频轮询兜底

    // 以下与本次修复无关，逻辑保持原样：地址栏 / 设置按钮 / hide()-show() 改为单变量开关
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
        setUrl: function (u) { urlEl.value = u; }
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
    """把提示文案与覆盖层 HTML 注入到 JS 模板，返回可执行的 JS 代码"""
    return _CHROME_JS_TEMPLATE.replace("__HINT__", json.dumps(hint_text)).replace(
        "__HTML__", json.dumps(_CHROME_HTML)
    )


class Api:
    """js_api：JS 通过 window.pywebview.api.* 调用这里的公有方法

    这些回调运行在 pywebview 的 WinForms/WebView2 线程上，因此只负责把请求
    转发成 bridge 的 Qt 信号（PySide6 会自动排队投递到 Qt 后台线程）
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
