"""独立浮层播放窗口。

为什么独立窗口
--------------
mpv 通过 ``wid`` 嵌入的原生 HWND 子窗口，与 MSFluentWindow（内部 QStackedWidget）
里的非原生 widget 在 z-order 上结构性冲突：原生窗口会"刺穿"导航栈，导致切换页面
时旧 UI 残留、按钮被遮挡失效。这是 Qt 嵌入第三方原生渲染的已知限制（qfluentwidgets
官方也承认 QWebEngineView/QOpenGLWidget 在 FluentWindow 里有同样问题）。

解法（参考 macast）：把 mpv 放在**独立的顶层窗口**里，完全脱离主窗口的 widget 树。
主窗口只做控制台（导航 + 信息 + 控制条），播放画面在独立窗口。两者无 z-order 冲突。

行为
----
- 狗立窗口默认 16:9，可调整大小、可置顶
- 投屏到达时由 app.py 调 ``show()`` 弹出
- 关闭窗口不退出应用（仅隐藏，mpv 继续在后台播）
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..i18n import tr
from ..logger import get_logger
from .mpv_player import Player
from .mpv_widget import MpvWidget

log = get_logger("player.window")


class PlayerWindow(QWidget):
    """独立播放窗口（承载 mpv 原生渲染）。"""

    # 用户按 Esc 退出全屏 / 关闭窗口时通知外部（用于同步 UI）
    closed = Signal()

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        # parent=None → 真正的顶层独立窗口（不进主窗口的 widget 树）
        super().__init__(parent)
        self._player = player
        self._fullscreen = False

        self.setWindowTitle(tr("player.window_title"))
        self.setWindowFlag(Qt.Window, True)
        self.setMinimumSize(480, 270)
        self.resize(960, 540)  # 16:9

        # 唯一子控件：mpv 渲染区
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mpvWidget = MpvWidget(player, self)
        layout.addWidget(self.mpvWidget)

    # ------------------------------------------------------------------ #
    # attach（必须在窗口 show 后调用，winId 才有效）
    # ------------------------------------------------------------------ #
    def attach_mpv(self) -> bool:
        """把 mpv attach 到本窗口的渲染区。可重复调用安全。"""
        return self.mpvWidget.attach_player()

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """关闭窗口不退出应用，仅隐藏。"""
        log.debug("PlayerWindow 关闭（隐藏）")
        self.hide()
        event.ignore()
        self.closed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Esc 退出全屏；F 切换全屏。"""
        key = event.key()
        if key == Qt.Key_Escape and self._fullscreen:
            self.toggle_fullscreen()
        elif key == Qt.Key_F:
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)

    def toggle_fullscreen(self) -> None:
        """切换全屏 / 窗口模式。"""
        if self._fullscreen:
            self.showNormal()
            self._fullscreen = False
        else:
            self.showFullScreen()
            self._fullscreen = True
        log.debug("全屏切换: %s", self._fullscreen)

    # ------------------------------------------------------------------ #
    def set_always_on_top(self, on_top: bool) -> None:
        """是否置顶。"""
        flags = self.windowFlags()
        if on_top:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
        self.show()  # setWindowFlags 后需要重新 show
