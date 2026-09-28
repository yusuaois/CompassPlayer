# CompassPlayer

置顶悬浮的 B 站浏览器 + 地图信标指南针叠加层。

基于 **Microsoft Edge WebView2**（通过 `pywebview` 驱动）。WebView2 内置
H.264/HEVC/AAC 解码器，因此能正常播放 B 站视频。程序在页面顶部注入工具栏
用于导航，并监听 B 站 CC 字幕中的方向词，驱动透明指南针悬浮窗指向对应方位。

## 运行环境

- **仅支持 Windows**（依赖 WebView2 运行时与 `keyboard` 全局热键）
- Python 3.10+（开发环境为 3.12）
- WebView2 Runtime：Windows 11 已内置；Windows 10 若缺失请安装
  [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)

## 安装与运行

```bash
pip install -r requirements.txt
python main.py
```

> **游戏内使用请以管理员身份运行**：游戏进程通常以更高权限运行，非管理员
> 的全局键盘钩子无法监听到游戏前台时的按键。

首次运行会在同目录生成 `config.json` 与 `webview_profile/`（WebView2 用户数据）。

## 功能

- **B 站浏览器**：顶部注入栏、拦截 `target="_blank"` 在当前窗口打开、置顶/透明度/沉浸模式
- **全局热键**：播放/暂停、快进快退、透明度调节、窗口显隐、沉浸模式
- **指南针 overlay**：选中目标窗口后，贴在该窗口上的透明指南针——解析 B 站 CC
  字幕中的方向词并实时指向对应方位；跟随目标窗口移动/缩放，目标窗口失去焦点时自动隐藏
- **弹幕 overlay**：拉取当前视频弹幕，按目标窗口尺寸排版后浮动显示，随视频播放
  进度同步，全程点击穿透

## 默认快捷键（可在 ⚙ 设置中自定义）

| 按键              | 功能                      |
| ----------------- | ------------------------- |
| `` ` ``（反引号） | 播放 / 暂停               |
| 5 / 6             | 快退 / 快进（默认 ±5 秒） |
| 7 / 8             | 降低 / 提高窗口透明度     |
| 9                 | 显示 / 隐藏窗口           |
| 0                 | 切换沉浸模式              |

## 指南针使用

先点"选择窗口"选中目标窗口，再点"地图"开关。

- **左键单击**：展开 / 收起（完整十字 ↔ 小圆点）
- **右键单击**：切换对齐编辑模式（红色虚线框 + 四角缩放手柄，拖不出目标窗口）；
  退出编辑模式时自动保存相对位置到 config.json

位置以相对目标窗口的比例存储，目标窗口移动或换用不同分辨率的窗口后，
指南针仍在原相对位置。

## 弹幕 overlay

选好目标窗口后点"弹幕"开关。弹幕由 Python 端直连 B 站 Protobuf 分段接口
（WBI 签名，6 分钟一段）并发拉取后合并，请求沿用浏览器当前的登录 Cookie，
覆盖整条时间轴。轨道（第几行）由本程序按目标窗口尺寸独立排版，不是原视频
画面的像素级镜像。仅支持普通滚动/逆向滚动/顶部/底部弹幕，高级/代码弹幕暂不支持。

独占全屏游戏无法叠加 overlay（Windows 桌面合成限制）。

弹幕显示参数（需重启生效）在 `config.json` 的 `danmaku` 节调整：
`display_area`、`tick_ms`、`font_scale`、`opacity`、`speed`、`cross_seconds`、`fixed_seconds`。

## 悬浮层架构

`overlay_manager.OverlayManager` 统一管理两个子 overlay，职责：

- 维护"当前挂载到哪个窗口"的状态，子 overlay 只需实现统一的生命周期方法
- 目标窗口位置变化走 `SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE)`，不轮询
- 前台切换走 `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`；切换时同步检查目标窗口
  是否还有效，"窗口被关掉"通过这次检查发现，无需单独轮询
- `self.overlays` 顺序即渲染顺序（从下到上），新增 overlay 只需实现相同接口并
  按层级插入列表

## 常见问题

| 现象                       | 处理方法                                                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 热键没反应（尤其游戏内）   | 以管理员身份运行                                                                                                                                  |
| 字幕抓不到 / 指南针不转    | B 站 DOM 更新了，在 F12 里查字幕元素的 class，更新 `bilibili_config.py` 的 `SUBTITLE_SELECTOR`                                                    |
| 注入栏与 B 站顶栏重叠      | B 站改版后固定顶栏结构变了，调整 `webview_chrome.py` 里"下移固定顶栏"的逻辑                                                                       |
| 拿不到弹幕                 | 确认当前是带 BV 号的视频播放页；控制台的报错带有服务端原始返回（如 `-352`），据此更新 `bilibili_config.py` 的 `DANMAKU_SEG_URL` / `WBI_MIXIN_TAB` |
| 弹幕位置与目标窗口对不上   | 目标窗口有非标准自绘边框时，在 `window_picker.get_window_client_rect_on_screen` 里做针对性调整                                                    |
| overlay 完全不跟目标窗口动 | 确认游戏不是独占全屏模式；否则排查 `window_picker.install_location_hook` 的注册是否成功                                                           |

## 项目结构

```
CompassPlayer/
├── main.py             # 入口：WebView2 主线程 + Qt 后台线程
├── webview_chrome.py   # 注入页面的顶部工具栏 + js_api 桥接
├── config.py           # 默认配置、JSON 加载/保存
├── bilibili_config.py  # B 站接口地址、请求头、CSS 选择器（改版时在此更新）
├── hotkey_manager.py   # 全局热键（keyboard.hook）
├── subtitle_parser.py  # 字幕轮询 + 方向词解析
├── beacon_overlay.py   # 透明指南针悬浮窗
├── danmaku_overlay.py  # 弹幕映射叠加层 + 视频播放进度轮询
├── overlay_manager.py  # 统一调度两个子 overlay，事件驱动跟随目标窗口
├── window_picker.py    # 目标窗口枚举/选择弹窗 + 点击穿透 + WinEventHook
├── bilibili_danmaku.py # 直连 Protobuf 分段接口拉取/解析 B 站视频弹幕（含 WBI 签名）
├── settings_dialog.py  # 按键设置弹窗
├── assets/pictures/    # 方向标记图片
└── requirements.txt
```

## 构建发布（GitHub Actions）

手动触发（`workflow_dispatch`）或推送 `v*.*.*` 标签：用 PyInstaller 打包为
`CompassPlayer.exe`，连同 `assets/` 压缩为 RAR 并发布 Release。

版本号从 `config.py` 的 `version` 字段读取，发版前请先更新。
