"""嵌入 libmpv 的 QWidget 容器。

关键点：必须设置 ``WA_NativeWindow`` + ``WA_DontCreateNativeAncestors``，
确保 ``winId()`` 返回真正的 HWND，否则 mpv 无法 attach。

鼠标事件
--------
本 widget 是原生窗口，鼠标事件不会冒泡给父级，因此这里
显式把鼠标活动通过 ``mouseActivity`` 信号转发出去（用于唤起控制栏）。
需要 ``setMouseTracking(True)`` 才能收到未按下的移动事件。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from ..logger import get_logger
from .mpv_player import Player

log = get_logger("player.widget")


class MpvWidget(QWidget):
    """承载 libmpv 渲染的黑色背景 widget。"""

    # 鼠标在渲染区活动（移动/点击/双击）——用于唤起控制栏
    mouseActivity = Signal()
    # 单击（双击判定窗口内无双击）——用于播放/暂停
    singleClicked = Signal()
    # 双击渲染区——用于切换全屏
    mouseDoubleClicked = Signal()

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
        # 单击判定：双击判定窗口（doubleClickInterval，默认 400ms）内
        # 没有跟来双击（mouseDoubleClickEvent 会取消）才算单击。
        # 定时器必须比双击窗口长——否则慢速双击（间隔 300~400ms）会先
        # 触发单击暂停、再触发双击全屏（用户反馈的误触）
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.setInterval(QApplication.doubleClickInterval() + 60)
        self._single_click_timer.timeout.connect(self.singleClicked.emit)

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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ANN001
        """左键抬起：启动单击判定（双击到达时在 mouseDoubleClickEvent 取消）。"""
        self.mouseActivity.emit()
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.start()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        # 取消待定的单击（避免双击全屏前先误触发一次播放/暂停）
        self._single_click_timer.stop()
        self.mouseActivity.emit()
        self.mouseDoubleClicked.emit()
        super().mouseDoubleClickEvent(event)
