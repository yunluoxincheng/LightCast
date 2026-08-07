"""主页：投屏状态、设备信息、操作引导。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
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
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
    setFont,
)

from ..i18n import tr, Translator
from ..logger import get_logger
from .widgets import StatusDot

if TYPE_CHECKING:
    from ..dlna.server import DlnaServer
    from ..player.mpv_player import Player

log = get_logger("ui.home")


class _InfoCard(CardWidget):
    """一个信息卡片：图标 + 标签 + 值。"""

    def __init__(self, icon, title: str, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setFixedHeight(96)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(16)

        self.iconWidget = IconWidget(icon, self)
        self.iconWidget.setFixedSize(32, 32)
        lay.addWidget(self.iconWidget, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(2)
        self.titleLabel = BodyLabel(title)
        self.titleLabel.setEnabled(False)
        self.valueLabel = StrongBodyLabel("—")
        setFont(self.valueLabel, 16)
        col.addWidget(self.titleLabel)
        col.addWidget(self.valueLabel)
        lay.addLayout(col, 1)


class HomeInterface(QWidget):
    """主页。"""

    # 用户点击启动/停止服务
    toggleServiceRequested = Signal(bool)  # True=启动, False=停止

    def __init__(self, player: "Player", server: "DlnaServer", parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("home-interface")
        self._player = player
        self._server = server
        self._build_ui()
        self._connect()
        self._retranslate()
        Translator.instance().languageChanged.connect(self._retranslate)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 24, 36, 24)
        root.setSpacing(16)

        # 标题 + 副标题
        self.titleLabel = TitleLabel(tr("home.title"))
        root.addWidget(self.titleLabel)

        # 状态条
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.statusDot = StatusDot("stopped")
        self.statusText = BodyLabel(tr("home.status.stopped"))
        status_row.addWidget(self.statusDot)
        status_row.addWidget(self.statusText, 0, Qt.AlignVCenter)
        status_row.addStretch(1)
        root.addLayout(status_row)

        # 三个信息卡片
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.deviceCard = _InfoCard(FIF.APPLICATION, tr("home.device_name"))
        self.ipCard = _InfoCard(FIF.WIFI, tr("home.device_ip"))
        self.playingCard = _InfoCard(FIF.MEDIA, tr("home.now_playing"))
        cards.addWidget(self.deviceCard)
        cards.addWidget(self.ipCard)
        cards.addWidget(self.playingCard)
        root.addLayout(cards)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.startButton = PrimaryPushButton(FIF.PLAY, tr("home.start_service"))
        self.stopButton = PushButton(FIF.PAUSE, tr("home.stop_service"))
        self.stopButton.setEnabled(False)
        btn_row.addWidget(self.startButton)
        btn_row.addWidget(self.stopButton)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        root.addSpacing(6)

        # 引导卡片
        self.tipCard = CardWidget(self)
        tip_lay = QVBoxLayout(self.tipCard)
        tip_lay.setContentsMargins(20, 16, 20, 16)
        tip_lay.setSpacing(6)
        self.tipTitle = SubtitleLabel(tr("home.tip.title"))
        self.tipBody = BodyLabel(tr("home.tip.body"))
        self.tipBody.setWordWrap(True)
        tip_lay.addWidget(self.tipTitle)
        tip_lay.addWidget(self.tipBody)
        root.addWidget(self.tipCard)

        root.addStretch(1)

    def _connect(self) -> None:
        self.startButton.clicked.connect(lambda: self.toggleServiceRequested.emit(True))
        self.stopButton.clicked.connect(lambda: self.toggleServiceRequested.emit(False))
        self._player.signals.mediaChanged.connect(self._on_media_changed)
        self._player.signals.stateChanged.connect(self._on_player_state)

    # ------------------------------------------------------------------ #
    # 外部调用：刷新设备/IP 信息
    # ------------------------------------------------------------------ #
    def update_device_info(self, device_name: str, local_ip: str) -> None:
        self.deviceCard.valueLabel.setText(device_name)
        self.ipCard.valueLabel.setText(local_ip)

    def set_service_running(self, running: bool) -> None:
        self.statusDot.set_status("running" if running else "stopped")
        self.statusText.setText(tr("home.status.running" if running else "home.status.stopped"))
        self.startButton.setEnabled(not running)
        self.stopButton.setEnabled(running)
        if running:
            # 刷新引导文案（带设备名）
            name = self.deviceCard.valueLabel.text()
            self.tipBody.setText(tr("home.tip.body", name=name))

    # ------------------------------------------------------------------ #
    def _on_media_changed(self, title: str, url: str) -> None:
        display = title or tr("player.unknown_title")
        self.playingCard.valueLabel.setText(display)

    def _on_player_state(self, state: str) -> None:
        if state in ("idle", "stopped"):
            if self._player.get_duration() is None:
                self.playingCard.valueLabel.setText(tr("home.nothing_playing"))

    def show_info(self, title: str, body: str, is_warning: bool = False) -> None:
        kind = InfoBarIcon.WARNING if is_warning else InfoBarIcon.INFORMATION
        InfoBar.show(
            title=title,
            content=body,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.titleLabel.setText(tr("home.title"))
        self.deviceCard.titleLabel.setText(tr("home.device_name"))
        self.ipCard.titleLabel.setText(tr("home.device_ip"))
        self.playingCard.titleLabel.setText(tr("home.now_playing"))
        self.startButton.setText(tr("home.start_service"))
        self.stopButton.setText(tr("home.stop_service"))
        self.tipTitle.setText(tr("home.tip.title"))
        name = self.deviceCard.valueLabel.text() or "YDLNA"
        self.tipBody.setText(tr("home.tip.body", name=name))
        running = self.statusDot.status == "running"
        self.statusText.setText(tr("home.status.running" if running else "home.status.stopped"))
        if self._player.get_duration() is None:
            self.playingCard.valueLabel.setText(tr("home.nothing_playing"))

    def retranslate_ui(self) -> None:
        self._retranslate()
