"""复用的小组件。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QLabel, QSizePolicy


class StatusDot(QLabel):
    """一个圆点状态指示灯，颜色可变。"""

    _COLORS = {
        "running": "#22c55e",   # 绿
        "stopped": "#9ca3af",   # 灰
        "error": "#ef4444",     # 红
        "warning": "#f59e0b",   # 黄
    }

    def __init__(self, status: str = "stopped", parent=None) -> None:
        super().__init__(parent)
        self._status = status
        self.setFixedSize(12, 12)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    @property
    def status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        color = self._COLORS.get(self._status, self._COLORS["stopped"])
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, self.width(), self.height())
