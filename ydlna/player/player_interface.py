"""播放器页面（内嵌 mpv 渲染区 + 悬浮控制栏）。

架构
----
- MpvWidget（原生窗口）嵌入本页面，占主体
- ControlBar（独立浮层窗口）悬浮在页面底部内侧——不占布局（画面比例不变）、
  独立窗口盖在原生渲染区之上（可点击）
- 页面切换处理：本页 hide 时强制 mpvWidget.hide()（防止原生窗口刺穿到其它
  导航页造成 UI 残留）；show 时恢复

投屏到达时由 app.py 切换到本页。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon as FIF,
    IconWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

from ..i18n import tr, Translator
from ..logger import get_logger
from .control_bar import ControlBar
from .mpv_player import Player
from .mpv_widget import MpvWidget

if TYPE_CHECKING:
    pass

log = get_logger("ui.player")


class PlayerInterface(QWidget):
    """播放器页面（内嵌渲染区 + 悬浮控制栏）。"""

    stateChanged = Signal(str)

    def __init__(self, player: Player, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("player-interface")
        self._player = player
        self._build_ui()
        self._connect()
        self._retranslate()
        Translator.instance().languageChanged.connect(self._retranslate)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部信息条（媒体标题 / 空状态提示）
        self.header = QFrame(self)
        self.header.setObjectName("playerHeader")
        self.header.setStyleSheet(
            "#playerHeader { background: #1a1a1a; border-bottom: 1px solid #2a2a2a; }"
        )
        header_lay = QHBoxLayout(self.header)
        header_lay.setContentsMargins(20, 10, 20, 10)

        self.titleLabel = TitleLabel(tr("player.empty"))
        self.titleLabel.setStyleSheet("color: #e0e0e0; font-size: 16px;")
        header_lay.addWidget(self.titleLabel, 1)
        root.addWidget(self.header)

        # 中部：mpv 渲染区（占满）
        self.mpvWidget = MpvWidget(self._player, self)
        self.mpvWidget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.mpvWidget, 1)

        # 空状态覆盖层（悬浮在渲染区之上，投屏后隐藏）
        self.emptyWidget = self._build_empty(self)
        self.emptyWidget.setGeometry(0, 0, 1, 1)  # 初始小几何，由 resizeEvent 校正
        self.emptyWidget.hide()

        # 独立浮层控制栏（悬浮，不占布局；归属主窗口，不置顶）
        self.controlBar = ControlBar(self._player)
        # 页面加入主窗口后，把控制栏归属到主窗口（Qt.Tool + parent 随主窗口
        # 最小化/隐藏，且不会覆盖其它应用）
        top = self.window()
        if top is not self:
            self.controlBar.setParent(top)
        self.controlBar.attach_to(self)
        self.controlBar.fullscreenRequested.connect(self._on_fullscreen_requested)
        self.mpvWidget.mouseActivity.connect(self._show_controls)
        self.mpvWidget.mouseDoubleClicked.connect(self._on_fullscreen_requested)
        self.controlBar.activity.connect(self._show_controls)

        # 自动隐藏定时器
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._hide_timer.start()

    def _build_empty(self, parent) -> QWidget:  # noqa: ANN001
        w = QWidget(parent)
        w.setStyleSheet("background: #141414;")
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(12)

        icon = IconWidget(FIF.VIDEO, w)
        icon.setFixedSize(64, 64)
        self.emptyTitle = SubtitleLabel(tr("player.empty"))
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        self.emptyHint = BodyLabel(tr("player.empty.hint"))
        self.emptyHint.setEnabled(False)
        self.emptyHint.setWordWrap(True)
        self.emptyHint.setAlignment(Qt.AlignCenter)

        lay.addWidget(icon, 0, Qt.AlignCenter)
        lay.addWidget(self.emptyTitle, 0, Qt.AlignCenter)
        lay.addWidget(self.emptyHint, 0, Qt.AlignCenter)
        return w

    def _connect(self) -> None:
        s = self._player.signals
        s.mediaChanged.connect(self._on_media_changed)
        s.stateChanged.connect(self._on_state_changed)
        s.errorOccurred.connect(self._on_error)

    # ------------------------------------------------------------------ #
    # 页面切换（关键：原生窗口的 hide/show 管理）
    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        # 延迟到布局完成后恢复渲染区 + attach
        QTimer.singleShot(0, self._on_page_shown)

    def hideEvent(self, event) -> None:  # noqa: N802, ANN001
        super().hideEvent(event)
        # 关键：切到其它导航页时强制隐藏原生窗口，防止 z-order 刺穿残留
        self.mpvWidget.hide()
        self.controlBar.hide()

    def _on_page_shown(self) -> None:
        self.mpvWidget.show()
        self.mpvWidget.attach_player()
        # 有媒体时显示播放画面，否则显示空状态
        if self._player.get_duration() is None:
            self._show_empty()
        else:
            self._hide_empty()
        self._position_overlays()
        # 非全屏常驻显示控制栏；全屏且播放中才启动自动隐藏
        self._show_controls()
        # 让页面获得焦点以接收键盘快捷键
        self.setFocus()

    # ------------------------------------------------------------------ #
    # 覆盖层定位（空状态 + 控制栏）
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._position_overlays()

    def _position_overlays(self) -> None:
        """把空状态覆盖层铺满渲染区；控制栏按页面底部定位。"""
        # 空状态覆盖层 = 渲染区几何（含 header 下方）
        r = self.mpvWidget.geometry()
        self.emptyWidget.setGeometry(r)
        self.controlBar.update_position()

    # ------------------------------------------------------------------ #
    # 控制栏显示/隐藏
    # ------------------------------------------------------------------ #
    def _show_controls(self) -> None:
        if not self.controlBar.isVisible():
            self.controlBar.show()
            self.controlBar.update_position()
        # 非全屏时控制栏常驻（不启动隐藏计时）；全屏时才自动隐藏
        if self._is_fullscreen():
            self._hide_timer.start()

    def _hide_controls(self) -> None:
        # 仅全屏时自动隐藏
        if self._is_fullscreen() and self._player.get_state() == "playing":
            self.controlBar.hide()

    def _is_fullscreen(self) -> bool:
        win = self.window()
        return win is not None and win.isFullScreen()

    def on_fullscreen_changed(self, is_fullscreen: bool) -> None:
        """全屏状态变化时同步控制栏（由 MainWindow 调用）。

        进全屏：启动自动隐藏计时；退全屏：常驻显示并停止计时。
        """
        if is_fullscreen:
            if self._player.get_state() == "playing":
                self._hide_timer.start()
            else:
                self._show_controls()
        else:
            self._hide_timer.stop()
            self._show_controls()

    def _show_empty(self) -> None:
        self.emptyWidget.show()
        self.emptyWidget.raise_()

    def _hide_empty(self) -> None:
        self.emptyWidget.hide()

    # ------------------------------------------------------------------ #
    # 全屏请求（由 MainWindow/app 处理：主窗口全屏 + 隐藏导航）
    # ------------------------------------------------------------------ #
    def _on_fullscreen_requested(self) -> None:
        self.toggleFullscreenRequested.emit()

    toggleFullscreenRequested = Signal()

    # ------------------------------------------------------------------ #
    # 键盘/鼠标快捷键
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001
        from PySide6.QtGui import QKeyEvent
        if not isinstance(event, QKeyEvent):
            return super().keyPressEvent(event)
        self._show_controls()
        key = event.key()
        if key == Qt.Key_Escape:
            self.toggleFullscreenRequested.emit()  # MainWindow 会判断是否退出全屏
        elif key == Qt.Key_F:
            self.toggleFullscreenRequested.emit()
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

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        # 点击页面获得键盘焦点（快捷键才能生效）
        self.setFocus()
        self._show_controls()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_media_changed(self, title: str, url: str) -> None:
        self.titleLabel.setText(title or tr("player.unknown_title"))
        self._hide_empty()
        self._show_controls()

    def _on_state_changed(self, state: str) -> None:
        self.stateChanged.emit(state)
        if state == "playing":
            self._hide_empty()
            self._hide_timer.start()
        elif state == "idle" and self._player.get_duration() is None:
            self._show_empty()

    def _on_error(self, msg: str) -> None:
        self.titleLabel.setText(msg)

    # ------------------------------------------------------------------ #
    # 国际化
    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.emptyTitle.setText(tr("player.empty"))
        self.emptyHint.setText(tr("player.empty.hint"))
        self.titleLabel.setText(tr("player.empty"))

    def retranslate_ui(self) -> None:
        self._retranslate()
