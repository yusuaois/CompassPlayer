"""
config.py
---------
应用程序配置管理：默认配置、JSON 加载 / 保存
"""

import copy
import json
import os
import sys


def app_dir():
    """PyInstaller 打包后为 exe 所在目录，源码运行为本文件所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    "version": "1.2.1",
    "hotkeys": {
        "play_pause": "`",
        "seek_backward": "5",
        "seek_forward": "6",
        "opacity_down": "7",
        "opacity_up": "8",
        "toggle_visibility": "9",
        "toggle_immersive": "0",
    },
    "seek_seconds": 5,
    "opacity": 1.0,
    "opacity_step": 0.1,
    "min_opacity": 0.2,
    "max_opacity": 1.0,
    "window_geometry": {"x": 200, "y": 200, "width": 800, "height": 500},
    # 位置以相对目标窗口宽高的比例存储（不是绝对屏幕坐标）
    "beacon": {"rel_x": 0.05, "rel_y": 0.05, "width": 220, "height": 220},
    "danmaku": {
        "font_scale": 1.0,
        "opacity": 0.9,
        "speed": 1.0,
        "cross_seconds": 8.0,  # 基准过屏时间（秒），实际 = 该值 / speed
        "max_lanes": 14,
        "fixed_seconds": 4.0,  # 顶部/底部弹幕停留时间（秒）
        "display_area": 0.34,  # 滚动/顶部弹幕限定在窗口最上面这一比例区域（1.0=不限）
        "tick_ms": 16,  # 弹幕动画刷新间隔（毫秒）
        "max_per_second": 3,  # 每秒最多显示的弹幕条数；高密度时段其余弹幕跳过
        "max_active": 18,  # 同时显示的弹幕条数上限；高密度时段其余弹幕跳过
    },
    "start_url": "https://www.bilibili.com",
    "last_url": "",  # 最近一次访问的页面，启动时优先打开
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
