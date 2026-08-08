"""主窗口：MSFluentWindow 框架，组合三个子页面。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon
from qfluentwidgets import (
    FluentIcon as FIF,
    MSFluentWindow,
    NavigationItemPosition,
)

from ..config import Config
from ..constants import APP_DISPLAY_NAME
from ..i18n import tr, Translator
from ..logger import get_logger
from .home_interface import HomeInterface
from ..player.player_interface import PlayerInterface
from .settings_interface import SettingsInterface

if TYPE_CHECKING:
    from ..dlna.server import DlnaServer
    from ..player.mpv_player import Player

log = get_logger("ui.main")


class MainWindow(MSFluentWindow):
    """主窗口。"""

    # 用户关闭窗口时（用于决定是否最小化到托盘）
    closing = Signal()

    def __init__(
        self,
        player: "Player",
        server: "DlnaServer",
        config: Config,
        parent=None,
    ) -> None:  # noqa: ANN001
        super().__init__(parent)
        # 禁用 Mica 背景特效：全屏/还原切换时 DWM 重建表面，Mica 失效会
        # 回退到系统浅色背景（表现为"退出全屏瞬间整体闪浅色"）。
        # 纯 QSS 背景在两种状态下表现一致，无闪烁
        try:
            self.setMicaEffectEnabled(False)
        except Exception:  # noqa: BLE001
            pass
        self._player = player
        self._server = server
        self._config = config
        self._minimize_hint_shown = bool(config.get("minimize_hint_shown", False))

        # 三个子页面
        self.homeInterface = HomeInterface(player, server, self)
        self.playerInterface = PlayerInterface(player, self)
        self.settingsInterface = SettingsInterface(config, player, self)

        self.addSubInterface(self.homeInterface, FIF.HOME, tr("nav.home"))
        self.addSubInterface(self.playerInterface, FIF.VIDEO, tr("nav.player"))
        self.addSubInterface(
            self.settingsInterface, FIF.SETTING, tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )

        self.setWindowTitle(APP_DISPLAY_NAME)
        # 目标 ~1500×1000 屏幕物理像素（用户以屏幕像素理解窗口尺寸）。
        # Qt 的 resize 用的是逻辑像素（与 DPI 无关），须按屏幕缩放换算：
        # 125% 缩放 → 1500px 物理 = 1200 逻辑单位，显示出来才和预期一致。
        # 屏幕物理区域不够大时按可用区收缩；几何版本号升级时替换一次旧几何
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        phys_w, phys_h = 1500, 1000
        if screen is not None:
            avail = screen.availableGeometry()
            phys_w = min(phys_w, int(avail.width() * dpr) - 60)
            phys_h = min(phys_h, int(avail.height() * dpr) - 80)
        self.resize(int(phys_w / dpr), int(phys_h / dpr))
        self.setMinimumSize(900, 600)
        if not config.get("window_geometry_v6", False):
            config.set("window_geometry_v6", True)
        else:
            geom = config.get("window_geometry")
            if isinstance(geom, list) and len(geom) == 4:
                try:
                    self.setGeometry(*geom)
                except Exception:  # noqa: BLE001
                    pass

        # 设置图标（若有）
        icon = self._load_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        # 页面切换的原生窗口 hide/show 由 PlayerInterface 的
        # showEvent/hideEvent 自行处理（QStackedWidget 切换会触发这两个事件）

        Translator.instance().languageChanged.connect(self._on_language_changed)

    def _load_icon(self) -> QIcon:
        from ..constants import ASSETS_DIR
        for name in ("icon.ico", "icon.png", "logo.png"):
            p = ASSETS_DIR / name
            if p.exists():
                return QIcon(str(p))
        return QIcon()

    # ------------------------------------------------------------------ #
    def switch_to_player(self) -> None:
        """切到播放器页面（投屏开始时调用）。"""
        try:
            # MSFluentWindow 通过 navigationInterface 切换
            self.switchTo(self.playerInterface)
        except Exception:  # noqa: BLE001
            self.playerInterface.show()

    # ------------------------------------------------------------------ #
    # 全屏（隐藏导航栏 + 标题栏 + 播放器页 header，播放器页占满）
    # ------------------------------------------------------------------ #
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.navigationInterface.show()
            if hasattr(self, "titleBar"):
                self.titleBar.show()
            # 恢复播放器页 header（标题栏）
            self.playerInterface.header.show()
            # 恢复 48px 顶部边距（MSFluentWindow 给悬浮标题栏留的位置）
            self.hBoxLayout.setContentsMargins(0, 48, 0, 0)
            self.playerInterface.on_fullscreen_changed(False)
        else:
            # 先切到播放器页
            self.switch_to_player()
            self.navigationInterface.hide()
            if hasattr(self, "titleBar"):
                self.titleBar.hide()
            # 隐藏播放器页 header（避免全屏时露出标题/按钮）
            self.playerInterface.header.hide()
            # 标题栏是浮层（不在布局里），布局靠固定 48px 上边距让位；
            # 全屏隐藏标题栏后必须清掉这个边距，否则顶部残留 48px 空条
            self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
            self.showFullScreen()
            self.playerInterface.on_fullscreen_changed(True)
        self.playerInterface._position_overlays()

    def refresh_device_info(self, name: str, ip: str) -> None:
        self.homeInterface.update_device_info(name, ip)

    # ------------------------------------------------------------------ #
    def _set_nav_text(self, widget: QWidget, text: str) -> None:  # noqa: ANN001
        """设置左侧导航项的文本（按 widget 的 objectName 查找）。"""
        try:
            btn = self.navigationInterface.items.get(widget.objectName())
            if btn is not None and hasattr(btn, "setText"):
                btn.setText(text)
                btn.update()
        except Exception:  # noqa: BLE001
            pass

    def _on_language_changed(self, _code: str) -> None:
        # 刷新导航项文本
        self._set_nav_text(self.homeInterface, tr("nav.home"))
        self._set_nav_text(self.playerInterface, tr("nav.player"))
        self._set_nav_text(self.settingsInterface, tr("nav.settings"))
        # 刷新各子页面文案
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        for child in (self.homeInterface, self.playerInterface, self.settingsInterface):
            if hasattr(child, "retranslate_ui"):
                child.retranslate_ui()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """关闭窗口时最小化到托盘（首次弹提示，可勾选「不再提示」）。

        系统触发的关闭（Windows 关机/注销、安装器 Restart Manager 的
        关闭请求）不走托盘逻辑：保存几何后直接退出进程——否则应用会
        赖在托盘不退出，安装器无法覆盖升级 exe（只能等强杀）。
        """
        # 保存几何
        g = self.geometry()
        self._config.set("window_geometry", [g.x(), g.y(), g.width(), g.height()])

        if event.spontaneous():
            # 系统/安装器触发的关闭：优雅退出（不弹最小化提示，不残留进程）
            log.info("系统触发的关闭（关机/注销/安装器关闭请求），直接退出")
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
            event.accept()
            return

        if not self._minimize_hint_shown:
            try:
                self._ask_minimize_to_tray()
            except Exception as e:  # noqa: BLE001
                # 弹窗失败绝不能把应用卡死：保持窗口可见并记录
                log.warning("最小化提示弹窗异常: %s", e)
            event.ignore()
        else:
            # 已经提示过（或用户勾选「不再提示」），直接最小化
            self.hide()
            self.closing.emit()
            event.ignore()

    def _ask_minimize_to_tray(self) -> None:
        """弹「最小化到托盘」提示（带「不再提示」复选框）。

        使用 Qt 原生 QMessageBox + 非阻塞 open()：不嵌套事件循环，
        彻底避免模态对话框把应用卡死（此前 qfluentwidgets MessageBox
        的 exec() 在打包版中点击按钮会冻结）。
        """
        from PySide6.QtWidgets import QCheckBox, QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle(tr("dialog.minimize_to_tray.title"))
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(tr("dialog.minimize_to_tray.body"))
        dont_ask = QCheckBox(tr("dialog.minimize_to_tray.dont_ask"), box)
        box.setCheckBox(dont_ask)
        ok_btn = box.addButton(tr("common.ok"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("common.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(ok_btn)
        box.finished.connect(
            lambda _result: self._on_minimize_answered(box, dont_ask, ok_btn)
        )
        # 关闭后销毁弹窗对象（否则每次点 X 都会累积一个隐藏的 QMessageBox）
        box.finished.connect(box.deleteLater)
        box.open()

    def _on_minimize_answered(self, box, dont_ask, ok_btn) -> None:  # noqa: ANN001
        """弹窗关闭后的回调：按勾选状态决定「记住」与「是否最小化」。"""
        if dont_ask.isChecked():
            self._config.set("minimize_hint_shown", True)
            self._minimize_hint_shown = True
            log.info("用户选择不再提示「最小化到托盘」")
        if box.clickedButton() is ok_btn:
            self.hide()
            self.closing.emit()
