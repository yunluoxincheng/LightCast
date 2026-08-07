"""独立播放窗口（完整播放器）。

架构
----
- mpv 渲染区（MpvWidget）铺满整个窗口（黑底，不参与布局压缩）
- 控制栏是**独立顶层浮层窗口**（ControlBar，见 control_bar.py）：
  - 不参与布局 → 显示/隐藏不影响画面比例
  - 独立窗口 → 悬浮在 mpv 原生窗口之上，可正常点击
  - 跟随播放窗口移动/缩放/全屏

自动隐藏
--------
- 鼠标在播放窗口（含 mpv 渲染区）移动 → 唤起控制栏
- 静止 3 秒（播放中）→ 隐藏
- MpvWidget 是原生窗口，鼠标事件不会冒泡到本窗口，因此 MpvWidget
  自己转发鼠标活动信号（见 mpv_widget.mouseActivity）

全屏
----
- F / 双击 / 控制栏按钮 进入全屏；Esc 退出
- 全屏时窗口黑底铺满（无系统边框露出壁纸），控制栏仍悬浮在底部
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QColor, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, TransparentToolButton, FluentIcon as FIF

from ..i18n import tr
from ..logger import get_logger
from .control_bar import ControlBar
from .mpv_player import Player
from .mpv_widget import MpvWidget

log = get_logger("player.window")

_AUTOHIDE_DELAY = 3000
_TITLEBAR_HEIGHT = 32


class _TitleBar(QWidget):
    """自绘标题栏（无边框窗口用）：标题 + 关闭按钮 + 拖动窗口。"""

    closeRequested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(_TITLEBAR_HEIGHT)
        self.setStyleSheet("background: #1a1a1a;")
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        self.titleLabel = BodyLabel(tr("player.window_title"))
        self.titleLabel.setStyleSheet("color: #ccc;")
        self.closeButton = TransparentToolButton(FIF.CLOSE, self)
        self.closeButton.setFixedSize(28, 28)
        self.closeButton.setToolTip(tr("common.close"))
        self.closeButton.clicked.connect(self.closeRequested.emit)
        layout.addWidget(self.titleLabel, 1)
        layout.addWidget(self.closeButton)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            win = self.window()
            if win is not None:
                self._drag_offset = event.globalPosition().toPoint() - win.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            win = self.window()
            if win is not None:
                win.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class PlayerWindow(QWidget):
    """独立播放窗口。"""

    closed = Signal()

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = player
        self._fullscreen = False

        self.setWindowTitle(tr("player.window_title"))
        # 始终无边框：全屏/窗口切换不重建 HWND（重建会导致 mpv 失效、视频重头播）。
        # 窗口模式用自绘标题栏（可拖动），全屏时隐藏标题栏。
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setMinimumSize(480, 270)
        self.resize(960, 540)
        # 纯黑背景（全屏时 mpv 未铺满的边缘也是黑，不会露出壁纸）
        self.setStyleSheet("background: black;")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        # 布局：自绘标题栏（窗口模式可见，全屏隐藏）+ mpv 渲染区
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自绘标题栏（可拖动窗口）
        self.titleBar = _TitleBar(self)
        layout.addWidget(self.titleBar)

        self.mpvWidget = MpvWidget(self._player, self)
        layout.addWidget(self.mpvWidget, 1)

        # 独立浮层控制栏
        self.controlBar = ControlBar(player)
        self.controlBar.attach_to(self)
        self.controlBar.fullscreenRequested.connect(self.toggle_fullscreen)

        # 自绘标题栏关闭按钮 → 隐藏窗口
        self.titleBar.closeRequested.connect(self.close)
        # mpv 渲染区的鼠标活动 → 唤起控制栏
        self.mpvWidget.mouseActivity.connect(self._show_controls)
        # 控制栏自身活动 → 重置隐藏计时
        self.controlBar.activity.connect(self._show_controls)

        # 自动隐藏定时器
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_AUTOHIDE_DELAY)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._hide_timer.start()

    # ------------------------------------------------------------------ #
    # attach
    # ------------------------------------------------------------------ #
    def attach_mpv(self) -> bool:
        """把 mpv attach 到本窗口的渲染区。窗口必须先 show。"""
        return self.mpvWidget.attach_player()

    # ------------------------------------------------------------------ #
    # 控制栏显示/隐藏
    # ------------------------------------------------------------------ #
    def _show_controls(self) -> None:
        if not self.controlBar.isVisible():
            self.controlBar.show()
            self.controlBar.update_position()
        self._hide_timer.start()

    def _hide_controls(self) -> None:
        if self._player.get_state() == "playing" and self.controlBar.isVisible():
            self.controlBar.hide()

    # ------------------------------------------------------------------ #
    # 鼠标事件（本窗口区域 + 转发）
    # ------------------------------------------------------------------ #
    def mouseMoveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._show_controls()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        self.toggle_fullscreen()

    def enterEvent(self, event) -> None:  # noqa: N802, ANN001
        self._show_controls()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._hide_timer.stop()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ #
    # 窗口移动/缩放 → 控制栏跟随
    # ------------------------------------------------------------------ #
    def moveEvent(self, event) -> None:  # noqa: N802, ANN001
        super().moveEvent(event)
        self.controlBar.update_position()

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self.controlBar.update_position()

    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        # 显示窗口时同步控制栏（若应显示）
        if self._player.get_state() != "playing" or self.controlBar.isVisible():
            self.controlBar.update_position()

    def hideEvent(self, event) -> None:  # noqa: N802, ANN001
        super().hideEvent(event)
        self.controlBar.hide()

    # ------------------------------------------------------------------ #
    # 全屏
    # ------------------------------------------------------------------ #
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._fullscreen = False
            self.titleBar.show()
        else:
            self.titleBar.hide()
            self.showFullScreen()
            self._fullscreen = True
        self.controlBar.update_position()
        log.debug("全屏切换: %s", self._fullscreen)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self._show_controls()
        key = event.key()
        if key == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            self._fullscreen = False
        elif key == Qt.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key_Space:
            self._player.play_pause()
        elif key == Qt.Key_Right:
            self._player.seek_relative(10)
        elif key == Qt.Key_Left:
            self._player.seek_relative(-10)
        elif key == Qt.Key_Up:
            self._player.set_volume(min(100, self._player.get_volume() + 5))
        elif key == Qt.Key_Down:
            self._player.set_volume(max(0, self._player.get_volume() - 5))
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        log.debug("PlayerWindow 关闭（隐藏）")
        self.hide()
        event.ignore()
        self.closed.emit()
