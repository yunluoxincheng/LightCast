"""独立浮层控制栏（不参与播放窗口布局）。

为什么独立窗口
--------------
1. mpv 的原生 HWND 子窗口会"刺穿"同父级的普通 widget（z-order 不归 Qt 管），
   控制栏若放在 mpv 渲染区上/下都会被遮挡或挤压。
2. 若把控制栏放进 QVBoxLayout，显示/隐藏会重排布局 → mpv 画面重新缩放跳动
   （用户反馈"唤起状态栏会改变画面比例"）。

解法：控制栏是**独立顶层窗口**（frameless + 置顶），悬浮在播放窗口底部内侧。
- 不参与布局 → 显示/隐藏不影响画面
- 独立顶层窗口 → 永远在 mpv 原生窗口之上，可正常点击
- 跟随播放窗口移动/缩放/全屏
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
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

log = get_logger("player.controlbar")

_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
BAR_HEIGHT = 52


def _fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "0:00"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ControlBar(QWidget):
    """独立浮层控制栏。"""

    # 用户在控制栏上的活动（用于重置自动隐藏计时）
    activity = Signal()

    def __init__(self, player: Player, parent: QWidget | None = None) -> None:
        # 嵌入模式：控制栏归属主窗口（parent），不置顶——
        # - Qt.Tool + parent：随主窗口最小化/隐藏，且不会覆盖其它应用
        # - 不加 WindowStaysOnTopHint（那是独立窗口时代为避免被主窗口盖住加的，
        #   嵌入后会让控制栏盖过所有应用）
        # - FramelessWindowHint：无边框
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        self._player = player
        self._anchor: QWidget | None = None
        self._dragging_slider = False

        # 不抢焦点（保持播放窗口接收键盘）
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedHeight(BAR_HEIGHT)
        self.setStyleSheet(
            "QWidget { background: rgba(16, 16, 16, 0.88); }"
            "QLabel { color: #e0e0e0; }"
        )
        self._build_ui()
        self._connect()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        # 上一集 / 后退 / 播放暂停 / 快进 / 下一集
        self.prevButton = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.prevButton.setToolTip(tr("player.prev"))
        self.backwardButton = TransparentToolButton(FIF.SKIP_BACK, self)
        self.backwardButton.setToolTip(tr("player.backward"))
        self.playButton = ToolButton(FIF.PLAY, self)
        self.playButton.setToolTip(tr("player.play"))
        self.forwardButton = TransparentToolButton(FIF.SKIP_FORWARD, self)
        self.forwardButton.setToolTip(tr("player.forward"))
        self.nextButton = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.nextButton.setToolTip(tr("player.next"))

        for b in (self.prevButton, self.backwardButton, self.forwardButton, self.nextButton):
            b.setFixedSize(30, 30)
        self.playButton.setFixedSize(36, 36)

        # 时间 + 进度条 + 时长
        self.timeLabel = BodyLabel("0:00")
        self.timeLabel.setMinimumWidth(38)
        self.timeLabel.setAlignment(Qt.AlignCenter)
        self.positionSlider = Slider(Qt.Horizontal, self)
        self.positionSlider.setRange(0, 1000)
        self.positionSlider.setValue(0)
        self.durationLabel = BodyLabel("0:00")
        self.durationLabel.setMinimumWidth(38)
        self.durationLabel.setAlignment(Qt.AlignCenter)

        # 倍速
        self.speedCombo = QComboBox(self)
        for sp in _SPEEDS:
            self.speedCombo.addItem(f"{sp}×" if sp != 1.0 else tr("player.speed.normal"), sp)
        self.speedCombo.setCurrentIndex(_SPEEDS.index(1.0))
        self.speedCombo.setFixedWidth(72)
        self.speedCombo.setToolTip(tr("player.speed"))

        # 音量
        self.muteButton = TransparentToolButton(FIF.VOLUME, self)
        self.muteButton.setFixedSize(30, 30)
        self.muteButton.setToolTip(tr("player.mute"))
        self.volumeSlider = Slider(Qt.Horizontal, self)
        self.volumeSlider.setRange(0, 100)
        self.volumeSlider.setValue(self._player.get_volume())
        self.volumeSlider.setFixedWidth(80)
        self.volumeSlider.setToolTip(tr("player.volume"))

        # 全屏
        self.fullscreenButton = TransparentToolButton(FIF.FULL_SCREEN, self)
        self.fullscreenButton.setFixedSize(30, 30)
        self.fullscreenButton.setToolTip(tr("player.fullscreen"))

        for w in (self.prevButton, self.backwardButton, self.playButton,
                  self.forwardButton, self.nextButton):
            layout.addWidget(w)
        layout.addSpacing(6)
        layout.addWidget(self.timeLabel)
        layout.addWidget(self.positionSlider, 1)
        layout.addWidget(self.durationLabel)
        layout.addSpacing(6)
        layout.addWidget(self.speedCombo)
        layout.addWidget(self.muteButton)
        layout.addWidget(self.volumeSlider)
        layout.addWidget(self.fullscreenButton)

    def _connect(self) -> None:
        self.playButton.clicked.connect(self._player.play_pause)
        self.forwardButton.clicked.connect(lambda: self._player.seek_relative(10))
        self.backwardButton.clicked.connect(lambda: self._player.seek_relative(-10))
        self.muteButton.clicked.connect(lambda: self._player.set_mute(not self._player.is_muted()))
        self.volumeSlider.valueChanged.connect(self._player.set_volume)
        self.speedCombo.currentIndexChanged.connect(
            lambda i: self._player.set_speed(self.speedCombo.itemData(i))
        )
        self.fullscreenButton.clicked.connect(self._request_fullscreen)
        self.prevButton.clicked.connect(lambda: log.debug("上一集（无播放列表）"))
        self.nextButton.clicked.connect(lambda: log.debug("下一集（无播放列表）"))
        self.prevButton.setEnabled(False)
        self.nextButton.setEnabled(False)

        self.positionSlider.sliderPressed.connect(self._on_slider_pressed)
        self.positionSlider.sliderReleased.connect(self._on_slider_released)

        s = self._player.signals
        s.positionChanged.connect(self._on_position)
        s.durationChanged.connect(self._on_duration)
        s.stateChanged.connect(self._on_state)
        s.volumeChanged.connect(self._on_volume)
        s.muteChanged.connect(self._on_mute)

    # 全屏请求信号由外部（PlayerWindow）连接
    def _request_fullscreen(self) -> None:
        self.activity.emit()
        # 用 QMetaObject 间接触发外部连接
        self.fullscreenRequested.emit()

    fullscreenRequested = Signal()

    # ------------------------------------------------------------------ #
    # 锚定：跟随播放窗口
    # ------------------------------------------------------------------ #
    def attach_to(self, anchor: QWidget) -> None:
        """锚定到播放窗口，显示时悬浮在其底部内侧。"""
        self._anchor = anchor

    def update_position(self) -> None:
        """按锚点 widget 的屏幕位置刷新自己的几何。

        锚点可以是独立窗口（PlayerWindow）或导航页里的页面（PlayerInterface）：
        - 窗口：frameGeometry 有屏幕坐标
        - 页面：mapToGlobal 换算到屏幕坐标（考虑主窗口位置）
        """
        if self._anchor is None or not self.isVisible():
            return
        a = self._anchor
        # 页面（无独立 frame）用 mapToGlobal；窗口用 frameGeometry
        try:
            if a.window() is not None and a.window() is not a:
                # 锚点是某个窗口内的子 widget（页面）
                top_left = a.mapToGlobal(a.rect().topLeft())
                w = a.width()
                y = top_left.y() + a.height() - BAR_HEIGHT
                x = top_left.x()
            else:
                fg = a.frameGeometry()
                x, w = fg.x(), fg.width()
                y = fg.y() + fg.height() - BAR_HEIGHT
        except Exception:  # noqa: BLE001
            return
        self.setGeometry(x, y, w, BAR_HEIGHT)

    # ------------------------------------------------------------------ #
    # 播放状态刷新
    # ------------------------------------------------------------------ #
    def _on_slider_pressed(self) -> None:
        self._dragging_slider = True

    def _on_slider_released(self) -> None:
        self._dragging_slider = False
        dur = self._player.get_duration()
        if dur and dur > 0:
            ratio = self.positionSlider.value() / 1000.0
            self._player.seek(ratio * dur)
        self.activity.emit()

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

    def _on_volume(self, v: int) -> None:
        self.volumeSlider.setValue(v)

    def _on_mute(self, muted: bool) -> None:
        self.muteButton.setIcon(FIF.MUTE if muted else FIF.VOLUME)

    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        self.update_position()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802, ANN001
        self.activity.emit()
        super().mouseMoveEvent(event)
