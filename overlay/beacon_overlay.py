"""
beacon_overlay.py
------------------
无边框透明置顶窗口，显示可拖拽、可缩放的指南针，并根据字幕解析出的方向
实时高亮对应方位

位置以相对目标窗口的比例（rel_x/rel_y）存储，目标窗口移动/缩放时等比
换算，不会跑出目标窗口范围，实际是否显示 = 开关开着 且 目标窗口是当前
前台窗口，两者的且

交互：
- 左键单击：展开 ↔ 收起（完整十字 ↔ 小圆点）
  收起时缩成仍可点击的小圆点，而非 QWidget.hide()，以保留鼠标交互
- 右键单击：切换对齐编辑模式（红色虚线框 + 四角缩放手柄）；
  退出编辑模式时自动将位置/大小保存到 config.json
"""

import math
import os

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

import config as config_module

HANDLE_SIZE = 10
COLLAPSED_SIZE = 28
MARKER_SIZE = 28
MARKER_PATH = os.path.join(config_module.app_dir(), "assets", "pictures", "marker.svg")
COMPASS_PATH = os.path.join(
    config_module.app_dir(), "assets", "pictures", "compass.png"
)
MIN_SIZE = HANDLE_SIZE * 4


class BeaconOverlay(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config

        beacon_cfg = config["beacon"]
        self._rel_x = float(beacon_cfg["rel_x"])
        self._rel_y = float(beacon_cfg["rel_y"])
        self._width = int(beacon_cfg["width"])
        self._height = int(beacon_cfg["height"])

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._target_rect = None
        self._enabled = False
        self._has_focus = False
        self._edit_mode = False
        self._collapsed = False
        self._drag_offset = None
        self._active_handle = None
        self._current_angle = None
        self._marker = QSvgRenderer(MARKER_PATH)
        self._compass = QPixmap(COMPASS_PATH)

    # ------------------------------------------------------------------
    # 由 OverlayManager 调用的生命周期接口
    # ------------------------------------------------------------------
    def on_target_moved(self, rect):
        self._target_rect = rect
        self._apply_geometry()

    on_attach = on_target_moved  # 指南针对挂载与目标窗口移动的处理相同

    def on_detach(self):
        self._target_rect = None
        self._enabled = False
        self._has_focus = False
        self._edit_mode = False
        self._sync_visibility()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._sync_visibility()

    def is_enabled(self) -> bool:
        """返回用户开关的意图，不代表当前是否可见"""
        return self._enabled

    def on_focus_changed(self, has_focus: bool):
        self._has_focus = has_focus
        self._sync_visibility()

    def _sync_visibility(self):
        if (
            self._enabled
            and self._target_rect is not None
            and (self._has_focus or self._edit_mode)
        ):
            self._apply_geometry()
            self.show()
            self.raise_()
        else:
            self.hide()

    def _apply_geometry(self):
        if self._target_rect is None:
            return
        tx, ty, tw, th = self._target_rect
        w = COLLAPSED_SIZE if self._collapsed else self._width
        h = COLLAPSED_SIZE if self._collapsed else self._height
        w = max(1, min(w, max(1, tw)))
        h = max(1, min(h, max(1, th)))
        x = tx + self._rel_x * tw
        y = ty + self._rel_y * th
        x = max(tx, min(x, tx + tw - w))
        y = max(ty, min(y, ty + th - h))
        self.setGeometry(round(x), round(y), w, h)

    # ------------------------------------------------------------------
    # 对外接口：由 subtitle_parser 的 direction_detected 信号调用
    # ------------------------------------------------------------------
    def set_direction(self, angle: int):
        self._current_angle = angle
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._collapsed:
            painter.drawPixmap(self.rect(), self._compass)
            return

        rect = self.rect().adjusted(
            HANDLE_SIZE, HANDLE_SIZE, -HANDLE_SIZE, -HANDLE_SIZE
        )
        if rect.width() <= 0 or rect.height() <= 0:
            rect = self.rect()

        center = rect.center()
        radius = min(rect.width(), rect.height()) // 2

        # 坐标十字参考线
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
        painter.drawLine(
            center.x() - radius, center.y(), center.x() + radius, center.y()
        )
        painter.drawLine(
            center.x(), center.y() - radius, center.x(), center.y() + radius
        )

        # 中心小圆点
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 200)))
        painter.drawEllipse(center, 2, 2)

        # 方向标记（圆边缘，0° = 正上方）
        if self._current_angle is not None:
            rad = math.radians(self._current_angle - 90)
            px = center.x() + radius * math.cos(rad)
            py = center.y() + radius * math.sin(rad)
            target = QRectF(
                px - MARKER_SIZE / 2, py - MARKER_SIZE / 2, MARKER_SIZE, MARKER_SIZE
            )
            self._marker.render(painter, target)

        # 编辑模式：红色虚线边框 + 四角缩放手柄
        if self._edit_mode:
            painter.setPen(QPen(QColor(255, 60, 60), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 60, 60)))
            for handle_rect in self._handle_rects().values():
                painter.drawRect(handle_rect)

    # ------------------------------------------------------------------
    # 缩放手柄区域（四个角）
    # ------------------------------------------------------------------
    def _handle_rects(self):
        w, h = self.width(), self.height()
        s = HANDLE_SIZE
        return {
            "top_left": QRect(0, 0, s, s),
            "top_right": QRect(w - s, 0, s, s),
            "bottom_left": QRect(0, h - s, s, s),
            "bottom_right": QRect(w - s, h - s, s, s),
        }

    def _handle_at(self, pos: QPoint):
        for name, rect in self._handle_rects().items():
            if rect.contains(pos):
                return name
        return None

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._edit_mode:
                handle = self._handle_at(event.position().toPoint())
                if handle:
                    self._active_handle = handle
                else:
                    self._drag_offset = (
                        event.globalPosition().toPoint() - self.geometry().topLeft()
                    )
            elif self._collapsed:
                self._expand()
            else:
                self._collapse()

        elif event.button() == Qt.MouseButton.RightButton and not self._collapsed:
            self._toggle_edit_mode()

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._edit_mode:
            return
        global_pos = event.globalPosition().toPoint()
        if self._active_handle:
            self._resize_by_handle(self._active_handle, global_pos)
        elif self._drag_offset is not None:
            new_top_left = global_pos - self._drag_offset
            self._apply_and_remember(
                new_top_left.x(), new_top_left.y(), self.width(), self.height()
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._active_handle = None
        super().mouseReleaseEvent(event)

    def _resize_by_handle(self, handle: str, global_pos: QPoint):
        """拖动角点缩放：对角点固定不动，被拖动的角点跟随鼠标"""
        geo = self.geometry()
        left, top = handle.endswith("left"), handle.startswith("top")
        x = global_pos.x() if left else geo.x()
        y = global_pos.y() if top else geo.y()
        right = geo.right() if left else global_pos.x()
        bottom = geo.bottom() if top else global_pos.y()
        self._apply_and_remember(x, y, right - x, bottom - y)

    def _apply_and_remember(self, x, y, w, h):
        """将拖动/缩放结果夹在目标窗口范围内，应用并更新相对位置记录"""
        w = max(MIN_SIZE, w)
        h = max(MIN_SIZE, h)
        if self._target_rect is not None:
            tx, ty, tw, th = self._target_rect
            w = min(w, max(1, tw))
            h = min(h, max(1, th))
            x = max(tx, min(x, tx + tw - w))
            y = max(ty, min(y, ty + th - h))
        self.setGeometry(round(x), round(y), round(w), round(h))
        if not self._collapsed:
            self._width, self._height = round(w), round(h)
            if self._target_rect is not None:
                tx, ty, tw, th = self._target_rect
                if tw > 0 and th > 0:
                    self._rel_x = (x - tx) / tw
                    self._rel_y = (y - ty) / th

    # ------------------------------------------------------------------
    # 折叠 / 展开 / 编辑模式
    # ------------------------------------------------------------------
    def _collapse(self):
        self._collapsed = True
        self._apply_geometry()

    def _expand(self):
        self._collapsed = False
        self._apply_geometry()

    def _toggle_edit_mode(self):
        self._edit_mode = not self._edit_mode
        if not self._edit_mode:
            self._save_geometry()
            self._sync_visibility()
        self.update()

    def _save_geometry(self):
        self.config["beacon"].update(
            {
                "rel_x": self._rel_x,
                "rel_y": self._rel_y,
                "width": self._width,
                "height": self._height,
            }
        )
        config_module.save_config(self.config)
