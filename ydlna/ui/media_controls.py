"""Fluent 风格的媒体控制条：进度滑块 + 播放/暂停 + 停止 + 音量 + 时长。

只负责把用户操作转发给 Player，以及根据 Player 信号刷新自身显示。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
)

from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    IconWidget,
    PushButton,
    Slider,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
)

from ..i18n import tr
from ..logger import get_logger
from ..player.mpv_player import Player

log = get_logger("ui.controls")


class _SeekSlider(Slider):
    """点击轨道直接定位的进度条。

    Qt 的 QSlider 默认点击轨道只移动一个 pageStep，不能跳到点击处；
    这里在按下时按 x 坐标换算目标值，并走一遍 pressed→released 的
    现有 seek 链路（点击后仍可按住继续拖动）。
    """

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            ratio = max(0.0, min(1.0, event.position().x() / max(1, self.width())))
            value = round(self.minimum() + (self.maximum() - self.minimum()) * ratio)
            self.setValue(value)
            # 点击 = 一次完整的按下+释放，复用现有 seek 逻辑
            self.sliderPressed.emit()
            self.sliderReleased.emit()
        super().mousePressEvent(event)


def _format_time(seconds: float | None) -> str:
    """把秒格式化为 H:MM:SS 或 M:SS。"""
    if seconds is None or seconds < 0:
        return "0:00"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class MediaControls(QFrame):
    """底部媒体控制条。

    信号
    ----
    - seekRequested(float)  : 用户拖动进度条，请求绝对定位（秒）
    - playPauseRequested()  : 播放/暂停按钮
    - stopRequested()       : 停止按钮
    - volumeRequested(int)  : 音量变化（0..100）
    - muteRequested(bool)   : 静音切换
    """

    seekRequested = Signal(float)
    playPauseRequested = Signal()
    stopRequested = Signal()
    volumeRequested = Signal(int)
    muteRequested = Signal(bool)

    def __init__(self, player: Player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._dragging = False  # 拖动进度条时不回写（避免抖动）
        self.setObjectName("mediaControls")
        self._build_ui()
        self._connect_player()
        self._retranslate()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.NoFrame)

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 8)
        root.setSpacing(12)

        # 进度区：当前时间 + 滑块 + 总时长
        self.timeLabel = BodyLabel("0:00")
        self.timeLabel.setMinimumWidth(40)
        self.timeLabel.setAlignment(Qt.AlignCenter)

        self.positionSlider = _SeekSlider(Qt.Horizontal, self)
        self.positionSlider.setRange(0, 1000)  # 用 0..1000 提高精度
        self.positionSlider.setValue(0)
        self.positionSlider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.durationLabel = BodyLabel("0:00")
        self.durationLabel.setMinimumWidth(40)
        self.durationLabel.setAlignment(Qt.AlignCenter)

        root.addWidget(self.timeLabel)
        root.addWidget(self.positionSlider, 1)
        root.addWidget(self.durationLabel)

        # 按钮区：播放/暂停 + 停止
        self.playButton = ToolButton(FIF.PLAY, self)
        self.playButton.setFixedSize(40, 40)
        self.stopButton = TransparentToolButton(FIF.CANCEL, self)
        self.stopButton.setFixedSize(32, 32)

        root.addSpacing(8)
        root.addWidget(self.playButton)
        root.addWidget(self.stopButton)

        # 音量区
        self.muteButton = TransparentToolButton(FIF.VOLUME, self)
        self.muteButton.setFixedSize(32, 32)
        self.volumeSlider = Slider(Qt.Horizontal, self)
        self.volumeSlider.setRange(0, 100)
        self.volumeSlider.setValue(self._player.get_volume())
        self.volumeSlider.setFixedWidth(100)

        root.addSpacing(8)
        root.addWidget(self.muteButton)
        root.addWidget(self.volumeSlider)

        # 信号连接
        self.playButton.clicked.connect(self._on_play_pause)
        self.stopButton.clicked.connect(self.stopRequested.emit)
        self.muteButton.clicked.connect(self._on_mute_toggle)
        self.volumeSlider.valueChanged.connect(self.volumeRequested.emit)
        self.positionSlider.sliderPressed.connect(self._on_slider_pressed)
        self.positionSlider.sliderReleased.connect(self._on_slider_released)

    def _connect_player(self) -> None:
        s = self._player.signals
        s.positionChanged.connect(self._on_position)
        s.durationChanged.connect(self._on_duration)
        s.stateChanged.connect(self._on_state)
        s.volumeChanged.connect(self.volumeSlider.setValue)
        s.muteChanged.connect(self._on_mute_changed)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_position(self, value) -> None:
        if value is None:
            self.timeLabel.setText("0:00")
            if not self._dragging:
                self.positionSlider.setValue(0)
            return
        self.timeLabel.setText(_format_time(value))
        dur = self._player.get_duration()
        if not self._dragging and dur and dur > 0:
            ratio = max(0.0, min(1.0, value / dur))
            self.positionSlider.setValue(int(ratio * 1000))

    def _on_duration(self, value) -> None:
        self.durationLabel.setText(_format_time(value))

    def _on_state(self, state: str) -> None:
        icon = FIF.PAUSE if state == "playing" else FIF.PLAY
        self.playButton.setIcon(icon)
        enabled = state in ("playing", "paused")
        self.positionSlider.setEnabled(enabled or self._player.get_duration() is not None)

    def _on_mute_changed(self, muted: bool) -> None:
        self.muteButton.setIcon(FIF.MUTE if muted else FIF.VOLUME)

    def _on_play_pause(self) -> None:
        self.playPauseRequested.emit()

    def _on_mute_toggle(self) -> None:
        self.muteRequested.emit(not self._player.is_muted())

    def _on_slider_pressed(self) -> None:
        self._dragging = True

    def _on_slider_released(self) -> None:
        self._dragging = False
        dur = self._player.get_duration()
        if dur and dur > 0:
            ratio = self.positionSlider.value() / 1000.0
            self.seekRequested.emit(ratio * dur)

    # ------------------------------------------------------------------ #
    # 国际化
    # ------------------------------------------------------------------ #
    def _retranslate(self) -> None:
        self.playButton.setToolTip(tr("player.play"))
        self.stopButton.setToolTip(tr("player.stop"))
        self.muteButton.setToolTip(tr("player.mute"))
        self.volumeSlider.setToolTip(tr("player.volume"))
        # 播放按钮的 tooltip 会随状态变，由 _on_state 后续刷新

    def retranslate_ui(self) -> None:
        self._retranslate()
