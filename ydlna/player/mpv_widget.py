"""嵌入 libmpv 的 QWidget 容器。

关键点：必须设置 ``WA_NativeWindow`` + ``WA_DontCreateNativeAncestors``，
确保 ``winId()`` 返回真正的 HWND，否则 mpv 无法 attach。

注意：本 widget 的属性必须和「已验证可渲染」的最小测试保持一致——
- 只设 WA_NativeWindow + WA_DontCreateNativeAncestors
- 黑色背景用 stylesheet，**不用** WA_OpaquePaintEvent + autoFillBackground
  （后两者会让 Qt 的合成层覆盖 mpv 渲染的帧，导致画面出不来）
- **不**设 setMinimumSize（让布局自由管理，避免与原生窗口几何冲突）
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from ..logger import get_logger
from .mpv_player import Player

log = get_logger("player.widget")


class MpvWidget(QWidget):
    """承载 libmpv 渲染的黑色背景 widget。"""

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = player

        # 让本 widget 成为原生窗口，winId() 才会返回有效 HWND
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        # 黑底用 stylesheet（与已验证可渲染的最小测试完全一致）
        self.setStyleSheet("background: black;")
        # 注意：除了上面三个设置，不加任何其它属性（setMinimumSize /
        # setSizePolicy / WA_OpaquePaintEvent 等）——它们都可能破坏渲染。

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
