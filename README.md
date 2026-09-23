# CompassPlayer

置顶悬浮的 B 站浏览器 + 地图信标指南针叠加层。

基于 **Microsoft Edge WebView2**（通过 `pywebview` 驱动）。WebView2 内置
H.264/HEVC/AAC 解码器，因此能正常播放 B 站视频（QtWebEngine/PySide6/PyQt6
自带的 Chromium 因缺专利编解码器而无法播放 B 站）。程序在页面顶部注入一个工具栏
用于导航，并监听 B 站 CC 字幕中的方向词，驱动一个透明指南针悬浮窗指向对应方位。

## 运行环境

- **仅支持 Windows**（依赖 WebView2 运行时与 `keyboard` 全局热键）。
- Python 3.10+（开发环境为 3.12）。
- WebView2 Runtime：Windows 11 已内置；Windows 10 若缺失请安装
  [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)。

## 安装依赖

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

> **在游戏内使用时请以管理员身份运行**：很多游戏以更高权限运行，非管理员进程的
> 全局键盘钩子无法监听到游戏前台时的按键。程序启动时会检测，非管理员时打印提示。

首次运行会在同目录生成 `config.json` 与 `webview_profile/`（WebView2 用户数据，
含 Cookie / 登录态）。

## 功能

- **B 站浏览器**：顶部注入栏（地址栏跳转、设置按钮、快捷键提示），拦截
  `target="_blank"` 使其在当前窗口打开；窗口置顶、透明度、沉浸模式。
- **全局热键**：播放/暂停、快进快退、透明度调节、窗口显隐、沉浸模式。
- **字幕 → 指南针**：解析 B 站 CC 字幕中的方向词（东/南/西/北/东南/…），驱动透明
  指南针悬浮窗指向对应方位。
- **记住上次页面**：关闭后重开会恢复到上次浏览的页面。

## 默认快捷键（可在设置弹窗中自定义）

| 按键 | 功能 |
|---|---|
| \`（反引号，不带 Shift） | 播放 / 暂停 |
| 5 / 6 | 快退 / 快进（默认 ±5 秒） |
| 7 / 8 | 降低 / 提高窗口透明度 |
| 9 | 显示 / 隐藏窗口 |
| 0 | 切换沉浸模式 |

打开设置：点击注入栏右侧的 ⚙ 按钮。

## 指南针悬浮窗

- 左键单击：展开 / 收起（完整十字 ↔ 小圆点）。
- 右键单击：进入/退出“对齐编辑模式”（红色虚线框 + 四角手柄），拖动主体移动、
  拖动手柄缩放，退出时自动保存。
- 方向标记默认使用 `assets/pictures/marker.svg`（SVG，可替换为 PNG 或自己的图）。

## 常见问题

- **热键没反应（尤其游戏内）**：以管理员身份运行。
- **抓不到字幕 / 指南针不转**：B 站 DOM 更新了。按 F12 查看字幕元素的 class，
  更新 `subtitle_parser.py` 里的 `SUBTITLE_SELECTORS`。
- **注入栏与 B 站顶栏重叠**：B 站改版后固定顶栏结构变了，`webview_chrome.py` 里
  注入时“下移固定顶栏”的逻辑可能需要同步调整。

## 项目结构

```
CompassPlayer/
├── main.py             # 入口：WebView2 主线程 + Qt 后台线程（指南针/热键/字幕）
├── webview_chrome.py   # 注入页面的顶部工具栏 + js_api 桥接
├── config.py           # 默认配置（含版本号）、JSON 加载/保存
├── hotkey_manager.py   # 全局热键（keyboard.hook）
├── subtitle_parser.py  # 字幕轮询 + 方向词解析
├── beacon_overlay.py   # 透明指南针悬浮窗
├── settings_dialog.py  # 按键设置弹窗
├── assets/pictures/    # 方向标记图片
├── requirements.txt
└── .github/workflows/  # 构建发布
```

## 构建发布（GitHub Actions）

手动触发（`workflow_dispatch`）或推送 `v*.*.*` 标签即可：用 PyInstaller 打包为
`CompassPlayer.exe`，连同 `assets/` 压缩为 RAR 并发布 Release。

- **版本号**：从 `config.py` 的 `version` 字段读取，发布时自动加 `v` 前缀。
  发版前请先更新该字段。
- **Release 说明**：自动生成“自上上次发布以来的变更”（GitHub 自动 changelog）。
