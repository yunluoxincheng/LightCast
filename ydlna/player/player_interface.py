"""播放器页面（纯控制台）—— 不再内嵌 mpv 渲染区。

架构变更
--------
mpv 的原生 HWND 渲染区放在独立的 ``PlayerWindow``（顶层窗口，脱离主窗口 widget 树），
避免原生窗口与 MSFluentWindow 的 QStackedWidget 导航栈产生 z-order 冲突。

本页面只负责：
- 显示当前媒体信息（标题 / URL / 播放状态）
- 提供播放控制（进度条 / 播放暂停 / 停止 / 音量）—— 这些按钮调 Player，Player 控制
  独立 PlayerWindow 里的 mpv
- 空状态提示（「等待投屏」）
- 「显示/隐藏播放窗口」按钮（让用户控制独立窗口的可见性）
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
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
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

from ..i18n import tr, Translator
from ..logger import get_logger
from .mpv_player import Player
from ..ui.media_controls import MediaControls

if TYPE_CHECKING:
    pass

log = get_logger("ui.player")


class PlayerInterface(QWidget):
    """播放器控制台页面（无内嵌渲染区）。"""

    # 用户通过 UI 播放/暂停（同步托盘等）
    stateChanged = Signal(str)
    # 请求显示/隐藏独立播放窗口
    togglePlayerWindowRequested = Signal(bool)  # True=显示, False=隐藏

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
        root.setContentsMargins(36, 24, 36, 24)
        root.setSpacing(14)

        # 顶部：标题 + 媒体信息
        self.titleLabel = TitleLabel(tr("player.empty"))
        self.statusLabel = BodyLabel("")
        self.statusLabel.setEnabled(False)
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(self.titleLabel)
        header.addWidget(self.statusLabel)
        root.addLayout(header)

        # 中部：状态卡片（空状态提示 / 媒体信息），无原生窗口
        self.tipCard = CardWidget(self)
        tip_lay = QVBoxLayout(self.tipCard)
        tip_lay.setContentsMargins(24, 20, 24, 20)
        tip_lay.setSpacing(10)
        tip_lay.setAlignment(Qt.AlignCenter)

        self.tipIcon = IconWidget(
            FIF.CAST_DESKTOP if hasattr(FIF, "CAST_DESKTOP") else FIF.VIDEO
        )
        self.tipIcon.setFixedSize(56, 56)
        self.tipTitle = SubtitleLabel(tr("player.empty"))
        self.tipTitle.setAlignment(Qt.AlignCenter)
        self.tipHint = BodyLabel(tr("player.empty.hint"))
        self.tipHint.setEnabled(False)
        self.tipHint.setWordWrap(True)
        self.tipHint.setAlignment(Qt.AlignCenter)

        tip_lay.addWidget(self.tipIcon, 0, Qt.AlignCenter)
        tip_lay.addWidget(self.tipTitle, 0, Qt.AlignCenter)
        tip_lay.addWidget(self.tipHint, 0, Qt.AlignCenter)

        # 阴影
        shadow = QGraphicsDropShadowEffect(self.tipCard)
        shadow.setBlurRadius(24)
        shadow.setColor(QColor(0, 0, 0, 90))
        shadow.setOffset(0, 4)
        self.tipCard.setGraphicsEffect(shadow)

        root.addWidget(self.tipCard, 1)

        # 播放窗口控制按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.showWindowButton = PrimaryPushButton(FIF.LINK, tr("player.show_window"))
        self.showWindowButton.setEnabled(False)
        self.fullscreenButton = PushButton(FIF.FULL_SCREEN, tr("player.fullscreen"))
        self.fullscreenButton.setEnabled(False)
        btn_row.addWidget(self.showWindowButton)
        btn_row.addWidget(self.fullscreenButton)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        # 底部控制条
        self.controls = MediaControls(self._player, self)
        root.addWidget(self.controls)

    def _connect(self) -> None:
        self.controls.seekRequested.connect(self._player.seek)
        self.controls.playPauseRequested.connect(self._player.play_pause)
        self.controls.stopRequested.connect(self._player.stop)
        self.controls.volumeRequested.connect(self._player.set_volume)
        self.controls.muteRequested.connect(self._player.set_mute)

        s = self._player.signals
        s.mediaChanged.connect(self._on_media_changed)
        s.stateChanged.connect(self._on_state_changed)
        s.errorOccurred.connect(self._on_error)

        self.showWindowButton.clicked.connect(lambda: self.togglePlayerWindowRequested.emit(True))
        # fullscreenButton 的 clicked 由 app.py 直接连到 PlayerWindow.toggle_fullscreen

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_media_changed(self, title: str, url: str) -> None:
        display = title or tr("player.unknown_title")
        self.titleLabel.setText(display)
        self.statusLabel.setText(url)
        self.showWindowButton.setEnabled(True)
        self.fullscreenButton.setEnabled(True)
        self.tipTitle.setText(display)
        self.tipHint.setText(url)

    def _on_state_changed(self, state: str) -> None:
        self.stateChanged.emit(state)
        if state == "idle" and self._player.get_duration() is None:
            # 回到空闲
            self.titleLabel.setText(tr("player.empty"))
            self.statusLabel.setText("")
            self.tipTitle.setText(tr("player.empty"))
            self.tipHint.setText(tr("player.empty.hint"))

    def _on_error(self, msg: str) -> None:
        self.statusLabel.setText(msg)

    # ------------------------------------------------------------------ #
    # 国际化
    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.showWindowButton.setText(tr("player.show_window"))
        self.fullscreenButton.setText(tr("player.fullscreen"))
        if self._player.get_state() == "idle" and self._player.get_duration() is None:
            self.titleLabel.setText(tr("player.empty"))
            self.tipTitle.setText(tr("player.empty"))
            self.tipHint.setText(tr("player.empty.hint"))

    def retranslate_ui(self) -> None:
        self._retranslate()
