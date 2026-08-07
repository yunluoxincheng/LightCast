"""嵌入 libmpv 的 QWidget 容器。

关键点：必须设置 ``WA_NativeWindow`` + ``WA_DontCreateNativeAncestors``，
确保 ``winId()`` 返回真正的 HWND，否则 mpv 无法 attach。

鼠标事件
--------
本 widget 是原生窗口，鼠标事件不会冒泡给父级（PlayerWindow），因此这里
显式把鼠标活动通过 ``mouseActivity`` 信号转发出去（用于唤起控制栏）。
需要 ``setMouseTracking(True)`` 才能收到未按下的移动事件。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

from ..logger import get_logger
from .mpv_player import Player

log = get_logger("player.widget")


class MpvWidget(QWidget):
    """承载 libmpv 渲染的黑色背景 widget。"""

    # 鼠标在渲染区活动（移动/点击/双击）——用于唤起控制栏
    mouseActivity = Signal()

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = player

        # 让本 widget 成为原生窗口，winId() 才会返回有效 HWND
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        # 黑底用 stylesheet（与已验证可渲染的最小测试完全一致）
        self.setStyleSheet("background: black;")
        # 接收未按下的鼠标移动事件（用于唤起控制栏）
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ #
    def attach_player(self) -> bool:
        """显式把 mpv attach 到本 widget 的窗口句柄。可重复调用安全。"""
        if self._player.available:
            return True
        wid = int(self.winId())
        log.info("MpvWidget attach: HWND=%s visible=%s", wid, self.isVisible())
        if wid:
            self._player.attach(wid)
            return self._player.available
        log.error("无法获取窗口句柄，mpv 嵌入失败")
        return False

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        """兜底：若尚未 attach，尝试 attach。"""
        super().showEvent(event)
        self.attach_player()

    # ------------------------------------------------------------------ #
    # 鼠标事件 → mouseActivity
    # ------------------------------------------------------------------ #
    def mouseMoveEvent(self, event) -> None:  # noqa: N802, ANN001
        self.mouseActivity.emit()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        self.mouseActivity.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        self.mouseActivity.emit()
        super().mouseDoubleClickEvent(event)
