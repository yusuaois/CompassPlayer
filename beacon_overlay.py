"""
beacon_overlay.py
------------------
"地图信标叠加层"：一个无边框、背景透明、始终置顶的独立顶层窗口
用于显示一个可拖拽、可缩放的指南针贴图，帮助玩家把它对齐到游戏内的小地图上
并根据 subtitle_parser 解析出的方向实时高亮显示对应方位

交互说明：
- 左键单击（非编辑模式下）：在"完整指南针"与"收起为小圆点"两种状态间切换
  之所以设计成"收起"而不是真正的 QWidget.hide()，是因为窗口一旦被 hide()
  就再也无法接收鼠标点击去恢复它——所以这里改为缩成一个仍可点击的小圆点，
  行为上才真正符合"点一下切换显示/隐藏"的交互直觉
- 右键单击：切换"对齐编辑模式"，进入编辑模式后会显示红色虚线边框和四角
  缩放手柄，可拖动信标主体移动位置，或拖动手柄调整大小；再次右键单击退出
  编辑模式，并把当前位置、大小保存到 config.json
"""

import math
import os

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

import config as config_module

HANDLE_SIZE = 10  # 缩放手柄边长（像素）
COLLAPSED_SIZE = 28  # 收起状态下的圆点直径（像素）
MARKER_SIZE = 28  # 方向标记图片边长（像素）
MARKER_PATH = os.path.join(config_module.app_dir(), "assets", "pictures", "marker.svg")


class BeaconOverlay(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config

        beacon_cfg = config["beacon"]
        self._expanded_geometry = QRect(
            beacon_cfg["x"], beacon_cfg["y"], beacon_cfg["width"], beacon_cfg["height"]
        )
        self.setGeometry(self._expanded_geometry)

        # 无边框 + 置顶 + 不在任务栏/Alt-Tab 中显示 + 背景透明
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._edit_mode = False
        self._collapsed = False
        self._drag_offset = None  # 拖动信标主体时，鼠标与窗口左上角的偏移
        self._active_handle = None  # 当前正在拖动的缩放手柄名称
        self._current_angle = None  # 当前高亮方向角度（0~359），None 表示暂无方向

        self._marker_renderer = None
        self._marker_pixmap = None
        self._load_marker()

        self.show()

    # ------------------------------------------------------------------
    # 对外接口：由 subtitle_parser 的 direction_detected 信号调用
    # ------------------------------------------------------------------
    def set_direction(self, angle: int):
        self._current_angle = angle
        self.update()

    def _load_marker(self):
        """加载方向标记图片（SVG 或 PNG），未找到则用绘制兜底"""
        if not os.path.exists(MARKER_PATH):
            return
        ext = os.path.splitext(MARKER_PATH)[1].lower()
        try:
            if ext == ".svg":
                self._marker_renderer = QSvgRenderer(MARKER_PATH)
            else:
                self._marker_pixmap = QPixmap(MARKER_PATH)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._collapsed:
            self._paint_collapsed(painter)
            return

        rect = self.rect().adjusted(
            HANDLE_SIZE, HANDLE_SIZE, -HANDLE_SIZE, -HANDLE_SIZE
        )
        if rect.width() <= 0 or rect.height() <= 0:
            rect = self.rect()

        center = rect.center()
        radius = min(rect.width(), rect.height()) // 2

        # 坐标十字线（淡色参考线，无文字、无圆圈，背景透明）
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

        # 当前方向标记：在“十字为直径、圆心为原点”的圆边缘显示一个地图定位点
        if self._current_angle is not None:
            rad = math.radians(self._current_angle - 90)  # -90 使 0° 对应正上方（北）
            px = center.x() + radius * math.cos(rad)
            py = center.y() + radius * math.sin(rad)
            target = QRectF(
                px - MARKER_SIZE / 2, py - MARKER_SIZE / 2, MARKER_SIZE, MARKER_SIZE
            )
            if self._marker_renderer is not None:
                self._marker_renderer.render(painter, target)
            elif self._marker_pixmap is not None:
                painter.drawPixmap(target.toRect(), self._marker_pixmap)
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
                painter.setBrush(QBrush(QColor(255, 60, 60, 255)))
                painter.drawEllipse(QPoint(int(px), int(py)), 7, 7)

        # 编辑模式：红色虚线边框 + 四角缩放手柄
        if self._edit_mode:
            painter.setPen(QPen(QColor(255, 60, 60), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 60, 60)))
            for handle_rect in self._handle_rects().values():
                painter.drawRect(handle_rect)

    def _paint_collapsed(self, painter: QPainter):
        # 收起为一个小红点（无文字）
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        painter.setBrush(QBrush(QColor(255, 80, 80, 220)))
        painter.drawEllipse(self.rect().adjusted(6, 6, -6, -6))

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
            self.move(global_pos - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._active_handle = None
        super().mouseReleaseEvent(event)

    def _resize_by_handle(self, handle: str, global_pos: QPoint):
        geo = self.geometry()
        min_size = HANDLE_SIZE * 4

        if handle == "bottom_right":
            self.resize(
                max(min_size, global_pos.x() - geo.x()),
                max(min_size, global_pos.y() - geo.y()),
            )
        elif handle == "top_left":
            self.setGeometry(
                global_pos.x(),
                global_pos.y(),
                max(min_size, geo.right() - global_pos.x()),
                max(min_size, geo.bottom() - global_pos.y()),
            )
        elif handle == "top_right":
            self.setGeometry(
                geo.x(),
                global_pos.y(),
                max(min_size, global_pos.x() - geo.x()),
                max(min_size, geo.bottom() - global_pos.y()),
            )
        elif handle == "bottom_left":
            self.setGeometry(
                global_pos.x(),
                geo.y(),
                max(min_size, geo.right() - global_pos.x()),
                max(min_size, global_pos.y() - geo.y()),
            )

    # ------------------------------------------------------------------
    # 折叠 / 展开 / 编辑模式
    # ------------------------------------------------------------------
    def _collapse(self):
        self._expanded_geometry = self.geometry()
        self._collapsed = True
        top_left = self.geometry().topLeft()
        self.setGeometry(top_left.x(), top_left.y(), COLLAPSED_SIZE, COLLAPSED_SIZE)
        self.update()

    def _expand(self):
        self._collapsed = False
        self.setGeometry(self._expanded_geometry)
        self.update()

    def _toggle_edit_mode(self):
        self._edit_mode = not self._edit_mode
        if not self._edit_mode:
            self._save_geometry()
        self.update()

    def _save_geometry(self):
        geo = self.geometry()
        self._expanded_geometry = geo
        self.config["beacon"].update(
            {
                "x": geo.x(),
                "y": geo.y(),
                "width": geo.width(),
                "height": geo.height(),
            }
        )
        config_module.save_config(self.config)
