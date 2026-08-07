"""系统托盘：QSystemTrayIcon + Fluent 风格菜单。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import Action, FluentIcon as FIF, SystemTrayMenu

from ..i18n import tr, Translator
from ..logger import get_logger

if TYPE_CHECKING:
    from ..player.mpv_player import Player

log = get_logger("ui.tray")


class TrayIcon(QSystemTrayIcon):
    """系统托盘图标。"""

    def __init__(self, player: "Player", parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._player = player
        self._parent = parent

        self.setIcon(self._default_icon())
        self.setToolTip(tr("tray.tooltip.idle"))

        self.menu = SystemTrayMenu(parent=parent)
        self.actShow = Action(FIF.APPLICATION, tr("tray.show"))
        self.actPlayPause = Action(FIF.PLAY, tr("tray.play_pause"))
        self.actStop = Action(FIF.CANCEL, tr("tray.stop"))
        self.actQuit = Action(FIF.CLOSE, tr("tray.quit"))
        self.menu.addAction(self.actShow)
        self.menu.addAction(self.actPlayPause)
        self.menu.addAction(self.actStop)
        self.menu.addSeparator()
        self.menu.addAction(self.actQuit)
        self.setContextMenu(self.menu)

        # 双击显示主窗口
        self.activated.connect(self._on_activated)

        # 播放状态变化时更新 tooltip
        self._player.signals.stateChanged.connect(self._on_state_changed)
        self._player.signals.mediaChanged.connect(self._on_media_changed)

        Translator.instance().languageChanged.connect(self._retranslate)

    def _default_icon(self) -> QIcon:
        """获取托盘图标：优先用窗口图标，否则用 FluentIcon 生成一个。"""
        if self._parent is not None and not self._parent.windowIcon().isNull():
            return self._parent.windowIcon()
        # 兜底：用 FluentIcon 的 VIDEO 生成一个非空图标，避免
        # "QSystemTrayIcon::setVisible: No Icon set" 警告
        from qfluentwidgets import FluentIcon as _FIF
        try:
            return _FIF.VIDEO.icon()
        except Exception:  # noqa: BLE001
            return QIcon()

    # ------------------------------------------------------------------ #
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:  # noqa: N802
        if reason == QSystemTrayIcon.DoubleClick:
            self.actShow.trigger()

    def _on_state_changed(self, state: str) -> None:
        icon = FIF.PAUSE if state == "playing" else FIF.PLAY
        # Action.setIcon 接受 FluentIconBase 或 QIcon
        try:
            self.actPlayPause.setIcon(icon)
        except Exception:  # noqa: BLE001
            pass

    def _on_media_changed(self, title: str, url: str) -> None:
        self.setToolTip(tr("tray.tooltip.playing", title=title or tr("player.unknown_title")))

    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.actShow.setText(tr("tray.show"))
        self.actPlayPause.setText(tr("tray.play_pause"))
        self.actStop.setText(tr("tray.stop"))
        self.actQuit.setText(tr("tray.quit"))
        if self._player.get_state() == "idle":
            self.setToolTip(tr("tray.tooltip.idle"))
