"""嵌入 libmpv 的 QWidget 容器。

关键点：必须设置 ``WA_NativeWindow`` + ``WA_DontCreateNativeAncestors``，
确保 ``winId()`` 返回真正的 HWND，否则 mpv 无法 attach。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QWidget

from ..logger import get_logger
from .mpv_player import Player

log = get_logger("player.widget")


class MpvWidget(QWidget):
    """承载 libmpv 渲染的黑色背景 widget。

    用法::

        widget = MpvWidget()
        player = Player()
        player.attach(widget.mpv_window_id)
    """

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = player

        # 让本 widget 成为原生窗口，winId() 才会返回有效 HWND
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # 注意：不设 WA_PaintOnScreen —— 它会触发 "QWidget::paintEngine: Should no
        # longer be called" 警告，且与 autoFillBackground 冲突。mpv 通过 wid 直接
        # 渲染到原生窗口，Qt 只需提供黑底背景即可。

        # 黑色背景
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setMinimumSize(320, 180)

    def attach_player(self) -> bool:
        """显式把 mpv attach 到本 widget 的窗口句柄。

        由父控件在自身显示后调用（showEvent 时机不可靠，故改用显式调用）。
        返回是否成功 attach。
        """
        if self._player.available:
            return True
        wid = int(self.winId())
        log.info("MpvWidget attach: HWND=%s visible=%s", wid, self.isVisible())
        if wid:
            self._player.attach(wid)
            return self._player.available
        log.error("无法获取窗口句柄，mpv 嵌入失败")
        return False

    def showEvent(self, event) -> None:  # noqa: ANN001
        """兜底：若尚未 attach，尝试 attach（某些场景 showEvent 会先于显式调用触发）。"""
        super().showEvent(event)
        self.attach_player()

    @property
    def mpv_window_id(self) -> int:
        """返回窗口句柄（int）。在 show 之后才有效。"""
        return int(self.winId())
