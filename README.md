# CompassPlayer 🧭

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)
![UI: PySide6](https://img.shields.io/badge/UI-PySide6-41CD52?logo=qt&logoColor=white)
![WebView: Edge WebView2](https://img.shields.io/badge/WebView-Edge%20WebView2-0078D7?logo=microsoftedge&logoColor=white)

**把 B 站攻略悬浮在游戏窗口上方，让视频里的方向提示和弹幕走进游戏画面。**

CompassPlayer 是一款基于 Python、PySide6 和 `pywebview`（EdgeChromium）的 Windows 桌面工具：它是一个置顶悬浮的 B 站浏览器，也能将视频 CC 字幕中的方向词映射为指南针指示，并把弹幕叠加到选定的游戏窗口上。

## ✨ 特性亮点（Features）

- **边玩边看**：基于 Microsoft Edge WebView2 播放 B 站视频，支持置顶、透明度调节和沉浸模式。
- **字幕变指南针**：识别 B 站 CC 字幕中的方向词，在目标窗口上实时显示对应方位。
- **纯 Python 拉取弹幕**：后端直连 B 站 WBI 签名 Protobuf 分段接口，不依赖不稳定的前端 XHR 拦截；沿用浏览器登录 Cookie，覆盖整条视频时间轴。
- **面向高密度弹幕的渲染**：使用预渲染 QPixmap、轨道碰撞检测和密度限制，减少弹幕重叠与卡顿。
- **事件驱动的窗口跟随**：通过 Win32 WinEventHook 响应目标窗口移动和前台切换，无需轮询窗口状态；弹幕层全程点击穿透。

## 🖥️ 运行环境

- **仅支持 Windows**：依赖 WebView2 运行时与 `keyboard` 全局热键。
- **Python 3.10+**：开发环境为 Python 3.12。
- **WebView2 Runtime**：Windows 11 已内置；Windows 10 若缺失，请安装 [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)。

WebView2 内置 H.264、HEVC 和 AAC 解码器，可正常播放 B 站视频。

## 🚀 安装与运行

```bash
pip install -r requirements.txt
python main.py
```

> **在游戏内使用时，请以管理员身份运行。** 游戏进程通常具有更高权限；非管理员进程的全局键盘钩子可能无法监听游戏处于前台时的按键。

首次运行会在同目录生成 `config.json` 和 `webview_profile/`（WebView2 用户数据）。

## 🧩 功能一览

- **B 站浏览器**：在页面顶部注入导航工具栏；拦截 `target="_blank"`，让链接在当前窗口打开；支持置顶、透明度调节和沉浸模式。
- **全局热键**：控制播放与暂停、快进与快退、窗口透明度与显隐，以及沉浸模式。
- **指南针 overlay**：解析 B 站 CC 字幕中的方向词，将透明指南针贴在选中的目标窗口上；随窗口移动、缩放，并在目标窗口失去焦点时自动隐藏。
- **弹幕 overlay**：拉取当前视频弹幕，按目标窗口尺寸独立排版，随视频进度同步显示，全程点击穿透。

## ⌨️ 默认快捷键

可在 **⚙ 设置** 中自定义。

| 按键              | 功能                      |
| ----------------- | ------------------------- |
| `` ` ``（反引号） | 播放 / 暂停               |
| 5 / 6             | 快退 / 快进（默认 ±5 秒） |
| 7 / 8             | 降低 / 提高窗口透明度     |
| 9                 | 显示 / 隐藏窗口           |
| 0                 | 切换沉浸模式              |

## 🧭 指南针使用指南

先点击 **「选择窗口」** 选中目标窗口，再点击 **「地图」** 开关。

- **左键单击指南针**：展开或收起，在完整十字与小圆点之间切换。
- **右键单击指南针**：切换对齐编辑模式。编辑时会显示红色虚线框和四角缩放手柄，拖动范围不会超出目标窗口；退出编辑模式时，位置会自动保存到 `config.json`。

位置按目标窗口的相对比例存储。因此，目标窗口移动，或换用不同分辨率的窗口后，指南针仍会保持原有的相对位置。

## 💬 弹幕 overlay 使用指南

选好目标窗口后，点击 **「弹幕」** 开关。

弹幕由 Python 端直连 B 站 Protobuf 分段接口获取：请求使用 WBI 签名，按 **6 分钟一段**并发拉取、合并，并沿用浏览器当前的登录 Cookie，覆盖整条视频时间轴。弹幕轨道由程序根据目标窗口尺寸排版，**不是原视频画面的像素级镜像**。

目前支持普通滚动、逆向滚动、顶部和底部弹幕；暂不支持高级弹幕与代码弹幕。

> **注意：**受 Windows 桌面合成限制，独占全屏游戏无法叠加 overlay。

弹幕显示参数可在 `config.json` 的 `danmaku` 节中调整，**修改后需重启生效**：

`tick_ms`、`cross_seconds`、`fixed_seconds`、`max_lanes`、`max_per_second`、`max_active`。

`display_area`、`font_scale`、`opacity`、`speed` 在开启弹幕时会被 B 站播放器的弹幕设置覆盖（并随配置一起保存），请在 B 站播放器里调整。

## 🏗️ 悬浮层架构

`overlay_manager.OverlayManager` 统一管理指南针和弹幕两个子 overlay：

- 维护当前挂载的目标窗口；子 overlay 只需实现统一的生命周期方法。
- 通过 `SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE)` 响应目标窗口的位置变化，**不轮询窗口位置**。
- 通过 `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` 响应前台切换，同时检查目标窗口是否仍然有效；窗口关闭也在这次检查中发现，无需单独轮询。
- `self.overlays` 的顺序就是从下到上的渲染顺序。新增 overlay 时，实现相同接口并按层级插入列表即可。

## ❓ 常见问题（FAQ）

| 现象                           | 处理方法                                                                                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 热键没反应，尤其是在游戏内     | 以管理员身份运行。                                                                                                                                        |
| 抓不到字幕，或指南针不转       | B 站 DOM 可能更新了。在 F12 中检查字幕元素的 class，并更新 `bilibili_config.py` 中的 `SUBTITLE_SELECTOR`。                                                |
| 注入工具栏与 B 站顶栏重叠      | B 站改版可能改变了固定顶栏结构；调整 `webview_chrome.py` 中「下移固定顶栏」的逻辑。                                                                       |
| 拿不到弹幕                     | 确认当前页面是带 BV 号的视频播放页。控制台报错包含服务端原始返回（如 `-352`），可据此更新 `bilibili_config.py` 中的 `DANMAKU_SEG_URL` / `WBI_MIXIN_TAB`。 |
| 弹幕位置与目标窗口对不上       | 如果目标窗口使用非标准自绘边框，可在 `window_picker.get_window_client_rect_on_screen` 中做针对性调整。                                                    |
| overlay 完全不跟随目标窗口移动 | 确认游戏不是独占全屏模式；否则检查 `window_picker.install_location_hook` 是否注册成功。                                                                   |

## 📁 项目结构

```text
CompassPlayer/
├── main.py                 # 入口：WebView2 主线程 + Qt 后台线程
├── config.py               # 默认配置、JSON 加载/保存
├── bilibili/
│   ├── bilibili_config.py  # B 站接口地址、请求头、CSS 选择器、页面 JS（改版时在此更新）
│   ├── bilibili_danmaku.py # 直连 Protobuf 分段接口拉取/解析 B 站视频弹幕（含 WBI 签名）
│   └── subtitle_parser.py  # 字幕轮询 + 方向词解析
├── overlay/
│   ├── overlay_manager.py  # 统一调度两个子 overlay，事件驱动跟随目标窗口
│   ├── beacon_overlay.py   # 透明指南针悬浮窗
│   └── danmaku_overlay.py  # 弹幕映射叠加层 + 视频播放进度轮询
├── ui/
│   ├── webview_chrome.py   # 注入页面的顶部工具栏 + js_api 桥接
│   ├── hotkey_manager.py   # 全局热键（keyboard.hook）
│   ├── settings_dialog.py  # 按键设置弹窗
│   └── window_picker.py    # 目标窗口枚举/选择弹窗 + 点击穿透 + WinEventHook
├── assets/pictures/        # 方向标记图片
└── requirements.txt
```

## 📦 构建与发布（GitHub Actions）

手动触发 `workflow_dispatch`，或推送 `v*.*.*` 标签，即可使用 PyInstaller 打包 `CompassPlayer.exe`，将其与 `assets/` 一同压缩为 RAR，并发布到 Release。

版本号从 `config.py` 的 `version` 字段读取。**发版前请先更新版本号。**
