"""独立播放窗口（完整播放器）。

为什么独立窗口 + 控制栏在底部
------------------------------
mpv 通过 ``wid`` 嵌入的原生 HWND 子窗口会"刺穿"Qt 的非原生 widget（z-order
不归 Qt 管）。如果把控制栏（普通 widget）叠加在 mpv 渲染区上方，控制栏会被
原生窗口遮挡、按钮失效。

解法：控制栏放在 mpv 渲染区的**下方**（垂直布局，两者不重叠），完全不产生
z-order 冲突。自动隐藏时把控制栏 setVisible(False)，mpv 渲染区自动扩大占满。

行为
----
- 16:9 窗口，可置顶，可全屏
- 底部控制栏：上一集 / 后退10s / 播放暂停 / 快进10s / 下一集 / 进度条 / 时间 /
  倍速 / 音量 / 全屏
- 鼠标在窗口内静止 3 秒 → 控制栏自动隐藏（播放中才隐藏）；鼠标移动/按键 → 立即唤起
- 全屏：F 键或双击；Esc 退出全屏
- 关闭窗口不退出应用（仅隐藏）
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    Slider,
    ToolButton,
    TransparentToolButton,
)

from ..i18n import tr
from ..logger import get_logger
from .mpv_player import Player
from .mpv_widget import MpvWidget

log = get_logger("player.window")

# 倍速档位
_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
# 控制栏静止多久后自动隐藏（秒）
_AUTOHIDE_DELAY = 3000


def _fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "0:00"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class PlayerWindow(QWidget):
    """独立播放窗口。"""

    closed = Signal()

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._player = player
        self._fullscreen = False
        self._dragging_slider = False

        self.setWindowTitle(tr("player.window_title"))
        self.setWindowFlag(Qt.Window, True)
        self.setMinimumSize(480, 270)
        self.resize(960, 540)
        # 接受焦点以接收按键
        self.setFocusPolicy(Qt.StrongFocus)
        # 鼠标追踪（即使不按键也能收到 mouseMove，用于唤起控制栏）
        self.setMouseTracking(True)

        self._build_ui()
        self._connect()

        # 自动隐藏定时器
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_AUTOHIDE_DELAY)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._hide_timer.start()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # mpv 渲染区（占主体，自适应缩放）
        self.mpvWidget = MpvWidget(self._player, self)
        self.mpvWidget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.mpvWidget, 1)

        # 底部控制栏（纯黑不透明，与 mpv 不重叠；避免半透明与原生窗口合成异常）
        self.controlBar = QWidget(self)
        self.controlBar.setObjectName("controlBar")
        self.controlBar.setStyleSheet(
            "#controlBar { background: #141414; }"
            "QLabel { color: #e0e0e0; }"
        )
        bar_layout = QHBoxLayout(self.controlBar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        bar_layout.setSpacing(8)

        # 第一行能放下的话放一行；这里用单行布局
        # 上一集 / 后退 / 播放暂停 / 快进 / 下一集
        self.prevButton = TransparentToolButton(FIF.LEFT_ARROW, self.controlBar)
        self.prevButton.setToolTip(tr("player.prev"))
        self.backwardButton = TransparentToolButton(FIF.SKIP_BACK, self.controlBar)
        self.backwardButton.setToolTip(tr("player.backward"))
        self.playButton = ToolButton(FIF.PLAY, self.controlBar)
        self.playButton.setToolTip(tr("player.play"))
        self.forwardButton = TransparentToolButton(FIF.SKIP_FORWARD, self.controlBar)
        self.forwardButton.setToolTip(tr("player.forward"))
        self.nextButton = TransparentToolButton(FIF.RIGHT_ARROW, self.controlBar)
        self.nextButton.setToolTip(tr("player.next"))

        for b in (self.prevButton, self.backwardButton, self.forwardButton, self.nextButton):
            b.setFixedSize(30, 30)
        self.playButton.setFixedSize(36, 36)

        # 时间 + 进度条 + 时长
        self.timeLabel = BodyLabel("0:00")
        self.timeLabel.setMinimumWidth(38)
        self.timeLabel.setAlignment(Qt.AlignCenter)
        self.positionSlider = Slider(Qt.Horizontal, self.controlBar)
        self.positionSlider.setRange(0, 1000)
        self.positionSlider.setValue(0)
        self.durationLabel = BodyLabel("0:00")
        self.durationLabel.setMinimumWidth(38)
        self.durationLabel.setAlignment(Qt.AlignCenter)

        # 倍速
        self.speedCombo = QComboBox(self.controlBar)
        for sp in _SPEEDS:
            self.speedCombo.addItem(f"{sp}×" if sp != 1.0 else tr("player.speed.normal"), sp)
        self.speedCombo.setCurrentIndex(_SPEEDS.index(1.0))
        self.speedCombo.setFixedWidth(72)
        self.speedCombo.setToolTip(tr("player.speed"))

        # 音量
        self.muteButton = TransparentToolButton(FIF.VOLUME, self.controlBar)
        self.muteButton.setFixedSize(30, 30)
        self.muteButton.setToolTip(tr("player.mute"))
        self.volumeSlider = Slider(Qt.Horizontal, self.controlBar)
        self.volumeSlider.setRange(0, 100)
        self.volumeSlider.setValue(self._player.get_volume())
        self.volumeSlider.setFixedWidth(80)
        self.volumeSlider.setToolTip(tr("player.volume"))

        # 全屏
        self.fullscreenButton = TransparentToolButton(FIF.FULL_SCREEN, self.controlBar)
        self.fullscreenButton.setFixedSize(30, 30)
        self.fullscreenButton.setToolTip(tr("player.fullscreen"))

        # 组装：[上一集][后退][播放][快进][下一集] [时间][===进度===][时长] [倍速][音量][静音][全屏]
        for w in (self.prevButton, self.backwardButton, self.playButton,
                  self.forwardButton, self.nextButton):
            bar_layout.addWidget(w)
        bar_layout.addSpacing(6)
        bar_layout.addWidget(self.timeLabel)
        bar_layout.addWidget(self.positionSlider, 1)
        bar_layout.addWidget(self.durationLabel)
        bar_layout.addSpacing(6)
        bar_layout.addWidget(self.speedCombo)
        bar_layout.addWidget(self.muteButton)
        bar_layout.addWidget(self.volumeSlider)
        bar_layout.addWidget(self.fullscreenButton)

        layout.addWidget(self.controlBar)

    def _connect(self) -> None:
        # 按钮直连 Player
        self.playButton.clicked.connect(self._player.play_pause)
        self.forwardButton.clicked.connect(lambda: self._player.seek_relative(10))
        self.backwardButton.clicked.connect(lambda: self._player.seek_relative(-10))
        self.muteButton.clicked.connect(lambda: self._player.set_mute(not self._player.is_muted()))
        self.volumeSlider.valueChanged.connect(self._player.set_volume)
        self.speedCombo.currentIndexChanged.connect(
            lambda i: self._player.set_speed(self.speedCombo.itemData(i))
        )
        self.fullscreenButton.clicked.connect(self.toggle_fullscreen)
        # 上一集/下一集：DLNA 单媒体场景下无效（灰显），预留接口
        self.prevButton.clicked.connect(lambda: log.debug("上一集（无播放列表）"))
        self.nextButton.clicked.connect(lambda: log.debug("下一集（无播放列表）"))
        self.prevButton.setEnabled(False)
        self.nextButton.setEnabled(False)

        # 进度条
        self.positionSlider.sliderPressed.connect(self._on_slider_pressed)
        self.positionSlider.sliderReleased.connect(self._on_slider_released)

        # player 信号刷新 UI
        s = self._player.signals
        s.positionChanged.connect(self._on_position)
        s.durationChanged.connect(self._on_duration)
        s.stateChanged.connect(self._on_state)
        s.volumeChanged.connect(self._on_volume)
        s.muteChanged.connect(self._on_mute)

    # ------------------------------------------------------------------ #
    # attach
    # ------------------------------------------------------------------ #
    def attach_mpv(self) -> bool:
        """把 mpv attach 到本窗口的渲染区。窗口必须先 show。"""
        return self.mpvWidget.attach_player()

    # ------------------------------------------------------------------ #
    # 控制栏自动隐藏
    # ------------------------------------------------------------------ #
    def _show_controls(self) -> None:
        if not self.controlBar.isVisible():
            self.controlBar.show()
        self._hide_timer.start()

    def _hide_controls(self) -> None:
        # 仅在播放中且非拖动进度条时隐藏
        if self._player.get_state() == "playing" and not self._dragging_slider:
            self.controlBar.hide()

    def enterEvent(self, event) -> None:  # noqa: N802, ANN001
        self._show_controls()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._hide_timer.stop()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802, ANN001
        # 鼠标移动 → 唤起控制栏（依赖 setMouseTracking(True)）
        self._show_controls()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        self.toggle_fullscreen()

    # ------------------------------------------------------------------ #
    # 进度/状态刷新
    # ------------------------------------------------------------------ #
    def _on_slider_pressed(self) -> None:
        self._dragging_slider = True
        self._hide_timer.stop()  # 拖动时不隐藏

    def _on_slider_released(self) -> None:
        self._dragging_slider = False
        dur = self._player.get_duration()
        if dur and dur > 0:
            ratio = self.positionSlider.value() / 1000.0
            self._player.seek(ratio * dur)
        self._hide_timer.start()

    def _on_position(self, value) -> None:
        if value is None:
            self.timeLabel.setText("0:00")
            if not self._dragging_slider:
                self.positionSlider.setValue(0)
            return
        self.timeLabel.setText(_fmt_time(value))
        dur = self._player.get_duration()
        if not self._dragging_slider and dur and dur > 0:
            self.positionSlider.setValue(int(max(0, min(1, value / dur)) * 1000))

    def _on_duration(self, value) -> None:
        self.durationLabel.setText(_fmt_time(value))

    def _on_state(self, state: str) -> None:
        self.playButton.setIcon(FIF.PAUSE if state == "playing" else FIF.PLAY)
        if state == "playing":
            self._hide_timer.start()
        else:
            self._show_controls()  # 非播放状态始终显示控制栏

    def _on_volume(self, v: int) -> None:
        self.volumeSlider.setValue(v)

    def _on_mute(self, muted: bool) -> None:
        self.muteButton.setIcon(FIF.MUTE if muted else FIF.VOLUME)

    # ------------------------------------------------------------------ #
    # 全屏 / 按键
    # ------------------------------------------------------------------ #
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._fullscreen = False
        else:
            self.showFullScreen()
            self._fullscreen = True
        self.fullscreenButton.setIcon(FIF.CANCEL if self._fullscreen else FIF.FULL_SCREEN)
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
    # 关闭
    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        log.debug("PlayerWindow 关闭（隐藏）")
        self.hide()
        event.ignore()
        self.closed.emit()
