"""
config.py
---------
应用程序配置管理：默认配置定义、从本地 JSON 加载配置、将配置保存回 JSON 文件。
"""

import copy
import json
import os
import sys


def app_dir():
    """应用根目录：PyInstaller 打包后为 exe 所在目录，源码运行为本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    # 应用版本号（GitHub Actions 发布时从此读取，自动加 v 前缀）
    "version": "1.0.0",
    # 全局快捷键
    "hotkeys": {
        "play_pause": "`",  # 播放/暂停切换
        "seek_backward": "5",  # 视频快退
        "seek_forward": "6",  # 视频快进
        "opacity_down": "7",  # 降低窗口透明度
        "opacity_up": "8",  # 提高窗口透明度
        "toggle_visibility": "9",  # 显示/隐藏整个悬浮窗
        "toggle_immersive": "0",  # 切换沉浸模式
    },
    # 视频快进/快退幅度（秒）
    "seek_seconds": 5,
    # 窗口透明度（0.2 ~ 1.0）
    "opacity": 1.0,
    "opacity_step": 0.1,
    "min_opacity": 0.2,
    "max_opacity": 1.0,
    # 主窗口位置与大小
    "window_geometry": {"x": 200, "y": 200, "width": 800, "height": 500},
    # 地图信标叠加层：位置与大小
    "beacon": {"x": 80, "y": 80, "width": 220, "height": 220},
    # 启动时默认加载的地址
    "start_url": "https://www.bilibili.com",
}


def _deep_merge_defaults(target: dict, defaults: dict) -> dict:
    for key, default_value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(default_value)
        elif isinstance(default_value, dict) and isinstance(target[key], dict):
            _deep_merge_defaults(target[key], default_value)
    return target


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] 读取配置文件失败，使用默认配置。错误信息: {e}")
        return copy.deepcopy(DEFAULT_CONFIG)

    return _deep_merge_defaults(user_config, DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except OSError as e:
        print(f"[config] 保存配置文件失败: {e}")
